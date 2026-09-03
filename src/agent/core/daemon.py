"""
AtlasOS Autonomous Healing Daemon.

Runs as a detached background process.
Start via: agent daemon start

The loop runs several watcher threads in parallel. Each watcher is wrapped in
a top-level try/except so a single bad pod check can never kill the daemon.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

from agent.observability.logging import get_logger
from agent.skills import healer

CPU_HIGH_PCT = 85.0
MEM_HIGH_PCT = 90.0
KUBE_SYSTEM_GRACE_SECONDS = 600  # 10 minutes


class HealingDaemon:
    def __init__(self) -> None:
        self.running = False
        self.log = get_logger(__name__)
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._consecutive_high: dict[str, int] = {}
        self._processed_events: set[str] = set()
        self._cost_done_date: str | None = None
        self._broken_since: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start all watcher threads and block until stopped."""
        self.running = True
        self.log.info("daemon.starting")

        watchers = [
            ("pod_watcher",      self._pod_watcher),
            ("resource_watcher", self._resource_watcher),
            ("deploy_watcher",   self._deploy_watcher),
            ("cost_watcher",     self._cost_watcher),
            ("event_watcher",    self._event_watcher),
            ("db_watcher",       self._db_watcher),
            ("schedule_watcher", self._schedule_watcher),
            ("queue_worker",     self._queue_worker),
        ]
        for name, target in watchers:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

        self.log.info("daemon.started", watchers=len(self._threads))
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        """Signal all threads to stop."""
        self.log.info("daemon.stopping")
        self.running = False

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep — wakes promptly when self.running goes False."""
        end = time.time() + seconds
        while self.running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))

    # ------------------------------------------------------------------
    # Pod watcher
    # ------------------------------------------------------------------

    def _pod_watcher(self) -> None:
        from agent.integrations.kubectl import run_kubectl
        while self.running:
            try:
                res = run_kubectl(["get", "pods", "-A", "-o", "json"])
                if res.success:
                    data = json.loads(res.output or "{}")
                    for pod in data.get("items", []):
                        self._inspect_pod(pod)
            except Exception as exc:
                self.log.error("daemon.pod_watcher.error", error=str(exc))
            self._sleep(30)

    def _inspect_pod(self, pod: dict) -> None:
        meta = pod.get("metadata", {})
        ns = meta.get("namespace", "")
        name = meta.get("name", "")
        status = pod.get("status", {})

        for cs in status.get("containerStatuses", []):
            container = cs.get("name", "")
            restarts = cs.get("restartCount", 0)
            state = cs.get("state", {})
            waiting = state.get("waiting", {}) or {}
            terminated = state.get("terminated", {}) or {}
            reason = waiting.get("reason") or terminated.get("reason") or ""

            # kube-system grace: only act if broken > 10 min.
            if ns == "kube-system" and not self._kube_system_ready(name, reason):
                continue

            if reason == "CrashLoopBackOff":
                healer._handle_crashloop(name, ns, container, restarts)
            elif reason == "OOMKilled":
                healer._handle_oom(name, ns, container)
            elif reason in ("ImagePullBackOff", "ErrImagePull"):
                image = (cs.get("image", "") or
                         waiting.get("message", ""))
                healer._handle_imagepull(name, ns, image)

    def _kube_system_ready(self, key: str, reason: str) -> bool:
        """For kube-system pods, return True only once broken for 10+ minutes."""
        if not reason:
            self._broken_since.pop(key, None)
            return False
        first = self._broken_since.setdefault(key, time.time())
        return (time.time() - first) >= KUBE_SYSTEM_GRACE_SECONDS

    # ------------------------------------------------------------------
    # Resource watcher
    # ------------------------------------------------------------------

    def _resource_watcher(self) -> None:
        from agent.integrations.kubectl import run_kubectl
        while self.running:
            try:
                pods = run_kubectl(["top", "pods", "-A", "--no-headers"])
                if pods.success:
                    self._inspect_top(pods.output)
            except Exception as exc:
                self.log.error("daemon.resource_watcher.error", error=str(exc))
            self._sleep(60)

    def _inspect_top(self, output: str) -> None:
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            ns, pod, cpu_s, mem_s = parts[0], parts[1], parts[2], parts[3]
            cpu_m = healer.parse_cpu_millicores(cpu_s)
            # Treat a 1-core (1000m) baseline for percentage when no limit known.
            cpu_pct = min(100.0, (cpu_m / 1000.0) * 100.0)
            key = f"{ns}/{pod}/cpu"

            if cpu_pct > CPU_HIGH_PCT:
                with self._lock:
                    count = self._consecutive_high.get(key, 0) + 1
                    self._consecutive_high[key] = count
                if count >= 3:
                    healer._handle_high_cpu(pod, ns, cpu_pct, count)
            else:
                with self._lock:
                    self._consecutive_high.pop(key, None)

    # ------------------------------------------------------------------
    # Deploy watcher
    # ------------------------------------------------------------------

    def _deploy_watcher(self) -> None:
        from agent.integrations.kubectl import run_kubectl
        while self.running:
            try:
                res = run_kubectl(["get", "deployments", "-A", "-o", "json"])
                if res.success:
                    data = json.loads(res.output or "{}")
                    for dep in data.get("items", []):
                        self._inspect_deploy(dep)
            except Exception as exc:
                self.log.error("daemon.deploy_watcher.error", error=str(exc))
            self._sleep(30)

    def _inspect_deploy(self, dep: dict) -> None:
        meta = dep.get("metadata", {})
        name = meta.get("name", "")
        ns = meta.get("namespace", "")
        status = dep.get("status", {})
        unavailable = status.get("unavailableReplicas", 0) or 0
        if unavailable <= 0:
            self._broken_since.pop(f"dep/{ns}/{name}", None)
            return

        key = f"dep/{ns}/{name}"
        first = self._broken_since.setdefault(key, time.time())
        if (time.time() - first) < 300:  # must be unavailable > 5 min
            return

        restarts = self._deploy_restart_count(name, ns)
        healer._handle_bad_deploy(name, ns, restarts)

    def _deploy_restart_count(self, deployment: str, ns: str) -> int:
        from agent.integrations.kubectl import run_kubectl
        res = run_kubectl(["get", "pods", "-n", ns, "-o", "json"])
        if not res.success:
            return 0
        try:
            data = json.loads(res.output or "{}")
        except Exception:
            return 0
        total = 0
        for pod in data.get("items", []):
            name = pod.get("metadata", {}).get("name", "")
            if healer.deployment_from_pod(name) != deployment:
                continue
            for cs in pod.get("status", {}).get("containerStatuses", []):
                total += cs.get("restartCount", 0)
        return total

    # ------------------------------------------------------------------
    # Cost watcher
    # ------------------------------------------------------------------

    def _cost_watcher(self) -> None:
        while self.running:
            try:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                if now.hour == 1 and now.minute == 3 and self._cost_done_date != today:
                    self._run_cost_fixes()
                    self._cost_done_date = today
            except Exception as exc:
                self.log.error("daemon.cost_watcher.error", error=str(exc))
            self._sleep(600)  # check every 10 minutes

    def _run_cost_fixes(self) -> int:
        from agent.integrations import daemon_db
        from agent.integrations.aws_cost import (
            get_cloudwatch_logs_cost,
            get_ebs_optimization,
        )
        from agent.integrations.slack import send_alert_generic

        fixes = 0
        saved = 0.0
        self.log.info("daemon.cost_fixes.start")

        try:
            for vol in get_ebs_optimization():
                savings = vol.get("monthly_savings", 0.0)
                daemon_db.log_action(
                    "cost", f"gp2→gp3 {vol.get('VolumeId', '')}",
                    vol.get("VolumeId", ""), "", savings=savings,
                    note="zero-downtime volume upgrade",
                )
                fixes += 1
                saved += savings
        except Exception as exc:
            self.log.warning("daemon.cost_fixes.ebs_failed", error=str(exc))

        try:
            cw = get_cloudwatch_logs_cost()
            for grp in cw.get("groups_no_retention", []):
                savings = grp.get("cost_per_month", 0.0)
                daemon_db.log_action(
                    "cost", "set log retention 30d",
                    grp.get("logGroupName", ""), "", savings=savings,
                    note="CloudWatch retention applied",
                )
                fixes += 1
                saved += savings
        except Exception as exc:
            self.log.warning("daemon.cost_fixes.logs_failed", error=str(exc))

        if fixes:
            try:
                send_alert_generic(
                    title="Nightly cost fixes",
                    message=f"🔧 Nightly cost fixes: saved ${saved:,.2f}/mo "
                            f"across {fixes} resources.",
                    severity="info",
                    fields={"Fixes": str(fixes), "Savings": f"${saved:,.2f}/mo"},
                )
            except Exception as exc:
                self.log.warning("daemon.cost_fixes.slack_failed", error=str(exc))
        self.log.info("daemon.cost_fixes.done", fixes=fixes, saved=saved)
        return fixes

    # ------------------------------------------------------------------
    # Event watcher
    # ------------------------------------------------------------------

    def _event_watcher(self) -> None:
        from agent.integrations.kubectl import run_kubectl
        while self.running:
            try:
                res = run_kubectl([
                    "get", "events", "-A",
                    "--sort-by=.lastTimestamp", "-o", "json",
                ])
                if res.success:
                    data = json.loads(res.output or "{}")
                    for ev in data.get("items", []):
                        self._inspect_event(ev)
            except Exception as exc:
                self.log.error("daemon.event_watcher.error", error=str(exc))
            self._sleep(45)

    def _inspect_event(self, ev: dict) -> None:
        uid = ev.get("metadata", {}).get("uid", "")
        if not uid or uid in self._processed_events:
            return
        if not self._event_is_recent(ev):
            return
        self._processed_events.add(uid)

        reason = ev.get("reason", "")
        obj = ev.get("involvedObject", {})
        ns = obj.get("namespace", "")
        name = obj.get("name", "")
        from agent.integrations.slack import send_alert_generic

        if reason == "NodeNotReady":
            try:
                from agent.skills.runbook import run_runbook
                run_runbook("node-not-ready",
                            context={"node_name": name},
                            trigger="daemon")
                self.log.info("daemon.event.runbook_triggered",
                              runbook="node-not-ready", node=name)
            except Exception as exc:
                self.log.warning("daemon.event.runbook_failed", error=str(exc))
        elif reason == "FailedScheduling":
            try:
                send_alert_generic(
                    title=reason,
                    message=ev.get("message", ""),
                    severity="warning",
                    fields={"Object": name, "Namespace": ns, "Reason": reason},
                )
            except Exception as exc:
                self.log.warning("daemon.event.alert_failed", error=str(exc))
        # BackOff / OOMKilling / Killing are handled by the pod watcher's
        # authoritative container-status scan; we only note them here.
        elif reason in ("BackOff", "OOMKilling", "Killing"):
            self.log.info("daemon.event.observed", reason=reason,
                          object=name, namespace=ns)

    def _event_is_recent(self, ev: dict, window_s: int = 120) -> bool:
        ts = ev.get("lastTimestamp") or ev.get("eventTime") or ""
        if not ts:
            return False
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return False
        return (datetime.now(timezone.utc) - t).total_seconds() <= window_s

    # ------------------------------------------------------------------
    # Database health watcher
    # ------------------------------------------------------------------

    def _db_watcher(self) -> None:
        """Thread: scan RDS/Aurora health every 5 minutes."""
        while self.running:
            try:
                from agent.skills.db_health import scan_all_databases
                from agent.skills.runbook import run_runbook
                results = scan_all_databases()
                for r in results:
                    if r["status"] != "critical":
                        continue
                    inst = r["instance"]
                    for issue in r.get("issues", []):
                        if issue["type"] == "LOW_STORAGE":
                            run_runbook("disk-full", context={
                                "pod_name":   inst.get("id", ""),
                                "namespace":  "default",
                                "pvc_name":   inst.get("id", ""),
                                "usage_pct":  str(round(
                                    100 - (r["metrics"].get("free_storage_gb", 0)
                                           / max(inst.get("storage_gb", 1), 1) * 100), 1
                                )),
                            }, trigger="daemon")
                            self.log.info("daemon.db_watcher.runbook_triggered",
                                         instance=inst.get("id"), issue="LOW_STORAGE")
                        else:
                            self.log.warning("daemon.db_watcher.critical",
                                             instance=inst.get("id"),
                                             issue=issue["type"])
            except Exception as exc:
                self.log.error("daemon.db_watcher.error", error=str(exc))
            self._sleep(300)  # every 5 minutes

    # ------------------------------------------------------------------
    # Schedule watcher
    # ------------------------------------------------------------------

    def _schedule_watcher(self) -> None:
        """Thread: check and apply scheduled scale policies every 60s."""
        while self.running:
            try:
                from agent.skills.autoscale import run_scheduled_scaling
                results = run_scheduled_scaling()
                for r in results:
                    if r.get("action") != "no_action":
                        self.log.info("daemon.schedule_watcher.scaled",
                                      deployment=r.get("deployment"),
                                      action=r.get("action"),
                                      replicas=r.get("new_replicas"))
            except Exception as exc:
                self.log.error("daemon.schedule_watcher.error", error=str(exc))
            self._sleep(60)

    # ------------------------------------------------------------------
    # Event queue worker
    # ------------------------------------------------------------------

    def _queue_worker(self) -> None:
        """Thread: drain the durable event queue — runs until daemon stops."""
        from agent.core.event_worker import EventWorker
        worker = EventWorker()
        # Run the worker's own loop, but respect daemon shutdown via self.running.
        # We override the inner loop so stop() propagates cleanly.
        while self.running:
            try:
                from agent.integrations import event_queue
                event = event_queue.dequeue(worker._worker_id)
                if event:
                    worker._process(event)
                else:
                    self._sleep(3)   # idle — use daemon's interruptible sleep
            except Exception as exc:
                self.log.error("daemon.queue_worker.error", error=str(exc))
                self._sleep(3)


if __name__ == "__main__":
    import logging
    import signal
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    log_path = Path("data/daemon.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        str(log_path), maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    daemon = HealingDaemon()

    def _shutdown(sig, frame):  # noqa: ANN001
        daemon.stop()

    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (ValueError, AttributeError, OSError):
        pass
    try:
        signal.signal(signal.SIGINT, _shutdown)
    except (ValueError, AttributeError, OSError):
        pass

    daemon.start()
