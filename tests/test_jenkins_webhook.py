"""POST /webhook/jenkins: a failed build is diagnosed and sent to Slack —
as an Approve/Reject proposal when the fix can be automated. Covers auth
(fail closed), payload parsing, once-per-build dedupe, and both outcomes.
Diagnosis, Slack and the dbs are faked/isolated — no Jenkins, no network.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from agent.core import approvals
from agent.integrations import daemon_db, webhook

SECRET = "jenkins-hook-secret-123"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "_DB_PATH", tmp_path / "approvals.db")
    monkeypatch.setattr(daemon_db, "_DB_PATH", tmp_path / "daemon.db")
    monkeypatch.setattr("agent.memory.retrieval.remember", lambda *a, **k: None)
    monkeypatch.setenv("JENKINS_WEBHOOK_SECRET", SECRET)


@pytest.fixture
def handled(monkeypatch):
    """Capture background handling instead of running a real diagnosis."""
    calls = []

    async def fake(job, number):
        calls.append((job, number))
        return {}
    monkeypatch.setattr(webhook, "_handle_jenkins_failure", fake)
    return calls


client = TestClient(webhook.app)


def post(body, token=SECRET, via="header"):
    if via == "header":
        return client.post("/webhook/jenkins", json=body, headers={"X-AtlasOS-Token": token})
    return client.post(f"/webhook/jenkins?token={token}", json=body)


FAIL = {"job": "infragpt", "build": 17, "status": "FAILURE"}


# --- auth ---------------------------------------------------------------------

def test_missing_token_is_rejected(handled):
    assert client.post("/webhook/jenkins", json=FAIL).status_code == 401
    assert handled == []


def test_wrong_token_is_rejected(handled):
    assert post(FAIL, token="nope").status_code == 401
    assert handled == []


def test_no_secret_configured_rejects_everything(handled, monkeypatch):
    monkeypatch.delenv("JENKINS_WEBHOOK_SECRET")
    assert post(FAIL, token="").status_code == 401
    assert post(FAIL, token="anything").status_code == 401
    assert handled == []


def test_token_in_query_string_is_accepted(handled):
    assert post(FAIL, via="query").status_code == 202
    assert handled == [("infragpt", 17)]


# --- parsing & filtering ------------------------------------------------------

@pytest.mark.parametrize("payload, expected", [
    ({"job": "api/main", "build": 42, "status": "FAILURE"}, ("api/main", 42, "FAILURE")),
    ({"job": "api", "build": "42", "status": "failure"}, ("api", 42, "FAILURE")),
    ({"name": "api", "build": {"number": 42, "phase": "COMPLETED", "status": "FAILURE"}}, ("api", 42, "FAILURE")),
    ({"name": "api", "build": {"number": 42, "phase": "STARTED"}}, None),
    ({"job": "api", "build": "x", "status": "FAILURE"}, None),
    ({"build": 42, "status": "FAILURE"}, None),
    ("not a dict", None),
])
def test_parse_jenkins_event(payload, expected):
    assert webhook.parse_jenkins_event(payload) == expected


@pytest.mark.parametrize("status", ["SUCCESS", "ABORTED", "UNSTABLE"])
def test_non_failures_are_ignored(handled, status):
    r = post({**FAIL, "status": status})
    assert r.status_code == 200 and "ignored" in r.text
    assert handled == []


def test_bad_json_is_400(handled):
    r = client.post("/webhook/jenkins", content=b"{not json",
                    headers={"X-AtlasOS-Token": SECRET, "content-type": "application/json"})
    assert r.status_code == 400


# --- once per build -----------------------------------------------------------

def test_same_build_is_handled_once(handled):
    assert post(FAIL).status_code == 202
    r = post(FAIL)                                   # retry, or plugin's second phase
    assert r.status_code == 200 and "duplicate" in r.text
    assert handled == [("infragpt", 17)]


def test_plugin_completed_then_finalized_is_handled_once(handled):
    base = {"name": "infragpt", "build": {"number": 18, "status": "FAILURE"}}
    post({**base, "build": {**base["build"], "phase": "COMPLETED"}})
    post({**base, "build": {**base["build"], "phase": "FINALIZED"}})
    assert handled == [("infragpt", 18)]


def test_different_builds_are_each_handled(handled):
    post(FAIL)
    post({**FAIL, "build": 19})
    assert handled == [("infragpt", 17), ("infragpt", 19)]


# --- what gets sent -----------------------------------------------------------

@pytest.fixture
def slack(monkeypatch):
    sent = {"alerts": [], "approvals": []}
    monkeypatch.setattr("agent.integrations.slack.is_configured", lambda: True)
    monkeypatch.setattr("agent.integrations.slack.send_alert_generic",
                        lambda **k: sent["alerts"].append(k) or True)
    monkeypatch.setattr("agent.integrations.slack.send_action_approval_request",
                        lambda a: sent["approvals"].append(a) or True)
    return sent


def _fake_diagnosis(monkeypatch, **diag):
    async def fake(job, number):
        return {"job_name": job, "build_number": number, "root_cause": "flaky network test",
                "confidence": "high", "explanation": "", "prevention": "", **diag}
    monkeypatch.setattr("agent.mcp_server.jenkins_diagnose", fake)


def test_automatable_failure_becomes_an_approval_proposal(monkeypatch, slack):
    from agent.core.models import JenkinsFixAction
    auto = next(a for a in JenkinsFixAction if a.value != "MANUAL_ONLY")
    _fake_diagnosis(monkeypatch, fix_action=auto)

    out = asyncio.run(webhook._handle_jenkins_failure("infragpt", 17))

    assert out["proposed"] is True
    (a,) = slack["approvals"]
    assert a.kind == "jenkins_apply_fix"
    assert a.params == {"job_name": "infragpt", "build_number": 17}
    assert "flaky network test" in a.summary and auto.value in a.summary
    assert slack["alerts"] == []


def test_manual_only_failure_is_a_plain_alert_not_a_proposal(monkeypatch, slack):
    from agent.core.models import JenkinsFixAction
    _fake_diagnosis(monkeypatch, fix_action=JenkinsFixAction.MANUAL_ONLY)

    out = asyncio.run(webhook._handle_jenkins_failure("infragpt", 17))

    assert out["proposed"] is False
    assert slack["approvals"] == []
    (alert,) = slack["alerts"]
    assert "flaky network test" in alert["message"] and "manual" in alert["message"].lower()


def test_diagnosis_failure_still_tells_slack(monkeypatch, slack):
    async def boom(job, number):
        raise RuntimeError("Jenkins unreachable")
    monkeypatch.setattr("agent.mcp_server.jenkins_diagnose", boom)

    out = asyncio.run(webhook._handle_jenkins_failure("infragpt", 17))

    assert out == {"handled": True, "diagnosed": False}
    (alert,) = slack["alerts"]
    assert "couldn't diagnose" in alert["message"]


def test_no_action_needed_is_a_plain_alert_not_a_proposal(monkeypatch, slack):
    """Found live: a job built to fail on purpose (NO_ACTION_NEEDED) got a
    pointless Approve button, because only MANUAL_ONLY was excluded."""
    from agent.core.models import JenkinsFixAction
    _fake_diagnosis(monkeypatch, fix_action=JenkinsFixAction.NO_ACTION_NEEDED)

    out = asyncio.run(webhook._handle_jenkins_failure("atlas-os-hook-test", 1))

    assert out["proposed"] is False and slack["approvals"] == []
    (alert,) = slack["alerts"]
    assert "No action needed" in alert["message"]
