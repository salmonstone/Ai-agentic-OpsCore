"""Incident management skill.

Handles opening, updating, and closing incidents.
Sends structured Slack thread messages.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from agent.integrations import incident_db
from agent.observability.logging import get_logger

log = get_logger(__name__)

_SEV_EMOJI = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
_SEV_COLOR = {"critical": "#FF0000", "warning": "#FFA500", "info": "#0000FF"}


def _webhook_url() -> str:
    try:
        from agent.config import settings
        return settings.slack_webhook_url
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Open
# ---------------------------------------------------------------------------

def open_incident(
    title: str,
    severity: str,
    service: str,
    namespace: str,
    cause: str,
    logs: str = "",
) -> str:
    """Open (or dedup into) an incident. Returns the incident id."""
    # 1. Dedup — reuse an open incident for the same service/namespace.
    existing = incident_db.get_open_incident(service, namespace)
    if existing:
        incident_db.add_event(
            existing["id"], "detected",
            detail=f"Recurring: {title} — {cause}",
        )
        log.info("incident.dedup", incident_id=existing["id"], service=service)
        return existing["id"]

    # 2. Create.
    incident_id = incident_db.open_incident(
        title=title, severity=severity, service=service,
        namespace=namespace, cause=cause,
    )

    # 3. Detected event.
    incident_db.add_event(incident_id, "detected", detail=cause or title)

    # 4. Slack.
    thread_ts = _send_incident_slack(
        incident_id, title, severity, service, namespace, cause
    )
    if thread_ts:
        incident_db.set_slack_thread(incident_id, thread_ts)

    # 5. Start burning error budget (if an SLO is defined for this service).
    try:
        from agent.skills.slo import track_incident_burn as _track_burn
        burn_id = _track_burn(incident_id, service, namespace, cause=cause)
        if burn_id:
            # store burn_id in incident events for later resolution
            incident_db.add_event(incident_id, "slo_burn_started", detail=burn_id)
    except Exception as exc:
        log.warning("incident.slo_burn_failed", error=str(exc))

    log.info("incident.opened", incident_id=incident_id, service=service,
             severity=severity)
    return incident_id


def _send_incident_slack(
    incident_id: str,
    title: str,
    severity: str,
    service: str,
    namespace: str,
    cause: str,
) -> str:
    """Send a rich Block Kit incident message. Returns thread_ts (empty for webhooks)."""
    url = _webhook_url()
    if not url:
        return ""

    emoji = _SEV_EMOJI.get(severity, "•")
    color = _SEV_COLOR.get(severity, "#888888")
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} INCIDENT: {title}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Service:*\n`{service or '—'}`"},
                {"type": "mrkdwn", "text": f"*Namespace:*\n`{namespace or '—'}`"},
                {"type": "mrkdwn", "text": f"*Severity:*\n{emoji} {severity.upper()}"},
                {"type": "mrkdwn", "text": f"*Time:*\n{ts}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Cause:*\n{cause or 'Unknown'}"},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"ID: `{incident_id}` | Respond: `agent incident resolve {incident_id}`",
                }
            ],
        },
        {"type": "divider"},
    ]

    payload = {
        "text": f"{emoji} INCIDENT — {title}",
        "attachments": [{"color": color, "blocks": blocks}],
    }

    try:
        r = httpx.post(url, json=payload, timeout=5)
        r.raise_for_status()
        log.info("incident.slack_sent", incident_id=incident_id)
    except Exception as exc:
        log.warning("incident.slack_failed", error=str(exc))

    # Incoming Webhooks don't return / support thread_ts — needs a Bot token.
    return ""


# ---------------------------------------------------------------------------
# Fix attempts
# ---------------------------------------------------------------------------

def add_fix_attempt(incident_id: str, action: str, success: bool) -> None:
    """Record a fix attempt against an incident and notify Slack."""
    incident_db.increment_fix_attempts(incident_id)
    incident_db.add_event(
        incident_id,
        "fix_attempted" if success else "fix_failed",
        detail=action,
    )
    outcome = "✅ succeeded" if success else "❌ failed"
    _send_followup(f"🔧 Fix attempted: {action} — {outcome}", "#FFA500")


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------

def resolve_incident(incident_id: str, cause: str = "auto-healed") -> None:
    """Mark an incident resolved and notify Slack."""
    incident_db.resolve_incident(incident_id, auto_fixed=True, note=cause)
    incident_db.add_event(incident_id, "resolved", detail=cause)

    inc = incident_db.get_incident(incident_id)
    title    = inc.get("title", incident_id) if inc else incident_id
    duration = inc.get("duration_sec", 0) if inc else 0
    minutes  = round(duration / 60.0, 1)
    _send_followup(f"✅ RESOLVED: {title} — Duration: {minutes}min", "#00AA00")

    # End the SLO burn (charge the incident's downtime against the budget).
    try:
        if inc:
            for ev in inc.get("events", []):
                if ev.get("event_type") == "slo_burn_started":
                    from agent.skills.slo import end_incident_burn as _end_burn
                    _end_burn(ev["detail"], duration)
                    break
    except Exception as exc:
        log.warning("incident.slo_end_burn_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Escalate
# ---------------------------------------------------------------------------

def escalate_incident(incident_id: str, reason: str) -> None:
    """Escalate an incident to a human and notify Slack."""
    incident_db.add_event(incident_id, "escalated", detail=reason)
    _send_followup(
        f"🚨 ESCALATED: needs human intervention — {reason}", "#FF0000"
    )


# ---------------------------------------------------------------------------
# Slack helper
# ---------------------------------------------------------------------------

def _send_followup(text: str, color: str = "#888888") -> None:
    url = _webhook_url()
    if not url:
        return
    payload = {
        "text": text,
        "attachments": [{
            "color": color,
            "blocks": [{
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            }],
        }],
    }
    try:
        r = httpx.post(url, json=payload, timeout=5)
        r.raise_for_status()
    except Exception as exc:
        log.warning("incident.followup_failed", error=str(exc))
