"""The shared SQLite connection helper (agent.integrations.sqlite_conn).

Every store's own tests (test_daemon_db_claim_once.py, etc.) prove their
behavior is unchanged after switching to this helper. These tests are about
the helper itself: it actually sets WAL + busy_timeout, it's a drop-in for
the check-then-DDL-then-return pattern every store used to hand-roll, and a
concurrent writer waits instead of failing immediately.
"""
import sqlite3
import threading
import time

import pytest

from agent.integrations import sqlite_conn


def test_creates_the_parent_directory(tmp_path):
    path = tmp_path / "nested" / "store.db"
    con = sqlite_conn.connect(path)
    assert path.parent.is_dir()
    con.close()


def test_sets_wal_journal_mode(tmp_path):
    con = sqlite_conn.connect(tmp_path / "store.db")
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    con.close()


def test_sets_busy_timeout(tmp_path):
    con = sqlite_conn.connect(tmp_path / "store.db")
    timeout_ms = con.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout_ms == sqlite_conn.BUSY_TIMEOUT_MS
    con.close()


def test_applies_ddl_once(tmp_path):
    ddl = "CREATE TABLE IF NOT EXISTS widgets (id TEXT PRIMARY KEY)"
    con = sqlite_conn.connect(tmp_path / "store.db", ddl)
    con.execute("INSERT INTO widgets VALUES ('a')")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM widgets").fetchone()[0] == 1
    con.close()


def test_row_factory_defaults_to_dict_style_access(tmp_path):
    con = sqlite_conn.connect(tmp_path / "store.db",
                              "CREATE TABLE t (id TEXT, name TEXT)")
    con.execute("INSERT INTO t VALUES ('1', 'x')")
    con.commit()
    row = con.execute("SELECT * FROM t").fetchone()
    assert row["name"] == "x" and row[0] == "1"   # both key and index access
    con.close()


def test_row_factory_can_be_disabled(tmp_path):
    con = sqlite_conn.connect(tmp_path / "store.db",
                              "CREATE TABLE t (id TEXT)", row_factory=None)
    con.execute("INSERT INTO t VALUES ('1')")
    con.commit()
    row = con.execute("SELECT * FROM t").fetchone()
    assert row == ("1",)   # plain tuple, not sqlite3.Row
    con.close()


def test_apply_pragmas_on_an_existing_connection(tmp_path):
    """The sqlite_utils-based stores (event_queue, memory) don't go through
    connect() — they hand connect() an already-open db.conn instead."""
    raw = sqlite3.connect(str(tmp_path / "store.db"))
    sqlite_conn.apply_pragmas(raw)
    assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert raw.execute("PRAGMA busy_timeout").fetchone()[0] == sqlite_conn.BUSY_TIMEOUT_MS
    raw.close()


def test_a_second_writer_waits_instead_of_failing_immediately(tmp_path):
    """The actual point of busy_timeout: without it, a writer that finds the
    database locked raises `database is locked` right away. With it, the
    second writer here waits out the first one's brief transaction instead
    of erroring."""
    path = tmp_path / "store.db"
    sqlite_conn.connect(path, "CREATE TABLE t (id INTEGER)").close()

    holder = sqlite_conn.connect(path)
    holder.execute("BEGIN IMMEDIATE")   # takes the write lock and holds it

    def release_after_a_moment():
        time.sleep(0.3)
        holder.commit()
        holder.close()

    threading.Thread(target=release_after_a_moment).start()

    second = sqlite_conn.connect(path)
    started = time.monotonic()
    second.execute("INSERT INTO t VALUES (1)")   # must wait, not raise
    second.commit()
    waited = time.monotonic() - started

    assert waited >= 0.25   # actually waited for the lock to clear
    assert second.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    second.close()


def test_without_busy_timeout_the_same_scenario_raises_locked(tmp_path):
    """Control case, proving the test above exercises a real mechanism and
    isn't just passing by luck: a plain sqlite3.connect() with no busy
    timeout raises immediately in the same setup."""
    path = tmp_path / "store.db"
    sqlite_conn.connect(path, "CREATE TABLE t (id INTEGER)").close()

    holder = sqlite3.connect(str(path))
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")

    plain = sqlite3.connect(str(path))   # no busy_timeout set — default is 0
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        plain.execute("INSERT INTO t VALUES (1)")

    holder.commit()
    holder.close()
    plain.close()
