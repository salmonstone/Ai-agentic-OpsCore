"""Notification inbox, GitHub Actions page, and AWS page.

The inbox must get every alert even with Slack off — that's what makes
Slack optional. GitHub and AWS must say "couldn't check" when they can't
reach the service, never show an empty page as "nothing there".
Everything external is faked; conftest points the inbox at a temp file.
"""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.integrations import notifications


# --- inbox store -----------------------------------------------------------------

def test_record_list_and_mark_read():
    a = notifications.record("Pod crashing", "api in prod", "critical")
    notifications.record("Daily summary", kind="summary")
    out = notifications.list_recent()
    assert out["unread"] == 2 and out["items"][0]["title"] == "Daily summary"
    notifications.mark_read([a])
    assert notifications.list_recent()["unread"] == 1
    notifications.mark_read()
    assert notifications.list_recent()["unread"] == 0


def test_unknown_severity_becomes_info():
    notifications.record("x", severity="weird")
    assert notifications.list_recent()["items"][0]["severity"] == "info"


def test_store_keeps_only_the_newest(monkeypatch):
    monkeypatch.setattr(notifications, "MAX_ROWS", 3)
    for i in range(5):
        notifications.record(f"n{i}")
    titles = [n["title"] for n in notifications.list_recent()["items"]]
    assert len(titles) == 3 and "n0" not in titles


def test_record_never_raises(monkeypatch):
    monkeypatch.setattr(notifications, "_conn", lambda: (_ for _ in ()).throw(OSError("disk full")))
    assert notifications.record("x") is None


# --- every sender feeds the inbox, Slack or not --------------------------------------

@pytest.fixture
def no_slack(monkeypatch):
    monkeypatch.setattr("agent.integrations.slack._webhook_url", lambda: "")


def test_generic_alert_is_recorded_even_without_slack(no_slack):
    from agent.integrations import slack
    assert slack.send_alert_generic("Jenkins build failed", "infragpt #22", "warning", fields={"Job": "infragpt"}) is False
    n = notifications.list_recent()["items"][0]
    assert n["title"] == "Jenkins build failed" and n["severity"] == "warning" and n["meta"]["Job"] == "infragpt"


def test_resolved_recovery_and_approval_are_recorded(no_slack):
    from agent.integrations import slack
    slack.send_resolved_generic("api healthy")
    slack.send_recovery("api-7f9c", "prod")
    slack.send_action_approval_request(SimpleNamespace(id="a1", kind="k8s_apply_fix", summary="Restart api"))
    kinds = [n["kind"] for n in notifications.list_recent()["items"]]
    assert kinds == ["approval", "resolved", "resolved"]


def test_daily_summary_blocks_are_recorded_as_summary(no_slack):
    from agent.integrations import slack
    slack.send_alert_blocks([{"type": "header", "text": {"type": "plain_text", "text": "AtlasOS daily summary — Wed"}}])
    n = notifications.list_recent()["items"][0]
    assert n["kind"] == "summary" and "daily summary" in n["title"]


def test_escalation_is_recorded_with_no_pager_configured(monkeypatch):
    from agent.integrations import pagerduty
    monkeypatch.setattr(pagerduty, "_pd_key", lambda: "")
    monkeypatch.setattr(pagerduty, "_og_key", lambda: "")
    assert pagerduty.page_oncall("api down", "3 failed heals")["skipped"] is True
    n = notifications.list_recent()["items"][0]
    assert n["kind"] == "page" and n["severity"] == "critical" and "not configured" in n["meta"]["provider"]


# --- endpoints ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def server():
    before = os.environ.get("MCP_READONLY")
    from agent.dashboard import server as srv
    if before is None:
        os.environ.pop("MCP_READONLY", None)
    else:
        os.environ["MCP_READONLY"] = before
    return srv


@pytest.fixture
def client(server):
    return TestClient(server.app)


def test_notifications_endpoints(client):
    notifications.record("one"); notifications.record("two")
    assert client.get("/api/notifications").json()["unread"] == 2
    assert client.post("/api/notifications/read", json={}).json()["marked"] == 2
    assert client.get("/api/notifications").json()["unread"] == 0


