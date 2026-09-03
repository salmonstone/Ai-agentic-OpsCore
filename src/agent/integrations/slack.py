"""
Slack Incoming Webhook integration.

Set SLACK_WEBHOOK_URL in .env to enable.
Create a webhook at: https://api.slack.com/messaging/webhooks

All functions return False silently when not configured — never crash the watch loop.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from agent.core.models import ResourceAlert
from agent.observability.logging import get_logger

log = get_logger(__name__)

_SEVERITY_EMOJI  = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
_SEVERITY_COLOR  = {"critical": "#FF0000", "warning": "#FFA500", "info": "#0000FF"}
_ALERT_LABEL = {
    "OOM_RISK":  "OOM Risk — imminent kill",
    "MEM_HIGH":  "Memory High",
    "CPU_HIGH":  "CPU High / Throttling",
    "NO_LIMITS": "No Resource Limits",
}


def _webhook_url() -> str:
    try:
        from agent.config import settings
        return settings.slack_webhook_url
    except Exception:
        return ""


def _cluster_name() -> str:
    try:
        from agent.integrations.kubectl import get_current_context
        return get_current_context() or "k8s-cluster"
    except Exception:
        return "k8s-cluster"


def is_configured() -> bool:
    return bool(_webhook_url())


# ---------------------------------------------------------------------------
# Generic alert — used by security, k8s scan, log analysis, monitor daemon
# ---------------------------------------------------------------------------

def send_alert_generic(
    title: str,
    message: str,
    severity: str = "warning",
    fields: dict | None = None,
    fix_command: str | None = None,
) -> bool:
    """Send a rich Block Kit alert for any event type. Returns True if sent."""
    url = _webhook_url()
    if not url:
        return False

    emoji  = _SEVERITY_EMOJI.get(severity, "•")
    color  = _SEVERITY_COLOR.get(severity, "#888888")
    ts     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cluster = _cluster_name()

    # Header
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} {severity.upper()}: {title}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Cluster:*\n`{cluster}`"},
                {"type": "mrkdwn", "text": f"*Time:*\n{ts}"},
            ],
        },
    ]

    # Custom fields (pod, namespace, error_type, etc.)
    if fields:
        field_items = [
            {"type": "mrkdwn", "text": f"*{k}:*\n{v}"}
            for k, v in fields.items()
        ]
        for i in range(0, len(field_items), 2):
            blocks.append({"type": "section", "fields": field_items[i:i+2]})

    # Main message body
    if message:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Details:*\n{message}"},
        })

    # Fix command in code block
    if fix_command:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Suggested Fix:*\n```{fix_command}```"},
        })

    blocks.append({"type": "divider"})

    # Slack uses attachments for colored sidebars
    payload = {
        "text": f"{emoji} {severity.upper()} — {title}",
        "attachments": [{
            "color": color,
            "blocks": blocks,
        }],
    }

    try:
        r = httpx.post(url, json=payload, timeout=5)
        r.raise_for_status()
        log.info("slack.generic_sent", title=title, severity=severity)
        return True
    except Exception as exc:
        log.warning("slack.generic_failed", error=str(exc))
        return False


def send_alert_blocks(blocks: list[dict]) -> bool:
    """Send pre-built Block Kit blocks directly."""
    url = _webhook_url()
    if not url:
        return False
    try:
        r = httpx.post(url, json={"blocks": blocks}, timeout=5)
        r.raise_for_status()
        return True
    except Exception as exc:
        log.warning("slack.blocks_failed", error=str(exc))
        return False


def send_resolved_generic(title: str, detail: str = "") -> bool:
    """Send a green 'RESOLVED' notification."""
    url = _webhook_url()
    if not url:
        return False
    text = f"✅ RESOLVED: {title}"
    if detail:
        text += f"\n{detail}"
    payload = {
        "text": text,
        "attachments": [{
            "color": "#00AA00",
            "blocks": [{
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"✅ *RESOLVED:* {title}\n{detail}"},
            }],
        }],
    }
    try:
        r = httpx.post(url, json=payload, timeout=5)
        r.raise_for_status()
        log.info("slack.resolved_sent", title=title)
        return True
    except Exception as exc:
        log.warning("slack.resolved_failed", error=str(exc))
        return False


def test_connection() -> bool:
    """Send a test message to verify the webhook works. Returns True on success."""
    url = _webhook_url()
    if not url:
        log.warning("slack.test.no_webhook_url")
        return False

    cluster = _cluster_name()
    payload = {
        "text": "✅ InfraGPT connected",
        "blocks": [{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"✅ *InfraGPT Slack integration working!*\n"
                    f"Cluster: `{cluster}`\n"
                    f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
                    "_You will receive alerts here when pods crash, resources spike, or security issues are found._"
                ),
            },
        }],
    }
    try:
        r = httpx.post(url, json=payload, timeout=5)
        r.raise_for_status()
        log.info("slack.test.ok")
        return True
    except Exception as exc:
        log.warning("slack.test.failed", error=str(exc))
        return False


def send_alert(alert: ResourceAlert) -> bool:
    """Send a single resource alert to Slack. Silent no-op if webhook not set."""
    url = _webhook_url()
    if not url:
        return False

    emoji = _SEVERITY_EMOJI.get(alert.severity, "•")
    label = _ALERT_LABEL.get(alert.alert_type, alert.alert_type)
    sev   = alert.severity.upper()
    name  = f"{alert.namespace}/{alert.pod_or_node}" if alert.namespace else alert.pod_or_node

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} {sev}: {label}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Resource:*\n`{name}`"},
                {"type": "mrkdwn", "text": f"*Usage:*\n{alert.current_usage}  ({alert.percent_used}% of {alert.limit})"},
                {"type": "mrkdwn", "text": f"*Recommendation:*\n{alert.recommendation}"},
            ],
        },
    ]

    if alert.fix_command:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Fix command:*\n```{alert.fix_command}```"},
        })

    blocks.append({"type": "divider"})

    payload = {
        "text": f"{emoji} {sev} — {name}: {alert.recommendation}",
        "blocks": blocks,
    }

    try:
        r = httpx.post(url, json=payload, timeout=5)
        r.raise_for_status()
        log.info("slack.alert_sent", resource=name, severity=alert.severity)
        return True
    except Exception as exc:
        log.warning("slack.alert_failed", error=str(exc))
        return False


def send_recovery(pod_or_node: str, namespace: str = "") -> bool:
    """Notify Slack that a previously critical resource is now healthy."""
    url = _webhook_url()
    if not url:
        return False

    name = f"{namespace}/{pod_or_node}" if namespace else pod_or_node
    payload = {
        "text": f"✅ RESOLVED — `{name}` is no longer critical",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✅ *RESOLVED* — `{name}` dropped below critical threshold",
                },
            }
        ],
    }
    try:
        r = httpx.post(url, json=payload, timeout=5)
        r.raise_for_status()
        log.info("slack.recovery_sent", resource=name)
        return True
    except Exception as exc:
        log.warning("slack.recovery_failed", error=str(exc))
        return False


def send_deploy_approval_request(pending) -> bool:
    """
    Send a Block Kit approval message with Approve / Reject buttons.
    Requires Slack App with Interactive Components enabled and
    SLACK_SIGNING_SECRET set so the /slack/actions callback can be verified.
    Returns True if sent.
    """
    url = _webhook_url()
    if not url:
        return False

    _RISK_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🔴", "critical": "🔴"}
    risk_emoji  = _RISK_EMOJI.get(pending.risk_label, "⚪")
    ts          = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    img_short   = pending.new_image.split("/")[-1] if "/" in pending.new_image else pending.new_image
    is_high_risk = pending.risk_label in ("high", "critical")

    approve_btn: dict = {
        "type":      "button",
        "text":      {"type": "plain_text", "text": "✅ Approve"},
        "style":     "primary",
        "action_id": "approve_deploy",
        "value":     pending.id,
    }
    if is_high_risk:
        approve_btn["confirm"] = {
            "title":   {"type": "plain_text", "text": "Confirm high-risk deploy"},
            "text":    {"type": "mrkdwn",
                        "text": f"Deploy *{pending.deployment}* to *{pending.namespace}*?\n"
                                f"Risk level: *{pending.risk_label.upper()}* ({pending.risk_score}/100)"},
            "confirm": {"type": "plain_text", "text": "Yes, deploy it"},
            "deny":    {"type": "plain_text", "text": "Cancel"},
        }

    reject_btn: dict = {
        "type":      "button",
        "text":      {"type": "plain_text", "text": "❌ Reject"},
        "style":     "danger",
        "action_id": "reject_deploy",
        "value":     pending.id,
    }

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🚀 Deploy Approval Required"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Deployment:*\n`{pending.deployment}`"},
                {"type": "mrkdwn", "text": f"*Namespace:*\n`{pending.namespace}`"},
                {"type": "mrkdwn", "text": f"*Image:*\n`{img_short}`"},
                {"type": "mrkdwn", "text": f"*Risk:*\n{risk_emoji} {pending.risk_label.upper()} ({pending.risk_score}/100)"},
                {"type": "mrkdwn", "text": f"*Repo:*\n`{pending.repo}` @ `{pending.branch}`"},
                {"type": "mrkdwn", "text": f"*Author:*\n{pending.author}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Commit:* `{pending.commit_sha[:8]}` — {pending.commit_message[:120]}\n"
                    f"*Deploy ID:* `{pending.id}`  |  *Time:* {ts}"
                ),
            },
        },
        {
            "type":     "actions",
            "elements": [approve_btn, reject_btn],
        },
        {"type": "divider"},
    ]

    payload = {
        "text": f"🚀 Deploy approval needed — {pending.deployment} ({pending.risk_label.upper()})",
        "attachments": [{
            "color":  "#FFA500" if not is_high_risk else "#FF0000",
            "blocks": blocks,
        }],
    }

    try:
        r = httpx.post(url, json=payload, timeout=5)
        r.raise_for_status()
        log.info("slack.deploy_approval_sent", deploy_id=pending.id, deployment=pending.deployment)
        return True
    except Exception as exc:
        log.warning("slack.deploy_approval_failed", error=str(exc))
        return False


def update_deploy_message(response_url: str, status: str, deployment: str, detail: str) -> bool:
    """
    Update the original approval message via Slack response_url after deploy completes.
    `status` is one of: APPROVED, REJECTED, SUCCESS, FAILED, ROLLED_BACK.
    """
    _ST = {
        "APPROVED":    ("✅", "#00AA00"),
        "REJECTED":    ("🚫", "#888888"),
        "SUCCESS":     ("✅", "#00AA00"),
        "FAILED":      ("❌", "#FF0000"),
        "ROLLED_BACK": ("↩️", "#FFA500"),
        "DEPLOYING":   ("⏳", "#0000FF"),
    }
    icon, color = _ST.get(status, ("•", "#888888"))

    try:
        r = httpx.post(
            response_url,
            json={
                "replace_original": True,
                "text": f"{icon} Deploy {status} — {deployment}",
                "attachments": [{
                    "color": color,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"{icon} *Deploy {status}* — `{deployment}`\n{detail}",
                            },
                        }
                    ],
                }],
            },
            timeout=5,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        log.warning("slack.update_deploy_message_failed", error=str(exc))
        return False


def send_watch_started(namespace: str, interval: int) -> bool:
    """Send a startup message when watch begins."""
    url = _webhook_url()
    if not url:
        return False

    payload = {
        "text": f"👁 Resource watch started — namespace: `{namespace}`, interval: {interval}s",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"👁 *Resource watch started*\n"
                        f"Namespace: `{namespace}` | Interval: `{interval}s`\n"
                        f"You will be notified when critical thresholds are breached."
                    ),
                },
            }
        ],
    }
    try:
        r = httpx.post(url, json=payload, timeout=5)
        r.raise_for_status()
        return True
    except Exception as exc:
        log.warning("slack.watch_start_failed", error=str(exc))
        return False
