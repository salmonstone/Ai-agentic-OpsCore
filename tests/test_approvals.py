"""agent.core.approvals — the generic pending-action store behind Slack
approval of the *_apply_fix family. Runs against a throwaway db file so the
real data/approvals.db is never touched.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agent.core import approvals


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "_DB_PATH", tmp_path / "approvals.db")


def test_create_then_get_round_trips_everything():
    a = approvals.create("k8s_apply_fix", "restart crashlooping pod x",
                         {"pod_name": "x", "namespace": "default"})
    fetched = approvals.get(a.id)
    assert fetched.kind == "k8s_apply_fix"
    assert fetched.summary == "restart crashlooping pod x"
    assert fetched.params == {"pod_name": "x", "namespace": "default"}
    assert fetched.status == "pending"
    assert fetched.result is None


def test_get_missing_returns_none():
    assert approvals.get("does-not-exist") is None


def test_ids_are_unique():
    ids = {approvals.create("test", "x", {}).id for _ in range(20)}
    assert len(ids) == 20


def test_decide_records_status_actor_and_result():
    a = approvals.create("test", "x", {})
    approvals.decide(a.id, "applied", "aditya", result={"applied": True, "marker": "abc"})
    fetched = approvals.get(a.id)
    assert fetched.status == "applied"
    assert fetched.decided_by == "aditya"
    assert fetched.result == {"applied": True, "marker": "abc"}
    assert fetched.decided_at is not None


def test_decide_without_result_leaves_it_none():
    a = approvals.create("test", "x", {})
    approvals.decide(a.id, "rejected", "aditya")
    assert approvals.get(a.id).result is None


def test_is_expired_false_before_ttl_true_after():
    a = approvals.create("test", "x", {}, ttl_minutes=10)
    created = datetime.fromisoformat(a.created_at)
    assert approvals.is_expired(a, now=created + timedelta(minutes=5)) is False
    assert approvals.is_expired(a, now=created + timedelta(minutes=11)) is True


def test_is_expired_handles_a_garbage_timestamp_without_raising():
    a = approvals.create("test", "x", {})
    a.expires_at = "not-a-timestamp"
    assert approvals.is_expired(a) is False


def test_list_actions_newest_first_and_filtered_by_status():
    a1 = approvals.create("test", "first", {})
    a2 = approvals.create("test", "second", {})
    approvals.decide(a2.id, "rejected", "aditya")

    all_rows = approvals.list_actions()
    assert [r.id for r in all_rows] == [a2.id, a1.id]

    pending_only = approvals.list_actions(status="pending")
    assert [r.id for r in pending_only] == [a1.id]


def test_list_actions_respects_limit():
    for i in range(5):
        approvals.create("test", f"item {i}", {})
    assert len(approvals.list_actions(limit=2)) == 2


def test_expire_stale_marks_only_pending_past_expiry():
    fresh   = approvals.create("test", "fresh", {}, ttl_minutes=60)
    stale   = approvals.create("test", "stale", {}, ttl_minutes=-1)   # already past
    decided = approvals.create("test", "decided", {}, ttl_minutes=-1)
    approvals.decide(decided.id, "approved", "aditya")   # not "pending" — must be left alone

    count = approvals.expire_stale()

    assert count == 1
    assert approvals.get(fresh.id).status == "pending"
    assert approvals.get(stale.id).status == "expired"
    assert approvals.get(decided.id).status == "approved"   # unchanged, not clobbered


def test_params_with_nested_and_special_values_survive_json_round_trip():
    params = {"resource_id": "i-0abc", "region": "ap-south-1", "days": 30, "flag": True}
    a = approvals.create("aws_apply_fix", "x", params)
    assert approvals.get(a.id).params == params
