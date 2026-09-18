"""
CloudWatch metrics + alarms via boto3. No AI. No business logic.

The second time-series source, and the one that works without anything being
installed in the cluster: ALB latency and 5xx, RDS connections and CPU, EC2
saturation are all there by default on any AWS account. That makes it the
fallback when Prometheus is absent, and the only source for the managed parts
of the request path (ALB, RDS) that Prometheus usually cannot see.

Follows the AccessDenied-tolerant pattern already established in rds.py:
every function returns an empty result and logs, never raises.
Required IAM: cloudwatch:GetMetricData, cloudwatch:DescribeAlarms.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from agent.core.models import MetricPoint, MetricSeries, MetricSource
from agent.observability.logging import get_logger

log = get_logger(__name__)

_DEFAULT_REGION = "us-east-1"


def _region(region: str | None) -> str:
    if region:
        return region
    try:
        from agent.config import settings
        return settings.aws_region or settings.ec2_region or _DEFAULT_REGION
    except Exception:
        return _DEFAULT_REGION


def _is_access_denied(exc) -> bool:
    try:
        code = exc.response["Error"]["Code"]
    except Exception:
        return False
    return code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation")


def _client(region: str):
    """CloudWatch client via the shared session, or None if unavailable."""
    try:
        from agent.integrations.aws import get_aws_client
        return get_aws_client("cloudwatch", region_name=region)
    except Exception as e:
        log.warning("cloudwatch.client_failed", error=str(e)[:200])
        return None


def _pick_period(minutes: int) -> int:
    """CloudWatch charges per datapoint and caps a response at 100 800 points.
    Keep roughly 120 points across the window, on a period CloudWatch
    actually supports (it rejects anything that is not a multiple of 60 above
    60s)."""
    raw = max(60, int((minutes * 60) / 120))
    return int(round(raw / 60.0)) * 60


# ---------------------------------------------------------------------------
# Core metric fetch
# ---------------------------------------------------------------------------

def get_metric_series(namespace: str, metric_name: str, dimensions: dict[str, str],
                      minutes: int = 60, stat: str = "Average",
                      region: str = "", unit: str = "") -> MetricSeries | None:
    """One CloudWatch metric as a MetricSeries.

    namespace:  AWS/ApplicationELB, AWS/RDS, AWS/EC2, ...
    dimensions: e.g. {"LoadBalancer": "app/my-alb/50dc6c495c0c9188"}
    """
    region = _region(region)
    cw = _client(region)
    if cw is None:
        return None

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    period = _pick_period(minutes)

    query = {
        "Id": "m1",
        "MetricStat": {
            "Metric": {
                "Namespace": namespace,
                "MetricName": metric_name,
                "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()],
            },
            "Period": period,
            "Stat": stat,
        },
        "ReturnData": True,
    }

    try:
        resp = cw.get_metric_data(
            MetricDataQueries=[query],
            StartTime=start, EndTime=end,
            ScanBy="TimestampAscending",
        )
    except Exception as e:
        if _is_access_denied(e):
            log.warning("cloudwatch.access_denied", metric=metric_name,
                        hint="needs cloudwatch:GetMetricData")
        else:
            log.warning("cloudwatch.failed", metric=metric_name, error=str(e)[:200])
        return None

    results = resp.get("MetricDataResults") or []
    if not results:
        return None

    r = results[0]
    points = [
        MetricPoint(ts=ts.timestamp(), value=float(v))
        for ts, v in zip(r.get("Timestamps", []), r.get("Values", []))
    ]
    if not points:
        return None

    return MetricSeries(
        name=f"{namespace}/{metric_name}",
        labels={**dimensions, "stat": stat},
        points=points,
        unit=unit,
        source=MetricSource.CLOUDWATCH,
    )


# ---------------------------------------------------------------------------
# Request-path signals — the managed hops Prometheus cannot see
# ---------------------------------------------------------------------------

def get_alb_signals(load_balancer: str, minutes: int = 60,
                    region: str = "") -> dict[str, MetricSeries]:
    """RED signals for one ALB.

    load_balancer is the CloudWatch dimension form of the ARN tail, e.g.
    "app/my-alb/50dc6c495c0c9188" — not the full ARN.
    """
    dims = {"LoadBalancer": load_balancer}
    wanted = [
        ("requests",    "RequestCount",             "Sum",     "req"),
        ("errors_5xx",  "HTTPCode_Target_5XX_Count", "Sum",    "req"),
        ("elb_5xx",     "HTTPCode_ELB_5XX_Count",   "Sum",     "req"),
        ("latency",     "TargetResponseTime",       "Average", "s"),
        ("latency_p99", "TargetResponseTime",       "p99",     "s"),
        ("unhealthy",   "UnHealthyHostCount",       "Average", "hosts"),
        ("healthy",     "HealthyHostCount",         "Average", "hosts"),
    ]
    out: dict[str, MetricSeries] = {}
    for key, metric, stat, unit in wanted:
        s = get_metric_series("AWS/ApplicationELB", metric, dims,
                              minutes=minutes, stat=stat, region=region, unit=unit)
        if s is not None:
            out[key] = s
    return out


def get_rds_signals(db_instance: str, minutes: int = 60,
                    region: str = "") -> dict[str, MetricSeries]:
    """Saturation signals for one RDS instance — the hops a pod-level view
    misses entirely, and the usual real cause of 'the app is slow'."""
    dims = {"DBInstanceIdentifier": db_instance}
    wanted = [
        ("cpu",             "CPUUtilization",      "Average", "%"),
        ("connections",     "DatabaseConnections", "Average", "conns"),
        ("free_storage",    "FreeStorageSpace",    "Average", "bytes"),
        ("freeable_memory", "FreeableMemory",      "Average", "bytes"),
        ("read_latency",    "ReadLatency",         "Average", "s"),
        ("write_latency",   "WriteLatency",        "Average", "s"),
        ("queue_depth",     "DiskQueueDepth",      "Average", "ops"),
    ]
    out: dict[str, MetricSeries] = {}
    for key, metric, stat, unit in wanted:
        s = get_metric_series("AWS/RDS", metric, dims,
                              minutes=minutes, stat=stat, region=region, unit=unit)
        if s is not None:
            out[key] = s
    return out


def get_ec2_signals(instance_id: str, minutes: int = 60,
                    region: str = "") -> dict[str, MetricSeries]:
    """Node-level saturation straight from the hypervisor — survives a node
    whose kubelet/metrics-server has stopped answering."""
    dims = {"InstanceId": instance_id}
    wanted = [
        ("cpu",         "CPUUtilization",   "Average", "%"),
        ("net_in",      "NetworkIn",        "Sum",     "bytes"),
        ("net_out",     "NetworkOut",       "Sum",     "bytes"),
        ("status_fail", "StatusCheckFailed", "Maximum", "count"),
    ]
    out: dict[str, MetricSeries] = {}
    for key, metric, stat, unit in wanted:
        s = get_metric_series("AWS/EC2", metric, dims,
                              minutes=minutes, stat=stat, region=region, unit=unit)
        if s is not None:
            out[key] = s
    return out


# ---------------------------------------------------------------------------
# Alarms — evidence a human already agreed was worth paging on
# ---------------------------------------------------------------------------

def get_alarms_in_alarm(region: str = "") -> list[dict]:
    """Every CloudWatch alarm currently in ALARM state."""
    region = _region(region)
    cw = _client(region)
    if cw is None:
        return []

    try:
        resp = cw.describe_alarms(StateValue="ALARM", MaxRecords=100)
    except Exception as e:
        if _is_access_denied(e):
            log.warning("cloudwatch.access_denied", check="describe_alarms")
        else:
            log.warning("cloudwatch.failed", check="describe_alarms", error=str(e)[:200])
        return []

    out = []
    for a in resp.get("MetricAlarms", []) or []:
        updated = a.get("StateUpdatedTimestamp")
        out.append({
            "name":        a.get("AlarmName", "?"),
            "metric":      a.get("MetricName", ""),
            "namespace":   a.get("Namespace", ""),
            "reason":      (a.get("StateReason") or "")[:300],
            "since":       updated.isoformat() if updated else "",
            "ts":          updated.timestamp() if updated else 0.0,
            "dimensions":  {d["Name"]: d["Value"] for d in a.get("Dimensions", []) or []},
        })
    return out


def check_connection(region: str = "") -> dict:
    """Shaped like prometheus.check_connection / aws_auth_status."""
    region = _region(region)
    cw = _client(region)
    if cw is None:
        return {"connected": False, "region": region,
                "error": "Could not build a CloudWatch client — check AWS auth (`agent aws auth-status`)."}
    try:
        cw.describe_alarms(MaxRecords=1)
        return {"connected": True, "region": region}
    except Exception as e:
        denied = _is_access_denied(e)
        return {
            "connected": False, "region": region,
            "error": ("Missing cloudwatch:DescribeAlarms permission."
                      if denied else str(e)[:200]),
        }
