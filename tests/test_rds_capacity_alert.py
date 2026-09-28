"""HealingDaemon._check_rds_capacity: only "critical" instances alert, and
each instance alerts at most once per week — forecast_storage_capacity()
re-evaluates every instance every night, so without that guard a slow
storage leak would page every single night until someone fixes it.
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


def _row(status, **over):
    row = {"id": "orders-db", "region": "ap-south-1", "allocated_storage_gb": 100,
           "free_gb_now": 20.0, "trend_gb_per_day": -2.0, "days_until_full": 10.0,
           "status": status, "note": ""}
    row.update(over)
    return row


def test_alerts_on_a_critical_instance(monkeypatch, alert):
    monkeypatch.setattr("agent.integrations.rds.forecast_storage_capacity", lambda days=14: [_row("critical")])
    HealingDaemon()._check_rds_capacity()

    alert.assert_called_once()
    kwargs = alert.call_args.kwargs
    assert "orders-db" in kwargs["message"] and "10 days" in kwargs["message"]
    assert kwargs["fields"]["Instance"] == "orders-db"


@pytest.mark.parametrize("status", ["stable", "watch", "not_enough_data", "no_data", "unknown"])
def test_no_alert_for_non_critical_statuses(monkeypatch, alert, status):
    monkeypatch.setattr("agent.integrations.rds.forecast_storage_capacity", lambda days=14: [_row(status)])
    HealingDaemon()._check_rds_capacity()
    alert.assert_not_called()


def test_the_same_instance_does_not_alert_again_within_a_week(monkeypatch, alert):
    monkeypatch.setattr("agent.integrations.rds.forecast_storage_capacity", lambda days=14: [_row("critical")])
    d = HealingDaemon()
    d._check_rds_capacity()
    d._check_rds_capacity()   # the nightly check runs again the very next night
    assert alert.call_count == 1


def test_force_bypasses_the_weekly_cooldown(monkeypatch, alert):
    monkeypatch.setattr("agent.integrations.rds.forecast_storage_capacity", lambda days=14: [_row("critical")])
    d = HealingDaemon()
    d._check_rds_capacity()
    d._check_rds_capacity(force=True)
    assert alert.call_count == 2


def test_multiple_critical_instances_each_get_their_own_alert(monkeypatch, alert):
    monkeypatch.setattr("agent.integrations.rds.forecast_storage_capacity", lambda days=14: [
        _row("critical", id="orders-db"), _row("critical", id="sessions-db"),
    ])
    HealingDaemon()._check_rds_capacity()
    assert alert.call_count == 2
    ids = {c.kwargs["fields"]["Instance"] for c in alert.call_args_list}
    assert ids == {"orders-db", "sessions-db"}


def test_one_instance_escalating_to_critical_does_not_block_alerting_another(monkeypatch, alert):
    monkeypatch.setattr("agent.integrations.rds.forecast_storage_capacity", lambda days=14: [_row("critical", id="a")])
    d = HealingDaemon()
    d._check_rds_capacity()
    monkeypatch.setattr("agent.integrations.rds.forecast_storage_capacity",
                        lambda days=14: [_row("critical", id="a"), _row("critical", id="b")])
    d._check_rds_capacity()   # "a" still in cooldown, "b" is new
    assert alert.call_count == 2
    assert alert.call_args.kwargs["fields"]["Instance"] == "b"


def test_an_exception_fetching_the_forecast_is_swallowed_not_raised(monkeypatch, alert):
    def boom(days=14):
        raise RuntimeError("AccessDenied")
    monkeypatch.setattr("agent.integrations.rds.forecast_storage_capacity", boom)
    HealingDaemon()._check_rds_capacity()   # must not raise
    alert.assert_not_called()


def test_a_slack_send_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr("agent.integrations.rds.forecast_storage_capacity", lambda days=14: [_row("critical")])
    monkeypatch.setattr("agent.integrations.slack.send_alert_generic",
                        MagicMock(side_effect=RuntimeError("webhook down")))
    HealingDaemon()._check_rds_capacity()   # must not raise
