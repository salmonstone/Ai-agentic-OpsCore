"""
Raw Prometheus HTTP API client. No AI. No business logic.

Mirrors how kubectl.py wraps kubectl and jenkins.py wraps the Jenkins REST
API — every function here does exactly one HTTP call and returns a typed
model or a plain value, and none of them raise: a Prometheus that is down,
unreachable or unconfigured must degrade the caller's answer, never crash a
diagnosis that was otherwise going fine.

This is the project's first real time-series source. Everything before it
(`kubectl top`, node_inspector) returned a single instantaneous reading,
which is enough for "is it hot right now" and useless for "has it been
climbing for six hours" — the shape that actually distinguishes a memory
leak from a busy afternoon.

Auth: optional bearer token (PROMETHEUS_TOKEN) or basic auth
(PROMETHEUS_USER / PROMETHEUS_PASSWORD). A Thanos/Cortex/Mimir query
endpoint speaks the same API and works unchanged.
"""
from __future__ import annotations

import time

import httpx

from agent.config import settings
from agent.core.models import MetricPoint, MetricSeries, MetricSource
from agent.observability.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# Client plumbing
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return (settings.prometheus_url or "").rstrip("/")


def is_configured() -> bool:
    """True when a Prometheus URL is set. Callers branch on this rather than
    catching an exception, so 'not configured' stays a normal state."""
    return bool(_base_url())


def _client() -> httpx.Client:
    headers = {}
    token = settings.prometheus_token
    if token:
        headers["Authorization"] = f"Bearer {token}"

    auth = None
    if settings.prometheus_user and settings.prometheus_password:
        auth = (settings.prometheus_user, settings.prometheus_password)

    return httpx.Client(
        headers=headers,
        auth=auth,
        verify=settings.prometheus_verify_ssl,
        timeout=settings.prometheus_timeout or _TIMEOUT,
    )


def _get(path: str, params: dict) -> dict | None:
    """One GET against the Prometheus API. Returns the decoded `data` block,
    or None on any failure (logged, never raised)."""
    if not is_configured():
        log.debug("prometheus.not_configured")
        return None

    url = f"{_base_url()}{path}"
    t0 = time.perf_counter()
    try:
        with _client() as c:
            r = c.get(url, params=params)
            r.raise_for_status()
            body = r.json()
    except httpx.HTTPStatusError as e:
        log.warning("prometheus.http_error", path=path,
                    status=e.response.status_code, body=e.response.text[:200])
        return None
    except Exception as e:
        log.warning("prometheus.failed", path=path, error=str(e)[:200])
        return None

    if body.get("status") != "success":
        log.warning("prometheus.query_error", path=path,
                    error=str(body.get("error"))[:200])
        return None

    log.debug("prometheus.ok", path=path,
              duration_ms=round((time.perf_counter() - t0) * 1000, 1))
    return body.get("data") or {}


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------

def check_connection() -> dict:
    """Verify the endpoint answers. Shaped like jenkins.get_connection_info()
    and aws_auth_status so the CLI/MCP surface stays consistent."""
    if not is_configured():
        return {"connected": False, "url": "",
                "error": "PROMETHEUS_URL is not set. Run `agent setup` or add it to .env."}

    data = _get("/api/v1/query", {"query": "vector(1)"})
    if data is None:
        return {"connected": False, "url": _base_url(),
                "error": "Query endpoint did not answer — check URL, auth and network reachability."}

    build = _get("/api/v1/status/buildinfo", {}) or {}
    return {
        "connected": True,
        "url": _base_url(),
        "version": build.get("version", "unknown"),
        "auth": "bearer" if settings.prometheus_token
                else ("basic" if settings.prometheus_user else "none"),
    }


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _to_series(result: list[dict], name: str, unit: str, ranged: bool) -> list[MetricSeries]:
    """Normalize either a `matrix` (range) or `vector` (instant) result into
    MetricSeries. Prometheus values arrive as [ts, "string"] pairs."""
    out: list[MetricSeries] = []
    for item in result or []:
        labels = {k: str(v) for k, v in (item.get("metric") or {}).items()}
        raw = item.get("values") if ranged else [item.get("value")]
        points: list[MetricPoint] = []
        for pair in raw or []:
            if not pair or len(pair) != 2:
                continue
            try:
                points.append(MetricPoint(ts=float(pair[0]), value=float(pair[1])))
            except (TypeError, ValueError):
                # NaN / +Inf come back as strings Prometheus can emit for
                # rates over empty ranges — skip the point, keep the series.
                continue
        if points:
            out.append(MetricSeries(
                name=labels.get("__name__") or name,
                labels=labels, points=points, unit=unit,
                source=MetricSource.PROMETHEUS,
            ))
    return out


