"""Incident tracking database.

Tables:
  incidents      — one row per incident
  incident_events — timeline of events within an incident
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DB_PATH = Path("data/incident.db")
_lock    = threading.Lock()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS incidents (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    severity     TEXT NOT NULL DEFAULT 'warning',
    status       TEXT NOT NULL DEFAULT 'open',
    service      TEXT NOT NULL DEFAULT '',
    namespace    TEXT NOT NULL DEFAULT '',
    opened_at    TEXT NOT NULL,
    resolved_at  TEXT,
    duration_sec INTEGER DEFAULT 0,
    cause        TEXT DEFAULT '',
    fix_attempts INTEGER DEFAULT 0,
    auto_fixed   INTEGER DEFAULT 0,
    slack_thread_ts TEXT DEFAULT '',
    notes        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS incident_events (
    id          TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    detail      TEXT DEFAULT '',
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
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
# Incidents
# ---------------------------------------------------------------------------

def open_incident(
    title: str,
    severity: str,
    service: str,
    namespace: str,
    cause: str = "",
) -> str:
    """Create new incident. Returns incident id."""
    incident_id = uuid.uuid4().hex
    with _lock:
        con = _conn()
        con.execute(
            """
            INSERT INTO incidents
            (id, title, severity, status, service, namespace, opened_at,
             resolved_at, duration_sec, cause, fix_attempts, auto_fixed,
             slack_thread_ts, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                incident_id, title, severity, "open", service, namespace,
                _now(), None, 0, cause, 0, 0, "", "",
            ),
        )
        con.commit()
        con.close()
    return incident_id


def add_event(incident_id: str, event_type: str, detail: str = "") -> None:
    """Add timeline event to an incident."""
    event_id = uuid.uuid4().hex
    with _lock:
        con = _conn()
        con.execute(
            """
            INSERT INTO incident_events
            (id, incident_id, timestamp, event_type, detail)
            VALUES (?,?,?,?,?)
            """,
            (event_id, incident_id, _now(), event_type, detail),
        )
        con.commit()
        con.close()


def resolve_incident(incident_id: str, auto_fixed: bool = False, note: str = "") -> None:
    """Mark incident resolved, calculate duration_sec."""
    with _lock:
        con = _conn()
        row = con.execute(
            "SELECT opened_at, status FROM incidents WHERE id = ?",
            (incident_id,),
        ).fetchone()
        if not row:
            con.close()
            return
        resolved_at = _now()
        opened = _parse_ts(row["opened_at"])
        duration_sec = 0
        if opened is not None:
            resolved = _parse_ts(resolved_at)
            duration_sec = int(max(0, (resolved - opened).total_seconds()))
        con.execute(
            """
            UPDATE incidents
            SET status = 'resolved', resolved_at = ?, duration_sec = ?,
                auto_fixed = ?, notes = ?
            WHERE id = ?
            """,
            (
                resolved_at, duration_sec, 1 if auto_fixed else 0,
                note, incident_id,
            ),
        )
        con.commit()
        con.close()


def get_incident(incident_id: str) -> dict | None:
    """Get single incident with its events list."""
    con = _conn()
    row = con.execute(
        "SELECT * FROM incidents WHERE id = ?", (incident_id,)
    ).fetchone()
    if not row:
        con.close()
        return None
    events = con.execute(
        """
        SELECT * FROM incident_events
        WHERE incident_id = ?
        ORDER BY timestamp ASC
        """,
        (incident_id,),
    ).fetchall()
    con.close()
    incident = dict(row)
    incident["events"] = [dict(e) for e in events]
    return incident


def list_incidents(
    status: str | None = None,
    limit: int = 20,
    since_hours: int = 168,
) -> list[dict]:
    """List incidents, newest first. status=None means all."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=since_hours)
    ).isoformat()
    con = _conn()
    if status:
        rows = con.execute(
            """
            SELECT * FROM incidents
            WHERE opened_at >= ? AND status = ?
            ORDER BY opened_at DESC LIMIT ?
            """,
            (cutoff, status, limit),
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT * FROM incidents
            WHERE opened_at >= ?
            ORDER BY opened_at DESC LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_open_incident(service: str, namespace: str) -> dict | None:
    """Find existing open incident for this service/namespace (avoid duplicates)."""
    con = _conn()
    row = con.execute(
        """
        SELECT * FROM incidents
        WHERE status = 'open' AND service = ? AND namespace = ?
        ORDER BY opened_at DESC LIMIT 1
        """,
        (service, namespace),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def increment_fix_attempts(incident_id: str) -> None:
    """Bump fix_attempts counter."""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE incidents SET fix_attempts = fix_attempts + 1 WHERE id = ?",
            (incident_id,),
        )
        con.commit()
        con.close()


def set_slack_thread(incident_id: str, thread_ts: str) -> None:
    """Store Slack thread timestamp for follow-up replies."""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE incidents SET slack_thread_ts = ? WHERE id = ?",
            (thread_ts, incident_id),
        )
        con.commit()
        con.close()


def get_mttr_hours(days: int = 30) -> float:
    """Mean time to resolve: avg duration of resolved incidents in last N days."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()
    con = _conn()
    row = con.execute(
        """
        SELECT AVG(duration_sec) AS avg_sec FROM incidents
        WHERE status = 'resolved' AND opened_at >= ?
        """,
        (cutoff,),
    ).fetchone()
    con.close()
    if not row or row["avg_sec"] is None:
        return 0.0
    return round(float(row["avg_sec"]) / 3600.0, 4)


def get_stats(days: int = 30) -> dict:
    """Return: total, open, resolved, critical_count, avg_duration_sec, mttr_hours."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()
    con = _conn()
    row = con.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open,
            SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) AS resolved,
            SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) AS critical_count,
            SUM(CASE WHEN status = 'resolved' AND auto_fixed = 1 THEN 1 ELSE 0 END) AS auto_resolved,
            MAX(CASE WHEN status = 'resolved' THEN duration_sec ELSE 0 END) AS longest_sec
        FROM incidents
        WHERE opened_at >= ?
        """,
        (cutoff,),
    ).fetchone()
    avg_row = con.execute(
        """
        SELECT AVG(duration_sec) AS avg_sec FROM incidents
        WHERE status = 'resolved' AND opened_at >= ?
        """,
        (cutoff,),
    ).fetchone()
    con.close()

    total          = int(row["total"] or 0)
    open_count     = int(row["open"] or 0)
    resolved_count = int(row["resolved"] or 0)
    critical_count = int(row["critical_count"] or 0)
    auto_resolved  = int(row["auto_resolved"] or 0)
    longest_sec    = int(row["longest_sec"] or 0)
    avg_duration_sec = int(avg_row["avg_sec"]) if avg_row and avg_row["avg_sec"] is not None else 0

    return {
        "total":            total,
        "open":             open_count,
        "resolved":         resolved_count,
        "critical_count":   critical_count,
        "auto_resolved":    auto_resolved,
        "avg_duration_sec": avg_duration_sec,
        "mttr_hours":       round(avg_duration_sec / 3600.0, 4),
        "longest_sec":      longest_sec,
    }
