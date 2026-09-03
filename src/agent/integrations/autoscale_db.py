"""SQLite persistence for auto-scaling policies and events.

Tables:
  scale_policies — user-defined scaling policies
  scale_events   — history of all scale actions taken
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path("data/autoscale.db")
_lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS scale_policies (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    namespace   TEXT NOT NULL DEFAULT 'default',
    deployment  TEXT NOT NULL,
    min_replicas INTEGER NOT NULL DEFAULT 1,
    max_replicas INTEGER NOT NULL DEFAULT 10,
    schedule_down_utc TEXT,    -- cron-like "HH:MM" UTC time to scale down
    schedule_up_utc   TEXT,    -- cron-like "HH:MM" UTC time to scale up
    down_replicas INTEGER DEFAULT 1,
    up_replicas   INTEGER DEFAULT 3,
    cpu_threshold_pct REAL DEFAULT 80.0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scale_events (
    id          TEXT PRIMARY KEY,
    policy_id   TEXT,
    deployment  TEXT NOT NULL,
    namespace   TEXT NOT NULL,
    action      TEXT NOT NULL,   -- schedule_down / schedule_up / cpu_scale_up / manual
    old_replicas INTEGER,
    new_replicas INTEGER,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending / done / failed
    created_at  TEXT NOT NULL
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


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

def add_policy(
    deployment: str,
    namespace: str,
    name: str,
    min_r: int = 1,
    max_r: int = 10,
    down_utc: str | None = "17:30",
    up_utc: str | None = "03:30",
    down_r: int = 1,
    up_r: int = 3,
    cpu_pct: float = 80.0,
) -> str:
    """Insert a new scaling policy. Returns policy id."""
    policy_id = uuid.uuid4().hex
    now = _now()
    with _lock:
        con = _conn()
        con.execute(
            """
            INSERT INTO scale_policies
            (id, name, namespace, deployment, min_replicas, max_replicas,
             schedule_down_utc, schedule_up_utc, down_replicas, up_replicas,
             cpu_threshold_pct, enabled, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                policy_id, name, namespace, deployment, min_r, max_r,
                down_utc, up_utc, down_r, up_r, cpu_pct, 1, now, now,
            ),
        )
        con.commit()
        con.close()
    return policy_id


def list_policies(enabled_only: bool = True) -> list[dict]:
    """Return all scaling policies (enabled only by default)."""
    con = _conn()
    if enabled_only:
        rows = con.execute(
            "SELECT * FROM scale_policies WHERE enabled = 1 ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM scale_policies ORDER BY created_at DESC"
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_policy(policy_id: str) -> dict | None:
    con = _conn()
    row = con.execute(
        "SELECT * FROM scale_policies WHERE id = ?", (policy_id,)
    ).fetchone()
    con.close()
    return dict(row) if row else None


def disable_policy(policy_id: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE scale_policies SET enabled = 0, updated_at = ? WHERE id = ?",
            (_now(), policy_id),
        )
        con.commit()
        con.close()


def enable_policy(policy_id: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE scale_policies SET enabled = 1, updated_at = ? WHERE id = ?",
            (_now(), policy_id),
        )
        con.commit()
        con.close()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def log_event(
    policy_id: str | None,
    deployment: str,
    namespace: str,
    action: str,
    old_r: int | None,
    new_r: int | None,
    reason: str = "",
    status: str = "done",
) -> str:
    """Record a scale action. Returns event id."""
    event_id = uuid.uuid4().hex
    with _lock:
        con = _conn()
        con.execute(
            """
            INSERT INTO scale_events
            (id, policy_id, deployment, namespace, action, old_replicas,
             new_replicas, reason, status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id, policy_id, deployment, namespace, action,
                old_r, new_r, reason, status, _now(),
            ),
        )
        con.commit()
        con.close()
    return event_id


def get_events(
    deployment: str | None = None,
    namespace: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return scale events, newest first. Optionally filtered."""
    con = _conn()
    clauses: list[str] = []
    params: list = []
    if deployment:
        clauses.append("deployment = ?")
        params.append(deployment)
    if namespace:
        clauses.append("namespace = ?")
        params.append(namespace)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = con.execute(
        f"SELECT * FROM scale_events {where} ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """Return summary stats.

    replicas_saved = sum of (old_r - new_r) for schedule_down events today.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con = _conn()

    total_policies = con.execute(
        "SELECT COUNT(*) AS c FROM scale_policies"
    ).fetchone()["c"]

    events_today = con.execute(
        "SELECT COUNT(*) AS c FROM scale_events WHERE created_at LIKE ?",
        (f"{today}%",),
    ).fetchone()["c"]

    saved_row = con.execute(
        """
        SELECT SUM(old_replicas - new_replicas) AS saved
        FROM scale_events
        WHERE action = 'schedule_down'
          AND status = 'done'
          AND created_at LIKE ?
          AND old_replicas IS NOT NULL
          AND new_replicas IS NOT NULL
        """,
        (f"{today}%",),
    ).fetchone()
    con.close()

    replicas_saved = int(saved_row["saved"] or 0)
    return {
        "total_policies": int(total_policies or 0),
        "events_today": int(events_today or 0),
        "replicas_saved": replicas_saved,
    }
