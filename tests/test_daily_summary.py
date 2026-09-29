"""Daily summary: each section on its own, the "one broken source doesn't
sink the message" rule, and once-a-day sending. Every data source is faked.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.core.models import BuildInfo, JenkinsJob, NodeInfo, PodInfo
from agent.integrations import daemon_db
from agent.skills import daily_summary as ds
from agent.skills.daily_summary import Section

NOW = datetime(2026, 9, 29, 3, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_db, "_DB_PATH", tmp_path / "daemon.db")


def _node(status="Ready"):
    return NodeInfo(name="n", status=status, age="1d", kubelet_version="v1.31", instance_type="t3",
                    os_image="al2", capacity_cpu="2", capacity_memory="4Gi",
                    allocatable_cpu="2", allocatable_memory="4Gi")


def _pod(name, status="Running", ready="1/1", restarts=0):
    return PodInfo(name=name, namespace="app", status=status, ready=ready, restarts=restarts, age="1d", node="n")


# --- cluster ------------------------------------------------------------------

def test_cluster_all_healthy(monkeypatch):
    monkeypatch.setattr("agent.integrations.kubectl.get_nodes_detail", lambda: [_node(), _node()])
    monkeypatch.setattr("agent.integrations.kubectl.get_pods", lambda ns: [_pod("a"), _pod("job", "Succeeded", "0/1")])
    s = ds._cluster(NOW)
    assert s.status == "ok" and s.lines[0] == "2/2 nodes ready · 2/2 pods healthy"


def test_cluster_flags_crashloop_and_not_ready_pods(monkeypatch):
    monkeypatch.setattr("agent.integrations.kubectl.get_nodes_detail", lambda: [_node(), _node("NotReady")])
    monkeypatch.setattr("agent.integrations.kubectl.get_pods", lambda ns: [
        _pod("ok"), _pod("crash", "CrashLoopBackOff", "0/1", 12), _pod("half", "Running", "1/2")])
    s = ds._cluster(NOW)
    assert s.status == "warn"
    assert s.lines[0] == "1/2 nodes ready · 1/3 pods healthy"
    assert any("crash" in line and "CrashLoopBackOff" in line for line in s.lines)


def test_cluster_unreachable_is_error_not_all_clear(monkeypatch):
    monkeypatch.setattr("agent.integrations.kubectl.get_nodes_detail", lambda: [])
    assert ds._cluster(NOW).status == "error"


# --- jenkins ------------------------------------------------------------------

def test_jenkins_counts_only_failures_in_the_last_24h(monkeypatch):
    ms = lambda dt: int(dt.timestamp() * 1000)
    monkeypatch.setattr("agent.integrations.jenkins.get_all_jobs",
                        lambda: [JenkinsJob(name="infragpt"), JenkinsJob(name="folder", is_folder=True)])
    monkeypatch.setattr("agent.integrations.jenkins.get_build_history", lambda job, count: [
        BuildInfo(number=22, status="FAILURE", timestamp=ms(NOW - timedelta(hours=3))),
        BuildInfo(number=21, status="SUCCESS", timestamp=ms(NOW - timedelta(hours=5))),
        BuildInfo(number=18, status="FAILURE", timestamp=ms(NOW - timedelta(days=3))),
    ])
    s = ds._jenkins(NOW)
    assert s.status == "warn" and len([line for line in s.lines if "❌" in line]) == 1
    assert "#22" in s.lines[1] and "3h ago" in s.lines[1]


def test_jenkins_quiet_day(monkeypatch):
    monkeypatch.setattr("agent.integrations.jenkins.get_all_jobs", lambda: [JenkinsJob(name="infragpt")])
    monkeypatch.setattr("agent.integrations.jenkins.get_build_history", lambda job, count: [])
    assert ds._jenkins(NOW).status == "ok"


# --- github -------------------------------------------------------------------

def test_github_counts_only_recent_failures(monkeypatch):
    monkeypatch.setattr("agent.integrations.github_actions.resolve_repo", lambda r: "o/r")
    monkeypatch.setattr("agent.integrations.github_actions.list_runs", lambda repo, limit, failed_only: [
        {"id": 1, "workflow": "CI", "branch": "main", "created_at": "2026-09-29T01:00:00Z"},
        {"id": 2, "workflow": "CI", "branch": "old", "created_at": "2026-09-20T01:00:00Z"},
    ])
    s = ds._github(NOW)
    assert s.status == "warn" and len(s.lines) == 2 and "run 1" in s.lines[1]


# --- aws ----------------------------------------------------------------------

def _spend(monkeypatch, spike):
    monkeypatch.setattr("agent.integrations.aws_cost.get_spend_anomalies", lambda days: {
        "daily_totals": [{"date": "2026-09-28", "amount": 30.0}], "mean_daily": 15.0,
        "anomaly_days": [{"date": "2026-09-28"}] if spike else []})


def test_aws_normal_day_is_ok(monkeypatch):
    _spend(monkeypatch, spike=False)
    s = ds._aws(NOW)
    assert s.status == "ok" and "$30.00 vs $15.00/day avg (+100%)" in s.lines[0]


def test_aws_spike_warns(monkeypatch):
    _spend(monkeypatch, spike=True)
    assert ds._aws(NOW).status == "warn"


def test_aws_no_data_is_error(monkeypatch):
    monkeypatch.setattr("agent.integrations.aws_cost.get_spend_anomalies", lambda days: {"daily_totals": []})
    assert ds._aws(NOW).status == "error"


# --- approvals, backups -------------------------------------------------------

def test_pending_approvals_warn(monkeypatch):
    monkeypatch.setattr("agent.core.approvals.expire_stale", lambda: 0)
    monkeypatch.setattr("agent.core.approvals.list_actions",
                        lambda status, limit: [SimpleNamespace(id="abc", summary="restart pod x")])
    s = ds._approvals(NOW)
    assert s.status == "warn" and "abc" in s.lines[1]


def _backup(hours_ago, pre=False):
    return SimpleNamespace(created=NOW - timedelta(hours=hours_ago), size=940_000, pre_restore=pre)


def test_recent_backup_ok_and_pre_restore_ignored(monkeypatch):
    monkeypatch.setattr("agent.core.backup.list_backups", lambda: [_backup(1, pre=True), _backup(4)])
    s = ds._backups(NOW)
    assert s.status == "ok" and "4h ago" in s.lines[0] and "1 kept" in s.lines[0]


def test_stale_or_missing_backup_warns(monkeypatch):
    monkeypatch.setattr("agent.core.backup.list_backups", lambda: [_backup(30)])
    assert ds._backups(NOW).status == "warn"
    monkeypatch.setattr("agent.core.backup.list_backups", lambda: [])
    assert ds._backups(NOW).status == "warn"


# --- whole message --------------------------------------------------------------

def _fake_sections(monkeypatch, statuses):
    fns = []
    for i, st in enumerate(statuses):
        def fn(now, st=st, i=i):
            if st == "raise":
                raise RuntimeError("AccessDenied")
            return Section(f"S{i}", st, ["line"])
        fns.append(fn)
    monkeypatch.setattr(ds, "SECTIONS", fns)
    monkeypatch.setattr(ds, "_TITLES", {fn: f"S{i}" for i, fn in enumerate(fns)})


def test_one_broken_source_does_not_sink_the_summary(monkeypatch):
    _fake_sections(monkeypatch, ["ok", "raise", "ok"])
    out = ds.build_summary(NOW)
    assert [s.status for s in out] == ["ok", "error", "ok"]
    assert "AccessDenied" in out[1].lines[0]
    assert ds.headline(out) == "Couldn't check: S1"


def test_all_clear_headline(monkeypatch):
    _fake_sections(monkeypatch, ["ok", "ok"])
    assert ds.headline(ds.build_summary(NOW)) == "All clear."


def test_blocks_have_header_and_one_block_per_section(monkeypatch):
    _fake_sections(monkeypatch, ["ok", "warn"])
    blocks = ds.render_blocks(ds.build_summary(NOW), NOW)
    assert blocks[0]["type"] == "header"
    assert sum(1 for b in blocks if b["type"] == "section") == 1 + 2   # headline + 2 sections


# --- sending ------------------------------------------------------------------

@pytest.fixture
def slack(monkeypatch):
    sent = []
    monkeypatch.setattr("agent.integrations.slack.is_configured", lambda: True)
    monkeypatch.setattr("agent.integrations.slack.send_alert_blocks", lambda blocks: sent.append(blocks) or True)
    _fake_sections(monkeypatch, ["ok"])
    return sent


def test_sends_at_most_once_a_day(slack):
    assert ds.send(NOW)["sent"] is True
    assert ds.send(NOW + timedelta(hours=2)) == {"sent": False, "reason": "already sent today"}
    assert len(slack) == 1


def test_next_day_sends_again(slack):
    ds.send(NOW)
    assert ds.send(NOW + timedelta(days=1))["sent"] is True


def test_force_sends_again_the_same_day(slack):
    ds.send(NOW)
    assert ds.send(NOW, force=True)["sent"] is True and len(slack) == 2


def test_failed_slack_send_can_retry_later_the_same_day(monkeypatch, slack):
    monkeypatch.setattr("agent.integrations.slack.send_alert_blocks", lambda blocks: False)
    assert ds.send(NOW)["sent"] is False
    monkeypatch.setattr("agent.integrations.slack.send_alert_blocks", lambda blocks: True)
    assert ds.send(NOW)["sent"] is True       # the failure didn't consume today's slot


def test_mcp_tool_reports_without_sending(monkeypatch):
    from agent.mcp_server import daily_summary
    _fake_sections(monkeypatch, ["ok", "warn"])
    monkeypatch.setattr("agent.integrations.slack.send_alert_blocks",
                        lambda blocks: (_ for _ in ()).throw(AssertionError("sent from MCP")))
    out = asyncio.run(daily_summary())
    assert out["headline"] == "Needs attention: S1" and len(out["sections"]) == 2
