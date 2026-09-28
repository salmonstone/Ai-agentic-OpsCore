"""HealingDaemon._check_spend_anomalies: only the most recent billed day can
trigger an alert, and each date alerts at most once — get_spend_anomalies()
always re-reports the last 30 days, so without that guard the same spike
would re-alert every night for weeks.
"""
from unittest.mock import MagicMock

import pytest

from agent.core.daemon import HealingDaemon
from agent.integrations import daemon_db


@pytest.fixture(autouse=True)
def isolated_daemon_db(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_db, "_DB_PATH", tmp_path / "daemon.db")


@pytest.fixture
def alert(monkeypatch):
    sent = MagicMock(return_value=True)
    monkeypatch.setattr("agent.integrations.slack.send_alert_generic", sent)
    return sent


def _anomalies(latest_is_anomaly: bool, date: str = "2026-09-27", mean: float = 20.0):
    totals = [{"date": "2026-09-25", "amount": 19.0},
              {"date": "2026-09-26", "amount": 21.0},
              {"date": date, "amount": 63.0 if latest_is_anomaly else 20.5}]
    anomaly_days = []
    if latest_is_anomaly:
        anomaly_days.append({
            "date": date, "amount": 63.0, "pct_above_mean": 215.0,
            "culprit_services": [
                {"service": "Amazon EC2", "amount": 40.0, "avg": 8.0, "delta": 32.0},
                {"service": "Amazon RDS", "amount": 15.0, "avg": 6.0, "delta": 9.0},
            ],
        })
    return {"daily_totals": totals, "mean_daily": mean, "stddev_daily": 5.0,
            "anomaly_days": anomaly_days, "trending_up": [], "trending_down": []}


def test_alerts_when_the_most_recent_day_is_anomalous(monkeypatch, alert):
    monkeypatch.setattr("agent.integrations.aws_cost.get_spend_anomalies",
                        lambda days=30: _anomalies(True))
    HealingDaemon()._check_spend_anomalies()

    alert.assert_called_once()
    kwargs = alert.call_args.kwargs
    assert "2026-09-27" in kwargs["message"] and "63.00" in kwargs["message"]
    assert "Amazon EC2" in kwargs["message"]
    assert kwargs["fields"]["Day"] == "2026-09-27"


def test_no_alert_when_the_most_recent_day_is_normal(monkeypatch, alert):
    monkeypatch.setattr("agent.integrations.aws_cost.get_spend_anomalies",
                        lambda days=30: _anomalies(False))
    HealingDaemon()._check_spend_anomalies()
    alert.assert_not_called()


def test_a_past_anomaly_that_is_no_longer_the_latest_day_does_not_alert(monkeypatch, alert):
    """The 30-day window still contains an old spike, but it's not today's
    story any more — only the most recent day should ever trigger."""
    data = _anomalies(False)
    data["anomaly_days"] = [{"date": "2026-09-10", "amount": 90.0, "pct_above_mean": 300.0,
                             "culprit_services": []}]
    monkeypatch.setattr("agent.integrations.aws_cost.get_spend_anomalies", lambda days=30: data)
    HealingDaemon()._check_spend_anomalies()
    alert.assert_not_called()


def test_the_same_date_never_alerts_twice(monkeypatch, alert):
    monkeypatch.setattr("agent.integrations.aws_cost.get_spend_anomalies",
                        lambda days=30: _anomalies(True))
    d = HealingDaemon()
    d._check_spend_anomalies()
    d._check_spend_anomalies()   # daemon restarted, or checked twice in the same window
    assert alert.call_count == 1


def test_force_bypasses_the_cooldown(monkeypatch, alert):
    monkeypatch.setattr("agent.integrations.aws_cost.get_spend_anomalies",
                        lambda days=30: _anomalies(True))
    d = HealingDaemon()
    d._check_spend_anomalies()
    d._check_spend_anomalies(force=True)
    assert alert.call_count == 2


def test_no_data_does_not_raise(monkeypatch, alert):
    monkeypatch.setattr("agent.integrations.aws_cost.get_spend_anomalies",
                        lambda days=30: {"daily_totals": [], "anomaly_days": [], "mean_daily": 0})
    HealingDaemon()._check_spend_anomalies()   # must not raise
    alert.assert_not_called()


def test_an_exception_from_cost_explorer_is_swallowed_not_raised(monkeypatch, alert):
    def boom(days=30):
        raise RuntimeError("AccessDenied: ce:GetCostAndUsage")
    monkeypatch.setattr("agent.integrations.aws_cost.get_spend_anomalies", boom)
    HealingDaemon()._check_spend_anomalies()   # must not raise
    alert.assert_not_called()


def test_a_slack_send_failure_still_lets_the_check_finish_without_raising(monkeypatch):
    monkeypatch.setattr("agent.integrations.aws_cost.get_spend_anomalies",
                        lambda days=30: _anomalies(True))
    monkeypatch.setattr("agent.integrations.slack.send_alert_generic",
                        MagicMock(side_effect=RuntimeError("webhook down")))
    HealingDaemon()._check_spend_anomalies()   # must not raise
