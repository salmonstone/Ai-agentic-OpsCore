"""Auto-scaling policies — scheduled and metric-based scaling.

Saves compute cost by scaling deployments down at night (17:30 UTC = 11pm IST)
and back up at peak (03:30 UTC = 9am IST). Also scales up reactively when a
deployment's CPU is sustained above its policy threshold.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.integrations import autoscale_db
from agent.observability.logging import get_logger

log = get_logger(__name__)

DEFAULT_DOWN_UTC = "17:30"
DEFAULT_UP_UTC = "03:30"
DEFAULT_MAX_REPLICAS = 10


def schedule_due(now: datetime, target_utc: str | None) -> bool:
    """True once `now` (UTC) has reached target_utc's "HH:MM" for today.

    Reaching the target, not matching it exactly: the old check was
    `now_hhmm == target_utc`, tested by the schedule watcher every 60s. Any
    poll that landed even a few seconds late — a slow prior iteration, a
    laptop that was asleep — skipped that minute and the scale-down (or
    scale-up) simply didn't happen for the whole day. Same class of bug as
    the old nightly-check trigger (`core.daemon.nightly_due`), fixed the
    same way: a "due" window instead of one exact tick.
    """
    if not target_utc:
        return False
    try:
        target_h, target_m = (int(x) for x in target_utc.split(":", 1))
    except (ValueError, AttributeError):
        return False
    return (now.hour, now.minute) >= (target_h, target_m)


def _get_current_replicas(deployment: str, namespace: str) -> int:
    from agent.integrations.kubectl import run_kubectl
    res = run_kubectl(["get", "deployment", deployment, "-n", namespace, "-o",
                       "jsonpath={.spec.replicas}"])
    return int(res.output.strip()) if res.success and res.output.strip().isdigit() else 1


def _set_replicas(deployment: str, namespace: str, replicas: int) -> bool:
    from agent.integrations.kubectl import run_kubectl
    res = run_kubectl(["scale", "deployment", deployment,
                       "-n", namespace, f"--replicas={replicas}"])
    return res.success


def _find_policy(deployment: str, namespace: str) -> dict | None:
    """Return the first enabled policy matching deployment + namespace."""
    for p in autoscale_db.list_policies(enabled_only=True):
        if p["deployment"] == deployment and p["namespace"] == namespace:
            return p
    return None


def _notify(deployment: str, namespace: str, action: str,
            old_r: int, new_r: int, reason: str) -> None:
    """Best-effort Slack notification — never raises."""
    try:
        from agent.integrations.slack import send_alert_generic
        send_alert_generic(
            title=f"Auto-scaled {deployment}",
            message=f"Scaled {action}: {old_r} -> {new_r} replicas",
            severity="info",
            fields={"Deployment": deployment, "Namespace": namespace,
                    "Reason": reason},
        )
    except Exception as exc:
        log.warning("autoscale.slack_failed", error=str(exc))


def apply_schedule(policy: dict, now: datetime | None = None) -> dict:
    """Check a policy against the current UTC time and scale if it's due.

    Scales to down_replicas once schedule_down_utc has passed for the day,
    up_replicas once schedule_up_utc has passed. Skips (no_action) before
    either time, or once already handled today.
    """
    from agent.integrations import daemon_db

    deployment = policy["deployment"]
    namespace = policy["namespace"]
    now = now or datetime.now(timezone.utc)

    down_utc = policy.get("schedule_down_utc")
    up_utc = policy.get("schedule_up_utc")

    if schedule_due(now, down_utc):
        action = "schedule_down"
        target = int(policy.get("down_replicas", 1))
        target_utc = down_utc
    elif schedule_due(now, up_utc):
        action = "schedule_up"
        target = int(policy.get("up_replicas", 3))
        target_utc = up_utc
    else:
        return {"action": "no_action", "deployment": deployment,
                "namespace": namespace, "old_replicas": 0,
                "new_replicas": 0, "success": True}

    # Reaching a target time stays true for the rest of the day, so without a
    # once-per-day claim this would re-fire on every poll after it, not just
    # once. Claimed before doing anything else — like the nightly checks — so
    # a crash partway through doesn't retry on the next poll either.
    claim_key = f"autoscale_schedule/{policy.get('id')}/{action}/{now:%Y-%m-%d}"
    if not daemon_db.claim_once(claim_key, cooldown_minutes=24 * 60):
        return {"action": "no_action", "deployment": deployment,
                "namespace": namespace, "old_replicas": 0,
                "new_replicas": 0, "success": True,
                "reason": "already_ran_today"}

    current = _get_current_replicas(deployment, namespace)
    if current == target:
        return {"action": "no_action", "deployment": deployment,
                "namespace": namespace, "old_replicas": current,
                "new_replicas": current, "success": True,
                "reason": "already_at_target"}

    success = _set_replicas(deployment, namespace, target)
    reason = f"scheduled {action} (due {target_utc} UTC, ran at {now:%H:%M})"
    autoscale_db.log_event(
        policy.get("id"), deployment, namespace, action,
        current, target, reason, status="done" if success else "failed",
    )
    if success:
        _notify(deployment, namespace, action, current, target, reason)

    return {"action": action, "deployment": deployment,
            "namespace": namespace, "old_replicas": current,
            "new_replicas": target, "success": success}


def run_scheduled_scaling(now: datetime | None = None) -> list[dict]:
    """Apply every enabled policy against the current time. Returns results."""
    now = now or datetime.now(timezone.utc)
    results: list[dict] = []
    for policy in autoscale_db.list_policies(enabled_only=True):
        try:
            results.append(apply_schedule(policy, now))
        except Exception as exc:
            log.error("autoscale.apply_schedule.error",
                      deployment=policy.get("deployment"), error=str(exc))
    return results


def add_default_policies() -> list[str]:
    """Discover cluster deployments and create sensible default scaling policies.

    Skips kube-system. Idempotent: a deployment that already has a policy is
    left untouched. Returns the list of newly created policy ids.
    """
    from agent.integrations.kubectl import run_kubectl

    res = run_kubectl(["get", "deployments", "-A", "-o", "json"])
    if not res.success or not res.output.strip():
        log.warning("autoscale.add_default_policies.no_deployments")
        return []

    try:
        items = json.loads(res.output).get("items", [])
    except Exception as exc:
        log.warning("autoscale.add_default_policies.parse_error", error=str(exc))
        return []

    created: list[str] = []
    for dep in items:
        meta = dep.get("metadata", {})
        name = meta.get("name", "")
        namespace = meta.get("namespace", "default")
        if not name or namespace == "kube-system":
            continue
        if _find_policy(name, namespace):
            continue

        current = int(dep.get("spec", {}).get("replicas", 1) or 1)
        down_r = max(1, current // 2)
        up_r = current if current >= 1 else 1

        policy_id = autoscale_db.add_policy(
            deployment=name,
            namespace=namespace,
            name=f"{name}-default",
            min_r=1,
            max_r=max(DEFAULT_MAX_REPLICAS, up_r),
            down_utc=DEFAULT_DOWN_UTC,
            up_utc=DEFAULT_UP_UTC,
            down_r=down_r,
            up_r=up_r,
            cpu_pct=80.0,
        )
        created.append(policy_id)
        log.info("autoscale.default_policy_added",
                 deployment=name, namespace=namespace,
                 down_replicas=down_r, up_replicas=up_r)

    return created


def scale_for_cpu(deployment: str, namespace: str,
                  current_pct: float, current_replicas: int) -> dict:
    """Scale up by one replica when CPU exceeds the policy threshold.

    Capped at the policy's max_replicas (or 10 when no policy exists).
    """
    policy = _find_policy(deployment, namespace)
    max_r = int(policy["max_replicas"]) if policy else DEFAULT_MAX_REPLICAS
    new_r = min(max_r, current_replicas + 1)

    if new_r == current_replicas:
        return {"action": "no_action", "deployment": deployment,
                "namespace": namespace, "old_replicas": current_replicas,
                "new_replicas": current_replicas, "success": True,
                "reason": "already_at_max"}

    success = _set_replicas(deployment, namespace, new_r)
    reason = f"CPU {current_pct:.0f}% above threshold"
    autoscale_db.log_event(
        policy.get("id") if policy else None,
        deployment, namespace, "cpu_scale_up",
        current_replicas, new_r, reason,
        status="done" if success else "failed",
    )
    if success:
        _notify(deployment, namespace, "cpu_scale_up",
                current_replicas, new_r, reason)

    return {"action": "cpu_scale_up", "deployment": deployment,
            "namespace": namespace, "old_replicas": current_replicas,
            "new_replicas": new_r, "success": success}


def get_scale_report() -> dict:
    return {
        "policies": autoscale_db.list_policies(),
        "recent_events": autoscale_db.get_events(limit=20),
        "stats": autoscale_db.get_stats(),
    }
