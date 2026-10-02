"""The daemon's pod watcher must say something when kubectl is actually
down, not just silently skip the cycle forever — that used to make a real
outage look identical to "nothing to heal". Covers the alert-after-N-failures
threshold, the cooldown that stops it repeating, the recovery notice, and the
matching fix in the MCP k8s_scan tool (unreachable vs. no problems).
"""
import asyncio

import pytest

from agent.core import daemon as daemon_mod
from agent.core.daemon import HealingDaemon, _POD_WATCHER_ALERT_AFTER
from agent.integrations import daemon_db


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_db, "_DB_PATH", tmp_path / "daemon.db")


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr("agent.integrations.slack.send_alert_generic",
                        lambda **k: sent.append(k) or True)
    return sent


def _run_n_polls(d: HealingDaemon, n: int, kubectl_results) -> None:
    """Drive _pod_watcher for exactly `n` iterations: each call to _sleep()
    marks one iteration done and stops the loop once `n` are reached — the
    same shape as the real loop's `while self.running: ... self._sleep(30)`.
    """
    calls = {"i": 0}

    def fake_run_kubectl(cmd):
        result = kubectl_results[min(calls["i"], len(kubectl_results) - 1)]
        return result

    def fake_sleep(seconds):
        calls["i"] += 1
        if calls["i"] >= n:
            d.running = False

    import agent.integrations.kubectl as kubectl_mod
    from unittest.mock import patch
    d.running = True
    with patch.object(kubectl_mod, "run_kubectl", side_effect=fake_run_kubectl), \
         patch.object(d, "_sleep", side_effect=fake_sleep):
        d._pod_watcher()


class _Result:
    def __init__(self, success, output="{}", error=None):
        self.success, self.output, self.error = success, output, error


def test_no_alert_before_the_threshold(alerts):
    d = HealingDaemon()
    fail = _Result(False, error="connection refused")
    _run_n_polls(d, n=_POD_WATCHER_ALERT_AFTER - 1, kubectl_results=[fail])
    assert alerts == []


def test_alert_fires_exactly_at_the_threshold(alerts):
    d = HealingDaemon()
    fail = _Result(False, error="connection refused")
    _run_n_polls(d, n=_POD_WATCHER_ALERT_AFTER, kubectl_results=[fail])
    assert len(alerts) == 1
    assert "blind" in alerts[0]["title"].lower()
    assert "connection refused" in alerts[0]["message"]


def test_alert_does_not_repeat_every_poll_after_the_threshold(alerts):
    d = HealingDaemon()
    fail = _Result(False, error="connection refused")
    _run_n_polls(d, n=_POD_WATCHER_ALERT_AFTER + 10, kubectl_results=[fail])
    assert len(alerts) == 1     # still down at poll 15, but only alerted once


def test_a_restart_mid_outage_does_not_re_alert_within_cooldown(alerts):
    """A fresh HealingDaemon (e.g. after a crash/restart) has its in-memory
    consecutive_failures reset to 0, so without the daemon_db cooldown it
    would hit the threshold and alert again immediately."""
    fail = _Result(False, error="connection refused")
    _run_n_polls(HealingDaemon(), n=_POD_WATCHER_ALERT_AFTER, kubectl_results=[fail])
    assert len(alerts) == 1
    _run_n_polls(HealingDaemon(), n=_POD_WATCHER_ALERT_AFTER, kubectl_results=[fail])
    assert len(alerts) == 1     # the restart's own threshold-hit was suppressed


def test_a_single_blip_does_not_alert(alerts):
    """One bad poll among good ones must not trip the threshold — this isn't
    about being flaky, it's about being down for a sustained stretch."""
    d = HealingDaemon()
    ok = _Result(True, output="{}")
    fail = _Result(False, error="timeout")
    _run_n_polls(d, n=6, kubectl_results=[ok, fail, ok, ok, ok, ok])
    assert alerts == []


def test_recovery_alert_after_sustained_failure(alerts):
    d = HealingDaemon()
    ok = _Result(True, output="{}")
    fail = _Result(False, error="connection refused")
    sequence = [fail] * _POD_WATCHER_ALERT_AFTER + [ok]
    _run_n_polls(d, n=len(sequence), kubectl_results=sequence)
    titles = [a["title"] for a in alerts]
    assert titles == ["Pod healing is blind", "Pod healing recovered"]


# --- MCP k8s_scan: unreachable vs. no problems -------------------------------

def test_k8s_scan_reports_unreachable_instead_of_an_empty_clean_scan(monkeypatch):
    from agent import mcp_server

    monkeypatch.setattr("agent.integrations.kubectl.is_cluster_available", lambda: False)

    def boom():
        raise AssertionError("full_cluster_scan should not run when the cluster is unreachable")
    monkeypatch.setattr("agent.skills.k8s.K8sSkill.full_cluster_scan",
                        lambda self, namespace="all": boom())

    result = asyncio.run(mcp_server.k8s_scan())
    assert len(result) == 1
    assert result[0]["severity"] == "critical"
    assert "unreachable" in result[0]["description"].lower()


def test_k8s_scan_runs_normally_when_reachable(monkeypatch):
    from agent import mcp_server

    monkeypatch.setattr("agent.integrations.kubectl.is_cluster_available", lambda: True)
    monkeypatch.setattr("agent.skills.k8s.K8sSkill.full_cluster_scan",
                        lambda self, namespace="all": [])

    assert asyncio.run(mcp_server.k8s_scan()) == []
