"""The generic Slack approve/reject dispatch in agent.integrations.webhook.

This is the code that runs when someone taps a real button in Slack, so a
bug here means either a click does nothing, or worse, an already-decided or
expired proposal gets applied twice. Covered against a throwaway
approvals.db; the registry and Slack HTTP calls are monkeypatched — no real
network call and no real fix executes.
"""
import asyncio

import pytest

from agent.core import approvals
from agent.integrations import webhook


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "_DB_PATH", tmp_path / "approvals.db")


@pytest.fixture(autouse=True)
def no_real_memory_writes(monkeypatch):
    """_handle_fix_action and the `test` fix both call memory.remember() for
    the audit trail — stub it so the suite never writes into the real
    data/memory.db / chroma_db just from running tests."""
    monkeypatch.setattr("agent.memory.retrieval.remember", lambda *a, **k: None)


@pytest.fixture
def captured_messages(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "agent.integrations.slack.update_action_message",
        lambda response_url, status, summary, detail: sent.append((status, summary, detail)) or True,
    )
    return sent


def run(coro):
    return asyncio.run(coro)


def test_approve_calls_the_registered_executor_with_stored_params_and_confirm_true(captured_messages, monkeypatch):
    seen = {}

    async def fake_execute(kind, params, confirm):
        seen["kind"], seen["params"], seen["confirm"] = kind, params, confirm
        return {"applied": True, "message": "done"}
    monkeypatch.setattr("agent.core.fix_registry.execute", fake_execute)

    a = approvals.create("k8s_apply_fix", "restart pod x", {"pod_name": "x", "namespace": "default"})
    run(webhook._handle_fix_action("approve_action", a.id, "aditya", "https://hooks.slack.test/x"))

    assert seen == {"kind": "k8s_apply_fix", "params": {"pod_name": "x", "namespace": "default"}, "confirm": True}
    final = approvals.get(a.id)
    assert final.status == "applied" and final.decided_by == "aditya"
    assert captured_messages[-1][0] == "APPLIED"


def test_reject_never_touches_the_registry(captured_messages, monkeypatch):
    called = {"n": 0}

    async def fake_execute(*a, **k):
        called["n"] += 1
        return {"applied": True}
    monkeypatch.setattr("agent.core.fix_registry.execute", fake_execute)

    a = approvals.create("test", "x", {})
    run(webhook._handle_fix_action("reject_action", a.id, "aditya", "https://hooks.slack.test/x"))

    assert called["n"] == 0
    assert approvals.get(a.id).status == "rejected"
    assert captured_messages[-1][0] == "REJECTED"


def test_a_failed_fix_is_recorded_as_failed_not_applied(captured_messages, monkeypatch):
    async def fake_execute(kind, params, confirm):
        return {"applied": False, "message": "no automated fix available"}
    monkeypatch.setattr("agent.core.fix_registry.execute", fake_execute)

    a = approvals.create("tls_apply_fix", "x", {})
    run(webhook._handle_fix_action("approve_action", a.id, "aditya", "https://hooks.slack.test/x"))

    assert approvals.get(a.id).status == "failed"
    assert captured_messages[-1][0] == "FAILED"


def test_an_exception_in_the_executor_is_caught_and_recorded_as_failed(captured_messages, monkeypatch):
    async def fake_execute(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr("agent.core.fix_registry.execute", fake_execute)

    a = approvals.create("aws_apply_fix", "x", {})
    run(webhook._handle_fix_action("approve_action", a.id, "aditya", "https://hooks.slack.test/x"))

    assert approvals.get(a.id).status == "failed"
    assert captured_messages[-1][0] == "FAILED"


def test_clicking_an_already_decided_action_does_not_execute_again(captured_messages, monkeypatch):
    calls = {"n": 0}

    async def fake_execute(*a, **k):
        calls["n"] += 1
        return {"applied": True}
    monkeypatch.setattr("agent.core.fix_registry.execute", fake_execute)

    a = approvals.create("test", "x", {})
    approvals.decide(a.id, "applied", "aditya", result={"applied": True})

    # A double-click, or Slack retrying an already-processed interaction.
    run(webhook._handle_fix_action("approve_action", a.id, "someone_else", "https://hooks.slack.test/x"))

    assert calls["n"] == 0
    assert captured_messages[-1][0] == "APPLIED"   # reports current state, doesn't re-run


def test_expired_action_is_marked_expired_and_never_executed(captured_messages, monkeypatch):
    calls = {"n": 0}

    async def fake_execute(*a, **k):
        calls["n"] += 1
        return {"applied": True}
    monkeypatch.setattr("agent.core.fix_registry.execute", fake_execute)

    a = approvals.create("test", "x", {}, ttl_minutes=-1)   # already past expiry
    run(webhook._handle_fix_action("approve_action", a.id, "aditya", "https://hooks.slack.test/x"))

    assert calls["n"] == 0
    assert approvals.get(a.id).status == "expired"
    assert captured_messages[-1][0] == "EXPIRED"


def test_unknown_action_id_is_reported_without_crashing(captured_messages):
    run(webhook._handle_fix_action("approve_action", "no-such-id", "aditya", "https://hooks.slack.test/x"))
    assert captured_messages[-1][0] == "FAILED"


# --- Slack signature verification (unchanged code, no prior test existed) --

def test_slack_signature_accepts_a_correctly_signed_request(monkeypatch):
    import hashlib
    import hmac
    import time

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    ts = str(int(time.time()))
    body = b"payload=%7B%7D"
    base = f"v0:{ts}:{body.decode()}".encode()
    sig = "v0=" + hmac.new(b"shh", base, hashlib.sha256).hexdigest()
    assert webhook._verify_slack_signature(body, ts, sig) is True


def test_slack_signature_rejects_a_wrong_secret(monkeypatch):
    import time
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    assert webhook._verify_slack_signature(b"payload=%7B%7D", str(int(time.time())), "v0=wrong") is False


def test_slack_signature_rejects_a_stale_timestamp(monkeypatch):
    import hashlib
    import hmac
    import time

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    old_ts = str(int(time.time()) - 3600)   # an hour old — outside the 5 minute window
    body = b"payload=%7B%7D"
    base = f"v0:{old_ts}:{body.decode()}".encode()
    sig = "v0=" + hmac.new(b"shh", base, hashlib.sha256).hexdigest()
    assert webhook._verify_slack_signature(body, old_ts, sig) is False


# --- registry ----------------------------------------------------------

def test_fix_registry_loads_every_apply_fix_tool_plus_test():
    from agent.core.fix_registry import kinds
    found = kinds()
    for expected in ("test", "k8s_apply_fix", "jenkins_apply_fix", "ingress_apply_fix",
                     "tls_apply_fix", "aws_apply_fix", "cost_apply_fix"):
        assert expected in found


def test_fix_registry_unknown_kind_fails_closed_without_raising():
    from agent.core.fix_registry import execute
    result = run(execute("not_a_real_kind", {}, confirm=True))
    assert result["applied"] is False


def test_test_action_only_runs_with_confirm_true():
    from agent.core.fix_registry import execute
    result = run(execute("test", {"marker": "abc"}, confirm=False))
    assert result["applied"] is False
    result = run(execute("test", {"marker": "abc"}, confirm=True))
    assert result["applied"] is True and result["marker"] == "abc"
