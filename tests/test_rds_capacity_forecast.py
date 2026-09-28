"""forecast_storage_capacity: a plain linear trend over CloudWatch history,
projecting when RDS storage runs out. The core safety property is that it
must never forecast in the wrong direction — flat or growing free space is
never reported as "days until full".
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from agent.integrations import rds


def _point(days_ago: int, free_gb: float) -> dict:
    return {"Timestamp": datetime.now(timezone.utc) - timedelta(days=days_ago),
            "Average": free_gb * rds._GB}


@pytest.fixture
def fake_cloudwatch(monkeypatch):
    """Install a fake `get_aws_client("cloudwatch", ...)` returning canned datapoints."""
    def install(datapoints):
        cw = MagicMock()
        cw.get_metric_statistics.return_value = {"Datapoints": datapoints}
        monkeypatch.setattr(rds, "get_aws_client", lambda service, region_name=None: cw)
        return cw
    return install


@pytest.fixture(autouse=True)
def one_instance(monkeypatch):
    monkeypatch.setattr(rds, "get_rds_instances",
                        lambda region="": [{"id": "orders-db", "storage_gb": 100, "region": "ap-south-1"}])


# --- _linear_trend -----------------------------------------------------

def test_linear_trend_of_a_perfect_line():
    slope, intercept = rds._linear_trend([(0, 100), (1, 90), (2, 80), (3, 70)])
    assert slope == pytest.approx(-10, abs=0.01)
    assert intercept == pytest.approx(100, abs=0.01)


def test_linear_trend_of_flat_data_is_zero_slope():
    slope, _ = rds._linear_trend([(0, 50), (1, 50), (2, 50)])
    assert slope == pytest.approx(0, abs=0.001)


def test_linear_trend_single_point_does_not_divide_by_zero():
    slope, intercept = rds._linear_trend([(0, 42)])
    assert slope == 0.0 and intercept == 42.0


# --- forecast_storage_capacity ------------------------------------------

def test_a_steadily_shrinking_instance_gets_a_days_until_full_estimate(fake_cloudwatch):
    # 10 GB/day free, so 6 days of history: 100, 90, 80, ... 50 (oldest to newest below)
    fake_cloudwatch([_point(5, 100), _point(4, 90), _point(3, 80), _point(2, 70),
                     _point(1, 60), _point(0, 50)])
    (row,) = rds.forecast_storage_capacity(region="ap-south-1")
    assert row["id"] == "orders-db"
    assert row["free_gb_now"] == pytest.approx(50, abs=0.1)
    assert row["trend_gb_per_day"] == pytest.approx(-10, abs=0.1)
    assert row["days_until_full"] == pytest.approx(5, abs=0.2)
    assert row["status"] == "critical"


def test_growing_free_space_is_never_reported_as_days_until_full(fake_cloudwatch):
    fake_cloudwatch([_point(5, 50), _point(4, 55), _point(3, 60), _point(2, 65),
                     _point(1, 70), _point(0, 75)])
    (row,) = rds.forecast_storage_capacity()
    assert row["days_until_full"] is None
    assert row["status"] == "stable"


def test_flat_free_space_is_stable_not_a_division_artifact(fake_cloudwatch):
    fake_cloudwatch([_point(i, 80.0) for i in range(5, -1, -1)])
    (row,) = rds.forecast_storage_capacity()
    assert row["days_until_full"] is None
    assert row["status"] == "stable"


def test_tiny_noise_level_shrinkage_does_not_trigger_a_forecast(fake_cloudwatch):
    # A slope of -0.001 GB/day is noise, not a real trend.
    fake_cloudwatch([_point(5, 80.000), _point(4, 79.999), _point(3, 79.998),
                     _point(2, 79.997), _point(1, 79.996), _point(0, 79.995)])
    (row,) = rds.forecast_storage_capacity()
    assert row["status"] == "stable" and row["days_until_full"] is None


@pytest.mark.parametrize("days_left, expected_status", [(5, "critical"), (30, "watch"), (200, "stable")])
def test_status_thresholds(fake_cloudwatch, days_left, expected_status):
    daily_drop = 100.0 / days_left
    points = [_point(i, 100.0 - daily_drop * (5 - i)) for i in range(5, -1, -1)]
    fake_cloudwatch(points)
    (row,) = rds.forecast_storage_capacity()
    assert row["status"] == expected_status


def test_too_few_datapoints_reports_not_enough_data_instead_of_guessing(fake_cloudwatch):
    fake_cloudwatch([_point(1, 90), _point(0, 80)])   # only 2 points
    (row,) = rds.forecast_storage_capacity()
    assert row["status"] == "not_enough_data" and row["days_until_full"] is None


def test_no_datapoints_at_all_is_reported_not_raised(fake_cloudwatch):
    fake_cloudwatch([])
    (row,) = rds.forecast_storage_capacity()
    assert row["status"] == "no_data"


def test_no_rds_instances_returns_empty_list(monkeypatch):
    monkeypatch.setattr(rds, "get_rds_instances", lambda region="": [])
    assert rds.forecast_storage_capacity() == []


def test_a_cloudwatch_exception_is_reported_per_instance_not_raised(monkeypatch):
    monkeypatch.setattr(rds, "get_rds_instances",
                        lambda region="": [{"id": "db1", "storage_gb": 50, "region": "x"}])
    cw = MagicMock()
    cw.get_metric_statistics.side_effect = RuntimeError("throttled")
    monkeypatch.setattr(rds, "get_aws_client", lambda service, region_name=None: cw)
    (row,) = rds.forecast_storage_capacity()
    assert row["status"] == "no_data"


def test_datapoints_out_of_order_are_sorted_before_fitting(fake_cloudwatch):
    # Same shrinking series as the first test, but shuffled in the response.
    fake_cloudwatch([_point(2, 70), _point(5, 100), _point(0, 50), _point(4, 90),
                     _point(1, 60), _point(3, 80)])
    (row,) = rds.forecast_storage_capacity()
    assert row["trend_gb_per_day"] == pytest.approx(-10, abs=0.1)
