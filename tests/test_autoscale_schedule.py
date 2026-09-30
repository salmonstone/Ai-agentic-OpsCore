"""Autoscale's scheduled scale-down/up: it must fire once per day even when
the schedule watcher's 60s polls never land on the target minute exactly.

The original trigger (`now_hhmm == target_utc`) is the same class of bug the
nightly trigger had: a poll that's a few seconds late skips that minute, and
the whole day's scale-down (or scale-up) never happens. These tests simulate
a real day of 60s polls, so a trigger that depends on one exact minute fails
here — same shape as tests/test_nightly_trigger.py.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agent.integrations import autoscale_db, daemon_db
from agent.skills import autoscale


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(autoscale_db, "_DB_PATH", tmp_path / "autoscale.db")
    monkeypatch.setattr(daemon_db, "_DB_PATH", tmp_path / "daemon.db")


@pytest.fixture
def scaled(monkeypatch):
    """Fake the kubectl-backed replica get/set — no cluster, no Slack.
    Replica state is kept per (deployment, namespace), like the real
    cluster, so two different policies don't see each other's scaling."""
    state: dict[tuple[str, str], int] = {}
    calls = []

    def fake_get(deployment, namespace):
        return state.get((deployment, namespace), 5)

    def fake_set(deployment, namespace, replicas):
        calls.append((deployment, namespace, replicas))
        state[(deployment, namespace)] = replicas
        return True

    monkeypatch.setattr(autoscale, "_get_current_replicas", fake_get)
    monkeypatch.setattr(autoscale, "_set_replicas", fake_set)
    monkeypatch.setattr(autoscale, "_notify", lambda *a, **k: None)
    return calls, state


def _policy(**over):
    p = {"id": "p1", "deployment": "api", "namespace": "default",
         "schedule_down_utc": "17:30", "schedule_up_utc": "03:30",
         "down_replicas": 1, "up_replicas": 5}
    p.update(over)
    return p


def _poll_day(policy: dict, start: datetime, hours: float, step_min: int = 60) -> list[dict]:
    """Poll like the real schedule watcher does, every `step_min` minutes."""
    fired, t = [], start
    while t < start + timedelta(hours=hours):
        r = autoscale.apply_schedule(policy, t)
        if r["action"] != "no_action":
            fired.append(r)
        t += timedelta(minutes=step_min)
    return fired


# --- schedule_due (pure rule) ------------------------------------------------

@pytest.mark.parametrize("hh, mm, due", [
    (17, 0, False), (17, 29, False),      # before 17:30: not yet
    (17, 30, True), (17, 31, True),       # any minute from 17:30 on, not just :30
    (23, 59, True),
])
def test_due_once_the_target_time_has_passed(hh, mm, due):
    assert autoscale.schedule_due(datetime(2026, 9, 29, hh, mm), "17:30") is due


def test_no_target_is_never_due():
    assert autoscale.schedule_due(datetime(2026, 9, 29, 17, 30), None) is False
    assert autoscale.schedule_due(datetime(2026, 9, 29, 17, 30), "") is False


def test_malformed_target_is_never_due():
    assert autoscale.schedule_due(datetime(2026, 9, 29, 17, 30), "not-a-time") is False


# --- the real polling behaviour ----------------------------------------------

@pytest.mark.parametrize("offset_min", [0, 1, 15, 29, 45])
def test_a_day_of_polls_that_never_hits_the_exact_minute_still_fires_once(scaled, offset_min):
    """The regression: 60s polls starting at :offset never land on 17:30 or
    03:30 exactly unless offset happens to be 30/0 — every other offset used
    to skip the day entirely."""
    calls, _ = scaled
    start = datetime(2026, 9, 29, 0, offset_min, tzinfo=timezone.utc)
    fired = _poll_day(_policy(), start, hours=24)
    assert [r["action"] for r in fired] == ["schedule_down"]
    assert calls == [("api", "default", 1)]


def test_both_down_and_up_fire_once_each_per_day(scaled):
    fired = _poll_day(_policy(), datetime(2026, 9, 29, 0, 7, tzinfo=timezone.utc), hours=24)
    assert [r["action"] for r in fired] == ["schedule_down"]
    # cross into the next day to catch the 03:30 up-scale too
    fired2 = _poll_day(_policy(), datetime(2026, 9, 30, 0, 7, tzinfo=timezone.utc), hours=6)
    assert [r["action"] for r in fired2] == ["schedule_up"]


def test_runs_once_per_day_across_several_days(scaled):
    """Once scaled down on day 1, the deployment stays at the down target —
    there's nothing left to scale on days 2 and 3, same as a real cluster
    with no up-schedule. What must still happen every day is the *claim*:
    the policy gets evaluated instead of the trigger silently never firing
    again, which is what "already_at_target" (not "already_ran_today")
    proves below."""
    p = _policy(schedule_up_utc=None)
    reasons, t = [], datetime(2026, 9, 29, 0, 7, tzinfo=timezone.utc)
    while t < datetime(2026, 10, 2, 0, 7, tzinfo=timezone.utc):
        r = autoscale.apply_schedule(p, t)
        if r["action"] != "no_action" or r.get("reason") == "already_at_target":
            reasons.append(r.get("reason", r["action"]))
        t += timedelta(minutes=60)
    assert reasons == ["schedule_down", "already_at_target", "already_at_target"]


def test_already_at_target_is_a_no_op_but_still_claims_the_day(scaled):
    calls, state = scaled
    state[("api", "default")] = 1   # already at the down target
    r = autoscale.apply_schedule(_policy(), datetime(2026, 9, 29, 17, 45, tzinfo=timezone.utc))
    assert r == {"action": "no_action", "deployment": "api", "namespace": "default",
                 "old_replicas": 1, "new_replicas": 1, "success": True,
                 "reason": "already_at_target"}
    assert calls == []


def test_second_poll_the_same_day_is_a_no_op(scaled):
    calls, _ = scaled
    p = _policy()
    autoscale.apply_schedule(p, datetime(2026, 9, 29, 17, 31, tzinfo=timezone.utc))
    r = autoscale.apply_schedule(p, datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc))
    assert r["action"] == "no_action" and r["reason"] == "already_ran_today"
    assert calls == [("api", "default", 1)]        # only the first poll scaled


def test_before_either_target_nothing_happens(scaled):
    calls, _ = scaled
    fired = _poll_day(_policy(), datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc), hours=13)
    assert fired == []
    assert calls == []


def test_two_policies_dont_share_a_claim(scaled):
    """Different policy ids must each get their own once-per-day slot, even
    for the same deployment+namespace (the claim key is keyed by policy id,
    not by what it scales)."""
    calls, state = scaled
    state[("api", "default")] = 1   # both policies' target — isolates the
                                     # assertion to the claim, not the scale
    now = datetime(2026, 9, 29, 17, 30, tzinfo=timezone.utc)
    r1 = autoscale.apply_schedule(_policy(id="p1"), now)
    r2 = autoscale.apply_schedule(_policy(id="p2"), now)
    assert r1["reason"] == r2["reason"] == "already_at_target"    # both got to check
    assert calls == []


def test_two_policies_for_different_deployments_both_scale(scaled):
    calls, _ = scaled
    now = datetime(2026, 9, 29, 17, 30, tzinfo=timezone.utc)
    r1 = autoscale.apply_schedule(_policy(id="p1", deployment="api"), now)
    r2 = autoscale.apply_schedule(_policy(id="p2", deployment="worker"), now)
    assert r1["action"] == r2["action"] == "schedule_down"
    assert calls == [("api", "default", 1), ("worker", "default", 1)]


def test_run_scheduled_scaling_shares_one_timestamp_across_policies(scaled, monkeypatch):
    """run_scheduled_scaling() computes `now` once for the whole batch, so
    every policy is judged against the same instant."""
    monkeypatch.setattr(autoscale_db, "list_policies",
                        lambda enabled_only=True: [_policy(id="p1", deployment="api"),
                                                   _policy(id="p2", deployment="worker")])
    results = autoscale.run_scheduled_scaling(datetime(2026, 9, 29, 17, 30, tzinfo=timezone.utc))
    assert [r["action"] for r in results] == ["schedule_down", "schedule_down"]