def query(promql: str, unit: str = "") -> list[MetricSeries]:
    """Instant query — one point per series, at 'now'."""
    data = _get("/api/v1/query", {"query": promql})
    if data is None:
        return []
    return _to_series(data.get("result", []), promql, unit, ranged=False)


def query_range(promql: str, minutes: int = 60, step: str = "",
                unit: str = "") -> list[MetricSeries]:
    """Range query over the last `minutes`.

    `step` defaults to roughly 120 points across the window, which keeps the
    payload small enough to hand to an LLM while leaving enough resolution
    for slope and standard-deviation math to mean something.
    """
    end = time.time()
    start = end - (minutes * 60)
    if not step:
        step = f"{max(15, int((minutes * 60) / 120))}s"

    data = _get("/api/v1/query_range", {
        "query": promql, "start": start, "end": end, "step": step,
    })
    if data is None:
        return []
    return _to_series(data.get("result", []), promql, unit, ranged=True)


def query_scalar(promql: str) -> float | None:
    """Single number out of an instant query — the common case for a golden
    signal like 'current error ratio'. None when there is no data, which is
    meaningfully different from 0.0 and must not be flattened into it."""
    series = query(promql)
    if not series:
        return None
    return series[0].latest()


def label_values(label: str, matcher: str = "") -> list[str]:
    """Distinct values for a label, e.g. every `namespace` or `job`."""
    params = {}
    if matcher:
        params["match[]"] = matcher
    data = _get(f"/api/v1/label/{label}/values", params)
    if data is None:
        return []
    return [str(v) for v in (data if isinstance(data, list) else [])]


def active_alerts() -> list[dict]:
    """Currently firing Prometheus alerts — the cheapest high-signal evidence
    source in the whole system, because a human already decided each one is
    worth waking up for."""
    data = _get("/api/v1/alerts", {})
    if data is None:
        return []
    out = []
    for a in data.get("alerts", []) or []:
        labels = a.get("labels") or {}
        out.append({
            "name":        labels.get("alertname", "?"),
            "severity":    labels.get("severity", ""),
            "namespace":   labels.get("namespace", ""),
            "pod":         labels.get("pod", ""),
            "service":     labels.get("service", ""),
            "state":       a.get("state", ""),
            "active_at":   a.get("activeAt", ""),
            "summary":     (a.get("annotations") or {}).get("summary", ""),
            "description": (a.get("annotations") or {}).get("description", ""),
        })
    return out


def targets_down() -> list[dict]:
    """Scrape targets currently failing — explains a flatlined metric before
    anyone wastes time diagnosing the workload it belongs to."""
    data = _get("/api/v1/targets", {"state": "active"})
    if data is None:
        return []
    out = []
    for t in data.get("activeTargets", []) or []:
        if t.get("health") == "down":
            labels = t.get("labels") or {}
            out.append({
                "job":        labels.get("job", "?"),
                "instance":   labels.get("instance", "?"),
                "namespace":  labels.get("namespace", ""),
                "last_error": t.get("lastError", "")[:200],
                "last_scrape": t.get("lastScrape", ""),
            })
    return out
