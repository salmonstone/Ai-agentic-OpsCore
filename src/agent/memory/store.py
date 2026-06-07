"""
SQLite persistence layer for agent memories.

Table: memories
  id            TEXT  PRIMARY KEY
  content       TEXT  NOT NULL
  source        TEXT  NOT NULL
  metadata_json TEXT  DEFAULT '{}'
  created_at    TEXT  NOT NULL  (ISO-8601 UTC)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import sqlite_utils

from agent.config import settings
from agent.core.models import Memory
from agent.observability.logging import get_logger

log = get_logger(__name__)

_TABLE = "memories"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _db() -> sqlite_utils.Database:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite_utils.Database(settings.db_path)
    _ensure_table(db)
    return db


def _ensure_table(db: sqlite_utils.Database) -> None:
    if _TABLE not in db.table_names():
        db[_TABLE].create(
            {
                "id":            str,
                "content":       str,
                "source":        str,
                "metadata_json": str,
                "created_at":    str,
            },
            pk="id",
        )
        log.info("memory.store.table_created", table=_TABLE)


def _row_to_memory(row: dict) -> Memory:
    return Memory(
        id=row["id"],
        content=row["content"],
        source=row["source"],
        metadata=json.loads(row.get("metadata_json") or "{}"),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_memory(
    content: str,
    source: str = "agent",
    metadata: dict | None = None,
) -> Memory:
    """Persist a new memory row and return the Memory object."""
    db = _db()
    memory = Memory(
        id=str(uuid.uuid4()),
        content=content,
        source=source,
        metadata=metadata or {},
        created_at=datetime.utcnow(),
    )
    db[_TABLE].insert(
        {
            "id":            memory.id,
            "content":       memory.content,
            "source":        memory.source,
            "metadata_json": json.dumps(memory.metadata),
            "created_at":    memory.created_at.isoformat(),
        }
    )
    log.info("memory.store.saved", id=memory.id, source=source)
    return memory


def get_recent(limit: int = 20) -> list[Memory]:
    """Return the most recently created memories."""
    db = _db()
    rows = db.execute(
        f"SELECT * FROM {_TABLE} ORDER BY created_at DESC LIMIT ?",
        [limit],
    ).fetchall()
    cols = [d[1] for d in db.execute(f"PRAGMA table_info({_TABLE})").fetchall()]
    result = [_row_to_memory(dict(zip(cols, r))) for r in rows]
    log.debug("memory.store.get_recent", count=len(result), limit=limit)
    return result


def get_by_source(source: str) -> list[Memory]:
    """Return all memories from a given source, newest first."""
    db = _db()
    rows = db.execute(
        f"SELECT * FROM {_TABLE} WHERE source = ? ORDER BY created_at DESC",
        [source],
    ).fetchall()
    cols = [d[1] for d in db.execute(f"PRAGMA table_info({_TABLE})").fetchall()]
    result = [_row_to_memory(dict(zip(cols, r))) for r in rows]
    log.debug("memory.store.get_by_source", source=source, count=len(result))
    return result


def delete_memory(memory_id: str) -> None:
    """Hard-delete a memory row by ID."""
    db = _db()
    db[_TABLE].delete(memory_id)
    log.info("memory.store.deleted", id=memory_id)
