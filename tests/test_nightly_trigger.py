"""The daemon's nightly trigger: cost fixes, spend-anomaly and RDS-capacity
checks must run exactly once per day.

The original trigger (`hour == 1 and minute == 3`, polled every 10 minutes)
silently skipped most nights. These tests simulate a real night of 10-minute
polls, so a trigger that depends on landing on one exact minute fails here.
"""
from datetime import datetime, timedelta

import pytest

from agent.core import daemon as daemon_mod
from agent.core.daemon import HealingDaemon, nightly_due
from agent.integrations import daemon_db


@pytest.fixture(autouse=True)
def isolated_daemon_db(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_db, "_DB_PATH", tmp_path / "daemon.db")


@pytest.fixture
def runs(monkeypatch):
    """Replace the three nightly jobs with counters — no AWS, no Slack."""
    calls = []
    monkeypatch.setattr(HealingDaemon, "_run_cost_fixes", lambda self: calls.append("fixes") or 0)
    monkeypatch.setattr(HealingDaemon, "_check_spend_anomalies", lambda self, force=False: calls.append("spend"))
    monkeypatch.setattr(HealingDaemon, "_check_rds_capacity", lambda self, force=False: calls.append("rds"))
    return calls


def _poll_night(d: HealingDaemon, start: datetime, hours: float, step_min: int = 10) -> int:
    """Poll like the real loop does, every `step_min` minutes; return how many
    polls actually ran the nightly checks."""
    fired, t = 0, start
    while t < start + timedelta(hours=hours):
        fired += d._run_nightly_if_due(t)
        t += timedelta(minutes=step_min)
    return fired


# --- nightly_due (pure rule) ------------------------------------------------

@pytest.mark.parametrize("hh, mm, due", [
    (0, 0, False), (0, 59, False),        # before 1 AM: not yet
    (1, 0, True), (1, 7, True),           # any minute from 1 AM, not just :03
    (9, 30, True), (23, 59, True),        # laptop woke up late — still that day's run
])
def test_due_from_one_am_onward(hh, mm, due):
    assert nightly_due(datetime(2026, 9, 29, hh, mm), None) is due


def test_not_due_twice_on_the_same_day():
    assert nightly_due(datetime(2026, 9, 29, 14, 0), "2026-09-29") is False
    assert nightly_due(datetime(2026, 9, 30, 1, 0), "2026-09-29") is True


# --- the real polling behaviour ---------------------------------------------

@pytest.mark.parametrize("offset_min", [0, 1, 2, 4, 5, 7, 9])
def test_a_night_of_polls_that_never_hits_minute_3_still_runs_once(runs, offset_min):
    """The regression: polls at 00:01, 00:11, 00:21 ... never land on 1:03."""
    fired = _poll_night(HealingDaemon(), datetime(2026, 9, 29, 0, offset_min), hours=24)
    assert fired == 1
    assert runs == ["fixes", "spend", "rds"]


def test_laptop_asleep_at_one_am_runs_when_it_wakes(runs):
    d = HealingDaemon()
    assert d._run_nightly_if_due(datetime(2026, 9, 29, 8, 42)) is True
    assert runs == ["fixes", "spend", "rds"]


def test_runs_once_per_day_across_several_days(runs):
    fired = _poll_night(HealingDaemon(), datetime(2026, 9, 29, 0, 4), hours=72)
    assert fired == 3


def test_a_daemon_restart_the_same_day_does_not_rerun(runs):
    HealingDaemon()._run_nightly_if_due(datetime(2026, 9, 29, 1, 5))
    restarted = HealingDaemon()                 # fresh instance: in-memory state is gone
    assert restarted._run_nightly_if_due(datetime(2026, 9, 29, 13, 0)) is False
    assert runs == ["fixes", "spend", "rds"]    # only the first run


def test_before_one_am_nothing_runs(runs):
    assert _poll_night(HealingDaemon(), datetime(2026, 9, 29, 0, 0), hours=0.95) == 0
    assert runs == []


def test_nightly_hour_constant_drives_the_rule(monkeypatch):
    monkeypatch.setattr(daemon_mod, "NIGHTLY_HOUR", 3)
    assert daemon_mod.nightly_due(datetime(2026, 9, 29, 2, 0), None) is False
    assert daemon_mod.nightly_due(datetime(2026, 9, 29, 3, 0), None) is True
