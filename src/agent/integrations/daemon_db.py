"""Daemon action log and state database.

SQLite persistence for the autonomous healing daemon.

Tables:
  daemon_actions    — audit log of every autonomous action taken
  daemon_cooldowns  — per-resource cooldown + fix-count tracking
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DB_PATH = Path("data/daemon.db")
_lock    = threading.Lock()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS daemon_actions (
    id          TEXT PRIMARY KEY,
    timestamp   TEXT NOT NULL,
    category    TEXT NOT NULL,
    action      TEXT NOT NULL,
    resource    TEXT NOT NULL,
    namespace   TEXT NOT NULL DEFAULT '',
    before_state TEXT,
    after_state  TEXT,
    success     INTEGER NOT NULL DEFAULT 1,
    auto_fixed  INTEGER NOT NULL DEFAULT 1,
    savings     REAL DEFAULT 0,
    note        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS daemon_cooldowns (
    resource_key TEXT PRIMARY KEY,
    last_action  TEXT NOT NULL,
    action_count INTEGER DEFAULT 1
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


def _to_json(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except Exception:
        return str(value)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def log_action(
    category: str,
    action: str,
    resource: str,
    namespace: str = "",
    before_state=None,
    after_state=None,
    success: bool = True,
    auto_fixed: bool = True,
    savings: float = 0.0,
    note: str = "",
) -> str:
    """Insert a daemon action row. Returns the generated id."""
    action_id = uuid.uuid4().hex
    with _lock:
        con = _conn()
        con.execute(
            """
            INSERT INTO daemon_actions
            (id, timestamp, category, action, resource, namespace,
             before_state, after_state, success, auto_fixed, savings, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                action_id, _now(), category, action, resource, namespace,
                _to_json(before_state), _to_json(after_state),
                1 if success else 0, 1 if auto_fixed else 0,
                float(savings or 0.0), note,
            ),
        )
        con.commit()
        con.close()
    return action_id


def list_actions(
    limit: int = 50,
    category: str | None = None,
    since_hours: int = 24,
) -> list[dict]:
    """Return recent actions, newest first. Filters by category and time window."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=since_hours)
    ).isoformat()
    con = _conn()
    if category:
        rows = con.execute(
            """
            SELECT * FROM daemon_actions
            WHERE timestamp >= ? AND category = ?
            ORDER BY timestamp DESC LIMIT ?
            """,
            (cutoff, category, limit),
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT * FROM daemon_actions
            WHERE timestamp >= ?
            ORDER BY timestamp DESC LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_action_count_today() -> int:
    """Count actions taken since local midnight today."""
    midnight = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc).isoformat()
    con = _conn()
    row = con.execute(
        "SELECT COUNT(*) AS n FROM daemon_actions WHERE timestamp >= ?",
        (midnight,),
    ).fetchone()
    con.close()
    return int(row["n"]) if row else 0


def get_total_savings() -> float:
    """Sum of savings across all logged actions."""
    con = _conn()
    row = con.execute(
        "SELECT COALESCE(SUM(savings), 0) AS total FROM daemon_actions"
    ).fetchone()
    con.close()
    return round(float(row["total"]) if row else 0.0, 2)


# ---------------------------------------------------------------------------
# Cooldowns
# ---------------------------------------------------------------------------

def check_cooldown(resource_key: str, cooldown_minutes: int = 30) -> bool:
    """Return True if the resource is still within its cooldown window."""
    con = _conn()
    row = con.execute(
        "SELECT last_action FROM daemon_cooldowns WHERE resource_key = ?",
        (resource_key,),
    ).fetchone()
    con.close()
    if not row:
        return False
    try:
        last = datetime.fromisoformat(row["last_action"])
    except Exception:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - last
    return elapsed < timedelta(minutes=cooldown_minutes)


def set_cooldown(resource_key: str) -> None:
    """Upsert the cooldown timestamp and increment the per-resource fix count."""
    with _lock:
        con = _conn()
        con.execute(
            """
            INSERT INTO daemon_cooldowns (resource_key, last_action, action_count)
            VALUES (?, ?, 1)
            ON CONFLICT(resource_key) DO UPDATE SET
                last_action = excluded.last_action,
                action_count = daemon_cooldowns.action_count + 1
            """,
            (resource_key, _now()),
        )
        con.commit()
        con.close()


def get_fix_count(resource_key: str) -> int:
    """How many times this resource has been auto-fixed."""
    con = _conn()
    row = con.execute(
        "SELECT action_count FROM daemon_cooldowns WHERE resource_key = ?",
        (resource_key,),
    ).fetchone()
    con.close()
    return int(row["action_count"]) if row else 0
