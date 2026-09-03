"""
Event queue worker.

Polls the SQLite event queue and dispatches each event to the correct
skill handler. Can run as a standalone thread or embedded in the daemon.

Usage (standalone):
    worker = EventWorker()
    t = threading.Thread(target=worker.start, daemon=True)
    t.start()

Event type → handler mapping:
    pod.crashloop    → healer._handle_crashloop
    pod.oom          → healer._handle_oom
    pod.imagepull    → healer._handle_imagepull
    deploy.push      → deployment skill (analyze + create pending deploy)
    alert.slack      → slack.send_alert_generic
    runbook.trigger  → runbook.run_runbook
    cost.scan        → cost skill nightly scan
"""
from __future__ import annotations

import socket
import threading
import time

from agent.observability.logging import get_logger

log = get_logger(__name__)

_POLL_IDLE   = 3      # seconds to wait when queue is empty
_WORKER_ID   = f"{socket.gethostname()}-{threading.get_ident()}"


# ---------------------------------------------------------------------------
# Handlers  (each receives the decoded payload dict, returns a result dict)
# ---------------------------------------------------------------------------

def _handle_pod_crashloop(payload: dict) -> dict:
    from agent.skills.healer import _handle_crashloop
    return _handle_crashloop(
        pod_name       = payload["pod"],
        namespace      = payload["namespace"],
        container_name = payload.get("container", payload["pod"]),
        restart_count  = payload.get("restarts", 0),
    )


def _handle_pod_oom(payload: dict) -> dict:
    from agent.skills.healer import _handle_oom
    return _handle_oom(
        pod_name       = payload["pod"],
        namespace      = payload["namespace"],
        container_name = payload.get("container", payload["pod"]),
        current_limit_mb = payload.get("limit_mb", 0),
    )


def _handle_pod_imagepull(payload: dict) -> dict:
    from agent.skills.healer import _handle_imagepull
    return _handle_imagepull(
        pod_name  = payload["pod"],
        namespace = payload["namespace"],
        image     = payload.get("image", ""),
    )


def _handle_deploy_push(payload: dict) -> dict:
    """
    Receive a GitHub push event from the queue and run deployment analysis.
    Creates a PendingDeploy and sends a Slack approval request.
    """
    from agent.core.models import WebhookEvent
    from agent.integrations.mapping_loader import get_loader
    from agent.skills.deployment import DeploymentSkill

    repo   = payload.get("repo", "")
    branch = payload.get("branch", "")

    loader  = get_loader()
    mapping = loader.find_mapping(repo, branch)
    if mapping is None:
        log.info("event_worker.deploy_push.no_mapping", repo=repo, branch=branch)
        return {"skipped": True, "reason": "no mapping"}

    event = WebhookEvent(
        repo           = repo,
        branch         = branch,
        commit_sha     = payload.get("sha", ""),
        commit_message = payload.get("message", ""),
        author         = payload.get("author", "unknown"),
        files_changed  = payload.get("files", []),
        timestamp      = payload.get("timestamp", ""),
    )

    skill   = DeploymentSkill()
    pending = skill.analyze_webhook_deploy(mapping, event, payload.get("new_image", ""))
    return {"deploy_id": pending.id, "risk": pending.risk_label}


def _handle_alert_slack(payload: dict) -> dict:
    from agent.integrations.slack import send_alert_generic
    sent = send_alert_generic(
        title       = payload.get("title", "Alert"),
        message     = payload.get("message", ""),
        severity    = payload.get("severity", "warning"),
        fields      = payload.get("fields"),
        fix_command = payload.get("fix_command"),
    )
    return {"sent": sent}


def _handle_runbook(payload: dict) -> dict:
    from agent.skills.runbook import run_runbook
    result = run_runbook(
        name    = payload["runbook"],
        context = payload.get("context", {}),
        trigger = "event_worker",
    )
    return result if isinstance(result, dict) else {"result": str(result)}


def _handle_cost_scan(payload: dict) -> dict:
    from agent.integrations.aws_cost import get_ebs_optimization, get_cloudwatch_logs_cost
    fixes = 0
    for _ in get_ebs_optimization():
        fixes += 1
    return {"fixes_found": fixes}


# Registry: event_type → handler function
_HANDLERS: dict[str, callable] = {
    "pod.crashloop":   _handle_pod_crashloop,
    "pod.oom":         _handle_pod_oom,
    "pod.imagepull":   _handle_pod_imagepull,
    "deploy.push":     _handle_deploy_push,
    "alert.slack":     _handle_alert_slack,
    "runbook.trigger": _handle_runbook,
    "cost.scan":       _handle_cost_scan,
}


# ---------------------------------------------------------------------------
# Worker class
# ---------------------------------------------------------------------------

class EventWorker:
    """
    Single-threaded event queue consumer.

    Start it in a daemon thread — it blocks in a poll loop until stop() is called.
    Multiple EventWorker instances can run in parallel safely (SQLite row-level
    locking via the UPDATE claim in dequeue()).
    """

    def __init__(self) -> None:
        self.running    = False
        self._worker_id = f"{_WORKER_ID}-{id(self)}"

    def start(self) -> None:
        """Poll and process events until stop() is called."""
        from agent.integrations import event_queue
        self.running = True
        log.info("event_worker.started", worker_id=self._worker_id)

        while self.running:
            try:
                event = event_queue.dequeue(self._worker_id)
                if event:
                    self._process(event)
                else:
                    time.sleep(_POLL_IDLE)
            except Exception as exc:
                log.error("event_worker.loop_error", error=str(exc))
                time.sleep(_POLL_IDLE)

        log.info("event_worker.stopped", worker_id=self._worker_id)

    def stop(self) -> None:
        self.running = False

    def _process(self, event: dict) -> None:
        from agent.integrations import event_queue

        event_id   = event["id"]
        event_type = event["event_type"]
        payload    = event["payload"]

        handler = _HANDLERS.get(event_type)
        if handler is None:
            log.warning("event_worker.no_handler",
                        event_type=event_type, id=event_id)
            event_queue.fail(
                event_id,
                f"No handler registered for event type: {event_type!r}",
                retry=False,
            )
            return

        log.info("event_worker.processing", id=event_id[:8], event_type=event_type)
        t0 = time.monotonic()
        try:
            result   = handler(payload)
            duration = round((time.monotonic() - t0) * 1000)
            event_queue.complete(event_id, result)
            log.info("event_worker.done",
                     id=event_id[:8], event_type=event_type, ms=duration)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.error("event_worker.handler_failed",
                      id=event_id[:8], event_type=event_type, error=error)
            event_queue.fail(event_id, error)


def register_handler(event_type: str, fn: callable) -> None:
    """
    Register a custom handler at runtime.
    Use this from skills or plugins to add new event types without
    editing this file.

    Example:
        from agent.core.event_worker import register_handler
        register_handler("my.event", lambda payload: {"ok": True})
    """
    _HANDLERS[event_type] = fn
    log.info("event_worker.handler_registered", event_type=event_type)
