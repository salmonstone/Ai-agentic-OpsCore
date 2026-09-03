"""
Durable SQLite-backed event queue.

Every action in AtlasOS goes through this queue:
  enqueue()  →  worker picks it up  →  skill executes  →  complete() / fail()

If the worker crashes mid-execution, the event stays in "processing" state.
On restart, any event stuck in "processing" for > 5 minutes is reset to "pending"
so another worker can pick it up.

Dead-letter: events that exceed max_retries move to status="dead" and are
Slack-alerted. Use retry_dead(id) to manually requeue them.

Priority levels:
  1 = critical  (pod crash, OOM, bad deploy)
  2 = high      (deploy push, security alert)
  3 = normal    (cost scan, runbook)
  4 = low       (informational alerts, stats)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sqlite_utils

from agent.observability.logging import get_logger

log = get_logger(__name__)

_DB_PATH       = "data/event_queue.db"
_TABLE         = "events"
_STUCK_MINUTES = 5    # events in "processing" longer than this → reset to pending


# ---------------------------------------------------------------------------
# DB bootstrap
# ---------------------------------------------------------------------------

def _db() -> sqlite_utils.Database:
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite_utils.Database(_DB_PATH)
    db.conn.isolation_level = None   # autocommit — every execute() commits immediately
    _ensure_schema(db)
    return db


def _ensure_schema(db: sqlite_utils.Database) -> None:
    if _TABLE in db.table_names():
        return
    db[_TABLE].create({
        "id":           str,
        "event_type":   str,
        "payload":      str,    # JSON blob
        "status":       str,    # pending / processing / done / failed / dead
        "priority":     int,    # 1=critical … 4=low
        "retry_count":  int,
        "max_retries":  int,
        "created_at":   str,    # ISO-8601 UTC
        "updated_at":   str,
        "scheduled_at": str,    # earliest time to process
        "worker_id":    str,
        "error":        str,
        "result":       str,    # JSON blob
    }, pk="id")
    db[_TABLE].create_index(["status", "priority", "scheduled_at"], if_not_exists=True)
    log.info("event_queue.schema_created")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cols(db: sqlite_utils.Database) -> list[str]:
    return [r[1] for r in db.execute(f"PRAGMA table_info({_TABLE})").fetchall()]


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

def enqueue(
    event_type: str,
    payload: dict[str, Any],
    priority: int = 3,
    max_retries: int = 3,
    delay_seconds: int = 0,
) -> str:
    """
    Add an event to the queue. Returns the event ID.

    priority:      1=critical, 2=high, 3=normal, 4=low
    delay_seconds: don't process before now + delay_seconds
    """
    db = _db()
    event_id  = str(uuid.uuid4())
    now       = datetime.now(timezone.utc)
    scheduled = (now + timedelta(seconds=delay_seconds)).isoformat()

    db[_TABLE].insert({
        "id":           event_id,
        "event_type":   event_type,
        "payload":      json.dumps(payload),
        "status":       "pending",
        "priority":     priority,
        "retry_count":  0,
        "max_retries":  max_retries,
        "created_at":   now.isoformat(),
        "updated_at":   now.isoformat(),
        "scheduled_at": scheduled,
        "worker_id":    "",
        "error":        "",
        "result":       "",
    })
    log.info("event_queue.enqueued",
             id=event_id, event_type=event_type, priority=priority)
    return event_id


def dequeue(worker_id: str) -> dict | None:
    """
    Atomically claim the next due pending event.
    Returns a fully decoded event dict, or None if the queue is empty.

    Also resets any events stuck in "processing" (worker crash recovery).
    """
    db  = _db()
    now = _now()

    # Reset stuck events (worker died mid-execution)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_STUCK_MINUTES)).isoformat()
    db.execute(
        f"UPDATE {_TABLE} "
        f"SET status='pending', worker_id='', updated_at=? "
        f"WHERE status='processing' AND updated_at < ?",
        [now, cutoff],
    )

    # Claim: highest priority, oldest, due now
    row = db.execute(
        f"SELECT * FROM {_TABLE} "
        f"WHERE status='pending' AND scheduled_at <= ? "
        f"ORDER BY priority ASC, created_at ASC LIMIT 1",
        [now],
    ).fetchone()

    if not row:
        return None

    event = dict(zip(_cols(db), row))

    db.execute(
        f"UPDATE {_TABLE} SET status='processing', worker_id=?, updated_at=? WHERE id=?",
        [worker_id, now, event["id"]],
    )

    event["payload"] = json.loads(event.get("payload") or "{}")
    log.info("event_queue.dequeued",
             id=event["id"], event_type=event["event_type"], worker=worker_id)
    return event


def complete(event_id: str, result: dict | None = None) -> None:
    """Mark an event as successfully processed."""
    _db().execute(
        f"UPDATE {_TABLE} SET status='done', result=?, updated_at=? WHERE id=?",
        [json.dumps(result or {}), _now(), event_id],
    )
    log.info("event_queue.completed", id=event_id)


def fail(event_id: str, error: str, retry: bool = True) -> None:
    """
    Record a failure. Schedules a retry with exponential backoff, or moves
    to dead-letter when retries are exhausted.

    Backoff schedule: attempt 1→30s, attempt 2→2min, attempt 3→8min.
    """
    db  = _db()
    row = db.execute(
        f"SELECT retry_count, max_retries, event_type FROM {_TABLE} WHERE id=?",
        [event_id],
    ).fetchone()
    if not row:
        return

    retry_count, max_retries, event_type = row
    new_count = retry_count + 1
    now       = _now()

    if retry and new_count < max_retries:
        delay     = 30 * (4 ** retry_count)   # 30s, 120s, 480s
        scheduled = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        db.execute(
            f"UPDATE {_TABLE} "
            f"SET status='pending', retry_count=?, error=?, updated_at=?, scheduled_at=? "
            f"WHERE id=?",
            [new_count, error[:500], now, scheduled, event_id],
        )
        log.warning("event_queue.retry_scheduled",
                    id=event_id, attempt=new_count, delay_s=delay)
    else:
        db.execute(
            f"UPDATE {_TABLE} "
            f"SET status='dead', retry_count=?, error=?, updated_at=? WHERE id=?",
            [new_count, error[:500], now, event_id],
        )
        log.error("event_queue.dead_letter",
                  id=event_id, event_type=event_type, attempts=new_count)
        try:
            from agent.integrations.slack import send_alert_generic
            send_alert_generic(
                title=f"Dead-letter: {event_type}",
                message=(
                    f"Event `{event_id[:8]}` failed {new_count} time(s) and "
                    f"moved to dead-letter.\nLast error: {error[:300]}"
                ),
                severity="critical",
                fields={
                    "Event ID":   event_id[:8],
                    "Type":       event_type,
                    "Attempts":   str(new_count),
                },
            )
        except Exception:
            pass


def retry_dead(event_id: str) -> bool:
    """Move a dead-letter event back to pending for a manual retry."""
    now = _now()
    _db().execute(
        f"UPDATE {_TABLE} "
        f"SET status='pending', retry_count=0, error='', scheduled_at=?, updated_at=? "
        f"WHERE id=? AND status='dead'",
        [now, now, event_id],
    )
    log.info("event_queue.manual_retry", id=event_id)
    return True


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------

def list_events(status: str | None = None, limit: int = 50) -> list[dict]:
    """Return events newest-first, optionally filtered by status."""
    db = _db()
    if status:
        rows = db.execute(
            f"SELECT * FROM {_TABLE} WHERE status=? ORDER BY created_at DESC LIMIT ?",
            [status, limit],
        ).fetchall()
    else:
        rows = db.execute(
            f"SELECT * FROM {_TABLE} ORDER BY created_at DESC LIMIT ?",
            [limit],
        ).fetchall()

    cols   = _cols(db)
    events = []
    for row in rows:
        e = dict(zip(cols, row))
        try:
            e["payload"] = json.loads(e.get("payload") or "{}")
        except Exception:
            pass
        events.append(e)
    return events


def get_stats() -> dict:
    """Return event counts by status and a total."""
    db   = _db()
    rows = db.execute(
        f"SELECT status, COUNT(*) FROM {_TABLE} GROUP BY status"
    ).fetchall()
    stats: dict[str, int] = {r[0]: r[1] for r in rows}
    for s in ("pending", "processing", "done", "failed", "dead"):
        stats.setdefault(s, 0)
    stats["total"] = sum(v for k, v in stats.items() if k != "total")
    return stats


def get_event(event_id: str) -> dict | None:
    """Fetch a single event by ID."""
    db  = _db()
    row = db.execute(
        f"SELECT * FROM {_TABLE} WHERE id=?", [event_id]
    ).fetchone()
    if not row:
        return None
    e = dict(zip(_cols(db), row))
    try:
        e["payload"] = json.loads(e.get("payload") or "{}")
        e["result"]  = json.loads(e.get("result")  or "{}")
    except Exception:
        pass
    return e
