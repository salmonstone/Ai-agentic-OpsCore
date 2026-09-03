"""SLO and Error Budget management skill.

Wires SLOs to the incident and deploy pipelines: incidents burn budget,
threshold crossings page Slack, and an exhausted budget auto-blocks deploys.
All Slack/DB failures are swallowed so this layer never breaks a watch loop.
"""
from __future__ import annotations

from agent.integrations import slo_db
from agent.observability.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Incident burn lifecycle
# ---------------------------------------------------------------------------

def track_incident_burn(
    incident_id: str,
    service: str,
    namespace: str,
    cause: str = "",
) -> str | None:
    """Start burning error budget for an incident. Returns burn_id or None."""
    slo = slo_db.get_slo_by_service(service, namespace)
    if not slo:
        return None
    burn_id = slo_db.record_burn(slo["id"], cause=cause, incident_id=incident_id)
    _check_and_alert(slo["id"])
    return burn_id


def end_incident_burn(burn_id: str, duration_sec: int) -> None:
    """End the SLO burn opened for an incident, recording its duration."""
    slo_db.end_burn(burn_id, duration_sec)


# ---------------------------------------------------------------------------
# Threshold alerting
# ---------------------------------------------------------------------------

def _check_and_alert(slo_id: str) -> None:
    """Check budget status and send a Slack alert when a threshold is crossed."""
    status  = slo_db.get_budget_status(slo_id)
    pct     = status["budget_pct_remaining"]
    service = status["service"]

    if pct <= 0:
        _send_budget_alert(
            service, status, "exhausted",
            f"\U0001F6A8 Error budget EXHAUSTED for `{service}`. "
            f"All {status['allowed_downtime_min']:.1f} min used. "
            f"New deploys are BLOCKED until next month.",
        )
    elif pct <= 20:
        _send_budget_alert(
            service, status, "critical",
            f"\U0001F534 Error budget CRITICAL for `{service}`: "
            f"{pct:.0f}% remaining ({status['remaining_min']:.1f} min left). "
            f"Deploys AUTO-BLOCKED.",
        )
    elif pct <= 50:
        _send_budget_alert(
            service, status, "warning",
            f"\U0001F7E1 Error budget WARNING for `{service}`: "
            f"{pct:.0f}% remaining ({status['remaining_min']:.1f} min left).",
        )


def _send_budget_alert(service: str, status: dict, severity: str, message: str) -> None:
    """Send an error-budget Slack alert. Never raises."""
    try:
        from agent.integrations.slack import send_alert_generic
        send_alert_generic(
            title=f"Error budget {severity.upper()} — {service}",
            message=message,
            severity="critical" if severity in ("exhausted", "critical") else "warning",
            fields={
                "Service":      service,
                "Budget left":  f"{status['budget_pct_remaining']:.0f}%",
                "Used":         f"{status['used_downtime_min']:.1f} min",
                "Remaining":    f"{status['remaining_min']:.1f} min",
                "Target":       f"{status['target_pct']}%",
                "Window":       f"{status['window_days']} days",
            },
        )
        log.info("slo.budget_alert", service=service, severity=severity,
                 pct=status["budget_pct_remaining"])
    except Exception as exc:
        log.warning("slo.budget_alert_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Deploy gate
# ---------------------------------------------------------------------------

def check_deploy_allowed(service: str, namespace: str) -> tuple[bool, str]:
    """Return (allowed, reason). Called by the deploy pipeline before approval."""
    if slo_db.is_deploy_blocked(service, namespace):
        slo = slo_db.get_slo_by_service(service, namespace)
        status = slo_db.get_budget_status(slo["id"]) if slo else {"budget_pct_remaining": 0.0}
        return (
            False,
            f"Deploy BLOCKED: error budget {status['budget_pct_remaining']:.0f}% remaining",
        )
    return True, ""


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def get_dashboard(service: str | None = None, namespace: str | None = None) -> list[dict]:
    """Return budget status for all SLOs, optionally filtered by service/namespace."""
    out: list[dict] = []
    for slo in slo_db.list_slos(active_only=True):
        if service is not None and slo["service"] != service:
            continue
        if namespace is not None and slo["namespace"] != namespace:
            continue
        out.append(slo_db.get_budget_status(slo["id"]))
    return out