def test_github_without_a_token_says_so(client, monkeypatch):
    monkeypatch.setattr("agent.integrations.github_actions._token", lambda: None)
    assert client.get("/api/github/runs").json() == {"configured": False, "runs": []}


def test_github_runs_and_error_is_not_empty_success(client, monkeypatch):
    gha = "agent.integrations.github_actions."
    monkeypatch.setattr(gha + "_token", lambda: "t")
    monkeypatch.setattr(gha + "resolve_repo", lambda r: r or "o/r")
    monkeypatch.setattr(gha + "list_runs", lambda repo, limit, failed_only: [{"id": 1, "conclusion": "failure"}])
    assert client.get("/api/github/runs").json()["runs"] == [{"id": 1, "conclusion": "failure"}]

    def boom(*a, **k):
        raise RuntimeError("403 rate limited")
    monkeypatch.setattr(gha + "list_runs", boom)
    out = client.get("/api/github/runs").json()
    assert out["runs"] == [] and "rate limited" in out["error"]


def test_github_job_log_is_redacted(client, monkeypatch):
    monkeypatch.setattr("agent.integrations.github_actions.get_job_log",
                        lambda repo, job_id, tail_chars: "step 1\nexport GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123456\n")
    logs = client.get("/api/github/job-log?repo=o/r&job_id=5").json()["logs"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in logs and "step 1" in logs


@pytest.fixture
def aws(monkeypatch, server):
    monkeypatch.setitem(server._aws_cache, "data", None)
    calls = {"cost": 0}

    def cost(days):
        calls["cost"] += 1
        return {"total": 90.0, "by_service": {"EC2": 60.0}, "daily": [{"date": "2026-09-30", "amount": 3.0}],
                "last_month": 80.0, "forecast": 95.0, "month_change_pct": 12.5, "month_to_date": 3.0}
    monkeypatch.setattr("agent.integrations.aws_cost.get_cost_and_usage", cost)
    for fn in ("get_all_ec2", "get_all_rds", "get_all_load_balancers", "get_elastic_ips"):
        monkeypatch.setattr(f"agent.integrations.aws.{fn}", lambda region: [])
    return calls


def test_aws_unreachable_is_reported_and_not_cached(client, server, aws, monkeypatch):
    def no_creds(service, region_name=None):
        raise RuntimeError("Unable to locate credentials")
    monkeypatch.setattr("agent.integrations.aws.get_aws_client", no_creds)
    out = client.get("/api/aws/overview").json()
    assert out["reachable"] is False and "credentials" in out["error"].lower()
    assert server._aws_cache["data"] is None and aws["cost"] == 0     # nothing else was asked


def test_aws_overview_shape_and_cache(client, aws, monkeypatch):
    sts = SimpleNamespace(get_caller_identity=lambda: {"Arn": "arn:aws:iam::1:user/a", "Account": "1"})
    monkeypatch.setattr("agent.integrations.aws.get_aws_client", lambda service, region_name=None: sts)
    out = client.get("/api/aws/overview").json()
    assert out["reachable"] and out["identity"]["account"] == "1" and out["cost"]["total"] == 90.0
    client.get("/api/aws/overview")
    assert aws["cost"] == 1                       # cached — Cost Explorer costs money
    client.get("/api/aws/overview?force=true")
    assert aws["cost"] == 2


def test_aws_empty_cost_is_couldnt_check_not_zero(client, aws, monkeypatch):
    sts = SimpleNamespace(get_caller_identity=lambda: {"Arn": "a", "Account": "1"})
    monkeypatch.setattr("agent.integrations.aws.get_aws_client", lambda service, region_name=None: sts)
    monkeypatch.setattr("agent.integrations.aws_cost.get_cost_and_usage",
                        lambda days: {"total": 0.0, "by_service": {}, "daily": []})
    out = client.get("/api/aws/overview").json()
    assert out["cost"] is None and out["cost_error"]
