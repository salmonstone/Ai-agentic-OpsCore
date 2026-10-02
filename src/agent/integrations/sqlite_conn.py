"""
Shared SQLite connection helper.

Every AtlasOS store (approvals, daemon_db, autoscale_db, ...) used to open
its own `sqlite3.connect(...)` with no journal mode or busy timeout set. That
was invisible on a single laptop process, but AtlasOS no longer has just one
writer: the daemon's watcher threads, the webhook receiver, the MCP server,
and the CLI can all touch the same data/*.db file at the same time now.
SQLite's default journal mode locks the whole file for the duration of a
write, so a second writer (or even a reader) that lands in that window gets
`database is locked` immediately instead of waiting.

Two settings fix that without changing any store's schema or call sites:

  - WAL journal mode lets readers keep reading while one writer commits,
    instead of the whole file being exclusively locked.
  - busy_timeout makes a connection that finds the database locked retry
    quietly for up to BUSY_TIMEOUT_MS before raising, instead of raising
    on the first attempt.

Use `connect()` in place of `sqlite3.connect()` in any store's `_conn()`.
It's a drop-in: same return type, same row access, just two PRAGMAs and
(optionally) the schema applied in one place.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 5000   # retry for up to 5s on a lock before raising


def connect(
    path: Path | str,
    ddl: str = "",
    *,
    check_same_thread: bool = False,
    row_factory=sqlite3.Row,
) -> sqlite3.Connection:
    """Open (creating the parent directory if needed), set WAL + busy_timeout,
    optionally apply `ddl` (a `CREATE TABLE IF NOT EXISTS ...` script), and
    return the connection. The caller still owns commit()/close() as before.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    if row_factory is not None:
        con.row_factory = row_factory
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if ddl:
        con.executescript(ddl)
    return con


def apply_pragmas(con: sqlite3.Connection) -> None:
    """For stores that don't go through connect() — e.g. the sqlite_utils
    (`Database(...)`) ones — apply the same two PRAGMAs to an existing
    connection: `apply_pragmas(db.conn)`."""
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
