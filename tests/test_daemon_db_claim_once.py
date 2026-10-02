"""daemon_db.claim_once — the atomic check-and-set dedupe helper used by the
Jenkins webhook and autoscale's once-per-day guard.

The two-step check_cooldown()/set_cooldown() it replaces at those call sites
had a gap a second caller could land in, and treated a database error as
"never seen before" (the unsafe answer — it lets a duplicate through).
claim_once() does both steps under one lock and fails closed instead.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent.integrations import daemon_db


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_db, "_DB_PATH", tmp_path / "daemon.db")


def _backdate(resource_key: str, minutes_ago: float) -> None:
    """Rewrite a claimed key's timestamp as if it happened `minutes_ago`."""
    con = daemon_db._conn()
    past = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    con.execute("UPDATE daemon_cooldowns SET last_action = ? WHERE resource_key = ?",
                (past, resource_key))
    con.commit()
    con.close()


def test_first_claim_succeeds():
    assert daemon_db.claim_once("res/1", cooldown_minutes=60) is True


def test_second_claim_within_cooldown_fails():
    assert daemon_db.claim_once("res/1", cooldown_minutes=60) is True
    assert daemon_db.claim_once("res/1", cooldown_minutes=60) is False


def test_claim_after_cooldown_expires_succeeds():
    assert daemon_db.claim_once("res/1", cooldown_minutes=60) is True
    _backdate("res/1", minutes_ago=61)
    assert daemon_db.claim_once("res/1", cooldown_minutes=60) is True


def test_claim_just_inside_cooldown_still_fails():
    assert daemon_db.claim_once("res/1", cooldown_minutes=60) is True
    _backdate("res/1", minutes_ago=59)
    assert daemon_db.claim_once("res/1", cooldown_minutes=60) is False


def test_different_keys_dont_share_a_claim():
    assert daemon_db.claim_once("res/1", cooldown_minutes=60) is True
    assert daemon_db.claim_once("res/2", cooldown_minutes=60) is True


def test_database_error_fails_closed(monkeypatch):
    """A DB problem must be treated as "already claimed", not "unseen" — the
    old check-then-set fell through to True (let the duplicate through) on
    exactly this kind of error."""
    def boom():
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(daemon_db, "_conn", boom)
    assert daemon_db.claim_once("res/1", cooldown_minutes=60) is False


def test_fix_count_increments_across_claims_after_cooldown():
    daemon_db.claim_once("res/1", cooldown_minutes=60)
    _backdate("res/1", minutes_ago=61)
    daemon_db.claim_once("res/1", cooldown_minutes=60)
    assert daemon_db.get_fix_count("res/1") == 2
