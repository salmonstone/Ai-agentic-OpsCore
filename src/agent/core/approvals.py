"""
Generic pending-action store for Slack-gated write operations.

deploy_db.py already has this pattern for deploys specifically (propose,
send to Slack, wait for a button, apply) and is left exactly as it is — this
is the same idea generalized for the six `*_apply_fix` MCP tools (Jenkins,
Kubernetes, ingress, TLS, AWS, cost), so any of them can be proposed instead
of requiring someone at the CLI to type `y`.

One row per proposal: what kind of fix, in plain English what it will do,
the exact parameters needed to run it, and a status. `execute()` in
`agent.integrations.webhook` looks the kind up in a small registry and calls
the same MCP tool function every other caller (ChatGPT, the CLI) uses — the
Slack button is a third caller of the one shared path, not a new one.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DB_PATH = Path("data/approvals.db")
_lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS pending_actions (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    summary      TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    decided_at   TEXT,
    decided_by   TEXT,
    result_json  TEXT
);
"""

DEFAULT_TTL_MINUTES = 45


@dataclass
class PendingAction:
    id: str
    kind: str
    summary: str
    params: dict
    status: str = "pending"          # pending | approved | rejected | expired | applied | failed
    created_at: str = ""
    expires_at: str = ""
    decided_at: str | None = None
    decided_by: str | None = None
    result: dict | None = None


def _conn() -> sqlite3.Connection:
    from agent.integrations.sqlite_conn import connect
    return connect(_DB_PATH, _DDL)


def _row_to_action(row: sqlite3.Row) -> PendingAction:
    return PendingAction(
        id=row["id"], kind=row["kind"], summary=row["summary"],
        params=json.loads(row["params_json"]), status=row["status"],
        created_at=row["created_at"], expires_at=row["expires_at"],
        decided_at=row["decided_at"], decided_by=row["decided_by"],
        result=json.loads(row["result_json"]) if row["result_json"] else None,
    )


def create(kind: str, summary: str, params: dict, ttl_minutes: int = DEFAULT_TTL_MINUTES) -> PendingAction:
    now = datetime.now(timezone.utc)
    action = PendingAction(
        id=uuid.uuid4().hex[:12], kind=kind, summary=summary, params=params,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=ttl_minutes)).isoformat(),
    )
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO pending_actions (id, kind, summary, params_json, status, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (action.id, action.kind, action.summary, json.dumps(params),
             action.status, action.created_at, action.expires_at),
        )
        con.commit()
        con.close()
    return action


def get(action_id: str) -> PendingAction | None:
    con = _conn()
    row = con.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
    con.close()
    return _row_to_action(row) if row else None


def list_actions(status: str | None = None, limit: int = 20) -> list[PendingAction]:
    con = _conn()
    if status:
        rows = con.execute(
            "SELECT * FROM pending_actions WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM pending_actions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    con.close()
    return [_row_to_action(r) for r in rows]


def is_expired(action: PendingAction, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(action.expires_at)
    except ValueError:
        return False
    return now >= expires


def decide(action_id: str, status: str, decided_by: str, result: dict | None = None) -> None:
    """Record approved/rejected/expired/applied/failed plus who and the result."""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE pending_actions SET status=?, decided_at=?, decided_by=?, result_json=? WHERE id=?",
            (status, datetime.now(timezone.utc).isoformat(), decided_by,
             json.dumps(result) if result is not None else None, action_id),
        )
        con.commit()
        con.close()


def expire_stale() -> int:
    """Mark any pending row past its expiry as expired. Returns count changed."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        con = _conn()
        cur = con.execute(
            "UPDATE pending_actions SET status='expired' WHERE status='pending' AND expires_at < ?",
            (now,),
        )
        count = cur.rowcount
        con.commit()
        con.close()
    return count
