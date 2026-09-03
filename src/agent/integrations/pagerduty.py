"""PagerDuty and OpsGenie on-call paging integration.

Supports both providers — whichever has a key configured wins.
If both are configured, PagerDuty is tried first, OpsGenie second.

Required env vars (set at least one):
  PAGERDUTY_ROUTING_KEY  — PagerDuty Events API v2 routing key (32-char hex)
  OPSGENIE_API_KEY       — OpsGenie Alerts API key
  OPSGENIE_REGION        — "us" (default) or "eu"

Usage:
  from agent.integrations.pagerduty import page_oncall
  page_oncall(title="api crashlooping", body="...", severity="critical",
              service="api", incident_id="abc123")
"""
from __future__ import annotations

import json
from typing import Literal

import httpx

from agent.config import settings
from agent.observability.logging import get_logger

log = get_logger(__name__)

Severity = Literal["critical", "error", "warning", "info"]

_PD_URL = "https://events.pagerduty.com/v2/enqueue"
_OG_URL_US = "https://api.opsgenie.com/v2/alerts"
_OG_URL_EU = "https://api.eu.opsgenie.com/v2/alerts"


def _pd_key() -> str:
    return getattr(settings, "pagerduty_routing_key", "") or ""


def _og_key() -> str:
    return getattr(settings, "opsgenie_api_key", "") or ""


def _og_region() -> str:
    return getattr(settings, "opsgenie_region", "us") or "us"


def page_pagerduty(
    title: str,
    body: str,
    severity: Severity = "critical",
    service: str = "",
    incident_id: str = "",
    dedup_key: str = "",
) -> bool:
    """Send a PagerDuty Events API v2 trigger. Returns True on success."""
    key = _pd_key()
    if not key:
        log.warning("pagerduty.no_routing_key")
        return False

    payload = {
        "routing_key": key,
        "event_action": "trigger",
        "dedup_key": dedup_key or incident_id or title[:255],
        "payload": {
            "summary": title,
            "severity": severity,
            "source": service or "atlasos",
            "custom_details": {"body": body, "service": service, "incident_id": incident_id},
        },
    }
    try:
        resp = httpx.post(_PD_URL, json=payload, timeout=10)
        if resp.status_code in (200, 202):
            log.info("pagerduty.page_sent", title=title, severity=severity)
            return True
        log.warning("pagerduty.page_failed", status=resp.status_code, body=resp.text[:200])
        return False
    except Exception as exc:
        log.warning("pagerduty.page_error", error=str(exc))
        return False


def resolve_pagerduty(dedup_key: str) -> bool:
    """Resolve (acknowledge) a PagerDuty incident by dedup key."""
    key = _pd_key()
    if not key:
        return False
    payload = {
        "routing_key": key,
        "event_action": "resolve",
        "dedup_key": dedup_key,
    }
    try:
        resp = httpx.post(_PD_URL, json=payload, timeout=10)
        return resp.status_code in (200, 202)
    except Exception as exc:
        log.warning("pagerduty.resolve_error", error=str(exc))
        return False


def page_opsgenie(
    title: str,
    body: str,
    severity: Severity = "critical",
    service: str = "",
    incident_id: str = "",
    alias: str = "",
) -> bool:
    """Send an OpsGenie alert. Returns True on success."""
    key = _og_key()
    if not key:
        log.warning("opsgenie.no_api_key")
        return False

    # OpsGenie priority: P1=critical, P2=error, P3=warning, P4=info
    _prio = {"critical": "P1", "error": "P2", "warning": "P3", "info": "P4"}

    payload = {
        "message": title,
        "alias": alias or incident_id or title[:512],
        "description": body,
        "priority": _prio.get(severity, "P2"),
        "tags": ["atlasos", service] if service else ["atlasos"],
        "details": {"service": service, "incident_id": incident_id},
    }
    url = _OG_URL_EU if _og_region() == "eu" else _OG_URL_US
    try:
        resp = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"GenieKey {key}"},
            timeout=10,
        )
        if resp.status_code in (200, 201, 202):
            log.info("opsgenie.page_sent", title=title, severity=severity)
            return True
        log.warning("opsgenie.page_failed", status=resp.status_code, body=resp.text[:200])
        return False
    except Exception as exc:
        log.warning("opsgenie.page_error", error=str(exc))
        return False


def resolve_opsgenie(alias: str) -> bool:
    """Close an OpsGenie alert by alias."""
    key = _og_key()
    if not key:
        return False
    import urllib.parse
    url = (_OG_URL_EU if _og_region() == "eu" else _OG_URL_US)
    url = f"{url}/{urllib.parse.quote(alias, safe='')}/close"
    try:
        resp = httpx.post(
            url,
            json={"note": "Resolved by AtlasOS daemon"},
            headers={"Authorization": f"GenieKey {key}"},
            timeout=10,
        )
        return resp.status_code in (200, 202)
    except Exception as exc:
        log.warning("opsgenie.resolve_error", error=str(exc))
        return False


def page_oncall(
    title: str,
    body: str,
    severity: Severity = "critical",
    service: str = "",
    incident_id: str = "",
) -> dict:
    """Page on-call via whichever provider is configured.

    Tries PagerDuty first (if key present), then OpsGenie.
    Returns {"provider": str, "success": bool, "skipped": bool}.
    """
    pd_key = _pd_key()
    og_key = _og_key()

    if not pd_key and not og_key:
        log.warning("oncall.no_provider_configured")
        return {"provider": "none", "success": False, "skipped": True}

    if pd_key:
        ok = page_pagerduty(title, body, severity, service, incident_id)
        return {"provider": "pagerduty", "success": ok, "skipped": False}

    ok = page_opsgenie(title, body, severity, service, incident_id)
    return {"provider": "opsgenie", "success": ok, "skipped": False}


def resolve_oncall(incident_id: str) -> bool:
    """Resolve alert in whichever provider is configured."""
    if _pd_key():
        return resolve_pagerduty(incident_id)
    if _og_key():
        return resolve_opsgenie(incident_id)
    return False
