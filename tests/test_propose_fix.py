"""propose_fix: a remote MCP client (ChatGPT) may PROPOSE a fix, never apply
one. These tests pin the limits on that — allowed kinds, strict params, no
`confirm`, dedupe, the pending cap — and that an invalid proposal writes and
sends nothing. Slack and the approvals db are faked/isolated.
"""
import asyncio

import pytest

from agent.core import approvals, fix_registry
from agent.core.fix_registry import ProposalError, propose, validate_params


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "_DB_PATH", tmp_path / "approvals.db")
    monkeypatch.setattr("agent.memory.retrieval.remember", lambda *a, **k: None)


@pytest.fixture
def slack(monkeypatch):
    sent = []
    monkeypatch.setattr("agent.integrations.slack.is_configured", lambda: True)
    monkeypatch.setattr("agent.integrations.slack.send_action_approval_request",
                        lambda action: sent.append(action) or True)
    return sent


def _pending():
    return approvals.list_actions(status="pending")


# --- validate_params ----------------------------------------------------------

def test_strings_are_converted_to_declared_types():
    assert validate_params("cost_apply_fix", {"fix_id": "vol-1", "days": "30"}) == {"fix_id": "vol-1", "days": 30}
    assert validate_params("jenkins_apply_fix", {"job_name": "api", "build_number": "none"}) == \
        {"job_name": "api", "build_number": None}
    assert validate_params("jenkins_apply_fix", {"job_name": "api", "build_number": "42"})["build_number"] == 42


def test_bad_int_is_rejected():
    with pytest.raises(ProposalError, match="integer"):
        validate_params("cost_apply_fix", {"fix_id": "x", "days": "thirty"})


def test_unknown_param_is_rejected():
    with pytest.raises(ProposalError, match="Unknown param"):
        validate_params("k8s_apply_fix", {"pod_name": "p", "namespace": "n", "force": "yes"})


def test_missing_required_param_is_rejected():
    with pytest.raises(ProposalError, match="Missing required"):
        validate_params("k8s_apply_fix", {"pod_name": "p"})


def test_confirm_can_never_be_proposed():
    with pytest.raises(ProposalError, match="confirm"):
        validate_params("k8s_apply_fix", {"pod_name": "p", "namespace": "n", "confirm": True})


def test_unknown_kind_is_rejected():
    with pytest.raises(ProposalError, match="Unknown kind"):
        validate_params("rm_rf", {})


# --- propose ------------------------------------------------------------------

def test_valid_proposal_is_recorded_and_sent(slack):
    r = propose("k8s_apply_fix", {"pod_name": "api-1", "namespace": "prod"},
                "api-1 is crashlooping; restart it", source="MCP (ChatGPT)", remote=True)
    assert r["duplicate"] is False and r["status"] == "pending"
    (a,) = _pending()
    assert a.id == r["id"] and a.params == {"pod_name": "api-1", "namespace": "prod"}
    assert "proposed via MCP (ChatGPT)" in a.summary
    assert len(slack) == 1


def test_remote_cannot_propose_the_free_form_crashloop_fix(slack):
    with pytest.raises(ProposalError, match="can't be proposed remotely"):
        propose("k8s_crashloop_apply_fix",
                {"pod_name": "p", "namespace": "n", "deployment": "d", "container_name": "c",
                 "fix_kind": "kubectl", "fix_value": "kubectl delete ns prod"},
                "x", source="MCP", remote=True)
    assert _pending() == [] and slack == []


def test_identical_pending_proposal_is_deduped_not_resent(slack):
    args = ("k8s_apply_fix", {"pod_name": "api-1", "namespace": "prod"}, "restart api-1")
    first = propose(*args, source="MCP", remote=True)
    second = propose(*args, source="MCP", remote=True)
    assert second["duplicate"] is True and second["id"] == first["id"]
    assert len(_pending()) == 1 and len(slack) == 1


