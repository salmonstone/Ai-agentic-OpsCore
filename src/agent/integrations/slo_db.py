"""SLO and Error Budget tracking database.

Tables:
  slos       - one row per service-level objective
  slo_burns  - individual downtime (budget burn) events per SLO
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DB_PATH = Path("data/slo.db")
_lock    = threading.Lock()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS slos (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    service         TEXT NOT NULL,
    namespace       TEXT NOT NULL DEFAULT '',
    target_pct      REAL NOT NULL DEFAULT 99.9,
    window_days     INTEGER NOT NULL DEFAULT 30,
    created_at      TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS slo_burns (
    id          TEXT PRIMARY KEY,
    slo_id      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    duration_sec INTEGER DEFAULT 0,
    cause       TEXT DEFAULT '',
    incident_id TEXT DEFAULT '',
    FOREIGN KEY (slo_id) REFERENCES slos(id)
);
"""


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(_DDL)
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


# ---------------------------------------------------------------------------
# SLOs
# ---------------------------------------------------------------------------

def create_slo(
    name: str,
    service: str,
    namespace: str,
    target_pct: float = 99.9,
    window_days: int = 30,
) -> str:
    """Create a new SLO. Returns id."""
    slo_id = uuid.uuid4().hex
    with _lock:
        con = _conn()
        con.execute(
            """
            INSERT INTO slos
            (id, name, service, namespace, target_pct, window_days, created_at, active)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (slo_id, name, service, namespace, target_pct, window_days, _now(), 1),
        )
        con.commit()
        con.close()
    return slo_id


def list_slos(active_only: bool = True) -> list[dict]:
    """List all SLOs, newest first."""
    con = _conn()
    if active_only:
        rows = con.execute(
            "SELECT * FROM slos WHERE active = 1 ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM slos ORDER BY created_at DESC"
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_slo(slo_id: str) -> dict | None:
    """Get single SLO."""
    con = _conn()
    row = con.execute("SELECT * FROM slos WHERE id = ?", (slo_id,)).fetchone()
    con.close()
    return dict(row) if row else None


def get_slo_by_service(service: str, namespace: str) -> dict | None:
    """Find active SLO for a service."""
    con = _conn()
    row = con.execute(
        """
        SELECT * FROM slos
        WHERE active = 1 AND service = ? AND namespace = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (service, namespace),
    ).fetchone()
    con.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Burns
# ---------------------------------------------------------------------------

def record_burn(slo_id: str, cause: str = "", incident_id: str = "") -> str:
    """Start recording downtime. Returns burn_id."""
    burn_id = uuid.uuid4().hex
    with _lock:
        con = _conn()
        con.execute(
            """
            INSERT INTO slo_burns
            (id, slo_id, started_at, ended_at, duration_sec, cause, incident_id)
            VALUES (?,?,?,?,?,?,?)
            """,
            (burn_id, slo_id, _now(), None, 0, cause, incident_id),
        )
        con.commit()
        con.close()
    return burn_id


def end_burn(burn_id: str, duration_sec: int) -> None:
    """End a downtime period."""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE slo_burns SET ended_at = ?, duration_sec = ? WHERE id = ?",
            (_now(), int(duration_sec), burn_id),
        )
        con.commit()
        con.close()


def get_budget_status(slo_id: str) -> dict:
    """
    Calculate current error budget for an SLO.

    Returns a dict with target/window, total/allowed/used/remaining minutes,
    budget_pct_remaining (0-100), a status string and the list of burn events
    counted within the rolling window.
    """
    slo = get_slo(slo_id)
    if slo is None:
        return {
            "slo_id": slo_id,
            "service": "",
            "namespace": "",
            "name": "",
            "target_pct": 0.0,
            "window_days": 0,
            "total_minutes": 0.0,
            "allowed_downtime_min": 0.0,
            "used_downtime_min": 0.0,
            "remaining_min": 0.0,
            "budget_pct_remaining": 0.0,
            "status": "exhausted",
            "burns": [],
        }

    target_pct  = float(slo["target_pct"])
    window_days = int(slo["window_days"])
    service     = slo["service"]

    # total_minutes = window_days * 24 * 60
    # allowed       = total_minutes * (1 - target_pct/100)
    total_minutes        = window_days * 24 * 60
    allowed_downtime_min = total_minutes * (1 - target_pct / 100.0)

    # Window filtering: only count burns started within the last window_days.
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    con = _conn()
    rows = con.execute(
        "SELECT * FROM slo_burns WHERE slo_id = ? ORDER BY started_at ASC",
        (slo_id,),
    ).fetchall()
    con.close()

    burns: list[dict] = []
    used_sec = 0
    for r in rows:
        started = _parse_ts(r["started_at"])
        if started is None or started < cutoff:
            continue
        burns.append(dict(r))
        used_sec += int(r["duration_sec"] or 0)

    used_downtime_min = used_sec / 60.0
    remaining_min     = allowed_downtime_min - used_downtime_min

    if allowed_downtime_min > 0:
        budget_pct_remaining = (remaining_min / allowed_downtime_min) * 100.0
    else:
        # A 100% target leaves zero budget - exhausted on any downtime.
        budget_pct_remaining = 100.0 if used_downtime_min == 0 else 0.0

    # Clamp to a sane floor so an over-burn does not report below zero.
    if budget_pct_remaining < 0:
        budget_pct_remaining = 0.0

    if budget_pct_remaining > 50:
        status = "healthy"
    elif budget_pct_remaining > 20:
        status = "warning"    # >50% burned
    elif budget_pct_remaining > 0:
        status = "critical"   # >80% burned
    else:
        status = "exhausted"

    return {
        "slo_id": slo_id,
        "service": service,
        "namespace": slo["namespace"],
        "name": slo["name"],
        "target_pct": target_pct,
        "window_days": window_days,
        "total_minutes": float(total_minutes),
        "allowed_downtime_min": round(allowed_downtime_min, 4),
        "used_downtime_min": round(used_downtime_min, 4),
        "remaining_min": round(remaining_min, 4),
        "budget_pct_remaining": round(budget_pct_remaining, 4),
        "status": status,
        "burns": burns,
    }


def is_deploy_blocked(service: str, namespace: str) -> bool:
    """Return True if error budget < 20% remaining (auto-block deploys)."""
    slo = get_slo_by_service(service, namespace)
    if slo is None:
        return False
    status = get_budget_status(slo["id"])
    return status["budget_pct_remaining"] < 20.0


def delete_slo(slo_id: str) -> bool:
    """Soft-delete (set active=0). Returns True if found."""
    with _lock:
        con = _conn()
        cur = con.execute(
            "UPDATE slos SET active = 0 WHERE id = ? AND active = 1",
            (slo_id,),
        )
        found = cur.rowcount > 0
        con.commit()
        con.close()
    return found
