"""
Notification inbox — every alert AtlasOS raises, kept locally so the
dashboard's bell shows it whether or not Slack is configured.

Recorded at the senders themselves (integrations/slack.py, incident
alerts, on-call pages), before the "is Slack set up?" check — so turning
Slack off doesn't make alerts disappear, it just makes the dashboard the
only place they go.

record() never raises: an inbox problem must never stop an alert from
being sent. The store keeps the newest MAX_ROWS.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path("data/notifications.db")
_lock = threading.Lock()
MAX_ROWS = 500

_DDL = """
CREATE TABLE IF NOT EXISTS notifications (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    severity   TEXT NOT NULL,          -- critical | warning | info | ok
    kind       TEXT NOT NULL,          -- alert | resolved | approval | summary | page | incident
    title      TEXT NOT NULL,
    message    TEXT NOT NULL DEFAULT '',
    meta       TEXT NOT NULL DEFAULT '{}',
    read       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notif_created ON notifications(created_at);
"""


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript(_DDL)
    return con


def record(title: str, message: str = "", severity: str = "info", kind: str = "alert",
           meta: dict | None = None) -> str | None:
    """Add one notification. Returns its id, or None if it couldn't be stored."""
    try:
        nid = uuid.uuid4().hex[:12]
        sev = severity if severity in ("critical", "warning", "info", "ok") else "info"
        with _lock:
            con = _conn()
            con.execute(
                "INSERT INTO notifications (id, created_at, severity, kind, title, message, meta) VALUES (?,?,?,?,?,?,?)",
                (nid, datetime.now(timezone.utc).isoformat(), sev, kind, str(title)[:300],
                 str(message or "")[:4000], json.dumps(meta or {}, default=str)[:4000]),
            )
            con.execute(
                "DELETE FROM notifications WHERE id NOT IN "
                "(SELECT id FROM notifications ORDER BY created_at DESC LIMIT ?)", (MAX_ROWS,))
            con.commit()
            con.close()
        return nid
    except Exception:
        return None


def list_recent(limit: int = 50) -> dict:
    con = _conn()
    rows = con.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    unread = con.execute("SELECT COUNT(*) FROM notifications WHERE read = 0").fetchone()[0]
    con.close()
    items = []
    for r in rows:
        d = dict(r)
        d["read"] = bool(d["read"])
        try:
            d["meta"] = json.loads(d["meta"])
        except ValueError:
            d["meta"] = {}
        items.append(d)
    return {"unread": int(unread), "items": items}


def mark_read(ids: list[str] | None = None) -> int:
    """Mark the given ids read, or everything when ids is None."""
    with _lock:
        con = _conn()
        if ids is None:
            cur = con.execute("UPDATE notifications SET read = 1 WHERE read = 0")
        else:
            cur = con.executemany("UPDATE notifications SET read = 1 WHERE id = ?", [(i,) for i in ids])
        con.commit()
        n = cur.rowcount
        con.close()
    return n


def title_from_blocks(blocks: list[dict]) -> str:
    """Best-effort headline for a raw Block Kit message (e.g. the daily summary)."""
    for b in blocks or []:
        text = b.get("text")
        if isinstance(text, dict) and text.get("text"):
            return str(text["text"]).strip("* ")[:200]
    return "AtlasOS message"