def test_remote_pending_cap(slack):
    for i in range(fix_registry.MAX_REMOTE_PENDING):
        propose("test", {"marker": f"m{i}"}, "t", source="MCP", remote=True)
    with pytest.raises(ProposalError, match="already waiting"):
        propose("test", {"marker": "one-too-many"}, "t", source="MCP", remote=True)
    assert len(_pending()) == fix_registry.MAX_REMOTE_PENDING


def test_cli_is_not_subject_to_the_remote_cap(slack):
    for i in range(fix_registry.MAX_REMOTE_PENDING + 2):
        propose("test", {"marker": f"m{i}"}, "t", source="CLI")
    assert len(_pending()) == fix_registry.MAX_REMOTE_PENDING + 2


def test_invalid_proposal_writes_and_sends_nothing(slack):
    with pytest.raises(ProposalError):
        propose("k8s_apply_fix", {"pod_name": "p"}, "missing namespace", source="MCP", remote=True)
    with pytest.raises(ProposalError, match="summary"):
        propose("test", {}, "   ", source="MCP", remote=True)
    assert approvals.list_actions() == [] and slack == []


def test_slack_failure_leaves_nothing_pending(monkeypatch):
    monkeypatch.setattr("agent.integrations.slack.is_configured", lambda: True)
    monkeypatch.setattr("agent.integrations.slack.send_action_approval_request", lambda a: False)
    with pytest.raises(ProposalError, match="Slack"):
        propose("test", {"marker": "x"}, "t", source="MCP", remote=True)
    assert _pending() == []
    assert approvals.list_actions()[0].status == "failed"


def test_approving_a_proposal_runs_the_fix_with_converted_types(slack, monkeypatch):
    """End to end through the real Slack-click handler: `days` arrives as an
    int even though it was proposed as the string "30"."""
    from agent.integrations import webhook

    seen = {}

    async def fake_execute(kind, params, confirm):
        seen.update(kind=kind, params=params, confirm=confirm)
        return {"applied": True}
    monkeypatch.setattr("agent.core.fix_registry.execute", fake_execute)
    monkeypatch.setattr("agent.integrations.slack.update_action_message", lambda *a, **k: True)

    r = propose("cost_apply_fix", {"fix_id": "vol-1", "days": "30"}, "gp2->gp3", source="MCP", remote=True)
    asyncio.run(webhook._handle_fix_action("approve_action", r["id"], "aditya", "https://hooks.slack.test/x"))

    assert seen == {"kind": "cost_apply_fix", "params": {"fix_id": "vol-1", "days": 30}, "confirm": True}
    assert approvals.get(r["id"]).status == "applied"


# --- the MCP tools ------------------------------------------------------------

def test_mcp_propose_fix_returns_an_error_instead_of_raising(slack):
    from agent.mcp_server import propose_fix
    out = asyncio.run(propose_fix(kind="k8s_apply_fix", summary="x", params={"pod_name": "p"}))
    assert out["proposed"] is False and "Missing required" in out["error"]


def test_mcp_propose_then_status(slack):
    from agent.mcp_server import approval_status, propose_fix
    out = asyncio.run(propose_fix(kind="test", summary="prove it", params={"marker": "abc"}))
    assert out["proposed"] is True and "Nothing has been changed" in out["message"]
    st = asyncio.run(approval_status(out["approval_id"]))
    assert st["found"] is True and st["status"] == "pending"
    assert asyncio.run(approval_status("nope"))["found"] is False


def test_mcp_list_proposable_fixes_hides_confirm_and_crashloop():
    from agent.mcp_server import list_proposable_fixes
    spec = asyncio.run(list_proposable_fixes())
    assert "k8s_crashloop_apply_fix" not in spec
    assert set(spec) == set(fix_registry.REMOTE_PROPOSABLE)
    assert all("confirm" not in params for params in spec.values())
    assert spec["k8s_apply_fix"]["namespace"]["required"] is True
