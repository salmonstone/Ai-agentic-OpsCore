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


def apply_schedule(policy: dict) -> dict:
    """Check a policy against the current UTC time and scale if it matches.

    Scales to down_replicas at schedule_down_utc, up_replicas at schedule_up_utc.
    Skips (no_action) if not the scheduled minute or already at target.
    """
    deployment = policy["deployment"]
    namespace = policy["namespace"]
    now_hhmm = datetime.now(timezone.utc).strftime("%H:%M")

    down_utc = policy.get("schedule_down_utc")
    up_utc = policy.get("schedule_up_utc")

    if down_utc and now_hhmm == down_utc:
        action = "schedule_down"
        target = int(policy.get("down_replicas", 1))
    elif up_utc and now_hhmm == up_utc:
        action = "schedule_up"
        target = int(policy.get("up_replicas", 3))
    else:
        return {"action": "no_action", "deployment": deployment,
                "namespace": namespace, "old_replicas": 0,
                "new_replicas": 0, "success": True}

    current = _get_current_replicas(deployment, namespace)
    if current == target:
        return {"action": "no_action", "deployment": deployment,
                "namespace": namespace, "old_replicas": current,
                "new_replicas": current, "success": True,
                "reason": "already_at_target"}

    success = _set_replicas(deployment, namespace, target)
    reason = f"scheduled {action} at {now_hhmm} UTC"
    autoscale_db.log_event(
        policy.get("id"), deployment, namespace, action,
        current, target, reason, status="done" if success else "failed",
    )
    if success:
        _notify(deployment, namespace, action, current, target, reason)

    return {"action": action, "deployment": deployment,
            "namespace": namespace, "old_replicas": current,
            "new_replicas": target, "success": success}


def run_scheduled_scaling() -> list[dict]:
    """Apply every enabled policy against the current time. Returns results."""
    results: list[dict] = []
    for policy in autoscale_db.list_policies(enabled_only=True):
        try:
            results.append(apply_schedule(policy))
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
