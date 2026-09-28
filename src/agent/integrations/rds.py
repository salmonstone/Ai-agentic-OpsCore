"""RDS/Aurora health monitoring via boto3.

All functions handle AccessDenied gracefully.
Required IAM: rds:DescribeDBInstances, rds:DescribeDBClusters,
              cloudwatch:GetMetricStatistics, rds:DescribeEvents
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent.integrations.aws import get_aws_client
from agent.observability.logging import get_logger

log = get_logger(__name__)

_GB = 1024.0 ** 3
_MB = 1024.0 ** 2

_DEFAULT_REGION = "us-east-1"


def _region(region: str | None) -> str:
    """Resolve region from arg, config, or us-east-1 default."""
    if region:
        return region
    try:
        from agent.config import settings
        return settings.aws_region or settings.ec2_region or _DEFAULT_REGION
    except Exception:
        return _DEFAULT_REGION


def _is_access_denied(exc) -> bool:
    """True if a botocore ClientError is a permissions failure."""
    try:
        code = exc.response["Error"]["Code"]
    except Exception:
        return False
    return code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation")


def _import_boto3():
    """Import boto3 + ClientError, or return (None, None) if unavailable."""
    try:
        import boto3
        from botocore.exceptions import ClientError
        return boto3, ClientError
    except ImportError:
        log.warning("rds.boto3_missing", hint="run: uv add boto3")
        return None, None


# ===========================================================================
# RDS instances
# IAM: rds:DescribeDBInstances
# ===========================================================================

def get_rds_instances(region: str = _DEFAULT_REGION) -> list[dict]:
    """List all RDS instances with class, engine, status, endpoint, tags."""
    region = _region(region)
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return []

    try:
        rds = get_aws_client("rds", region_name=region)
    except Exception as e:
        log.warning("rds.failed", check="rds_client", error=str(e))
        return []

    try:
        result = rds.describe_db_instances()
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("rds.access_denied", check="describe_instances", error=str(e))
            return []
        log.warning("rds.failed", check="describe_instances", error=str(e))
        return []
    except Exception as e:
        log.warning("rds.failed", check="describe_instances", error=str(e))
        return []

    instances: list[dict] = []
    for inst in result.get("DBInstances", []):
        endpoint = inst.get("Endpoint") or {}
        tag_list = inst.get("TagList", []) or []
        instances.append({
            "id": inst.get("DBInstanceIdentifier", ""),
            "class": inst.get("DBInstanceClass", ""),
            "engine": inst.get("Engine", ""),
            "status": inst.get("DBInstanceStatus", ""),
            "endpoint": endpoint.get("Address", ""),
            "port": endpoint.get("Port", 0),
            "multi_az": bool(inst.get("MultiAZ", False)),
            "storage_gb": inst.get("AllocatedStorage", 0) or 0,
            "storage_type": inst.get("StorageType", ""),
            "region": region,
            "tags": {t["Key"]: t["Value"] for t in tag_list if "Key" in t},
        })
    return instances


# ===========================================================================
# CloudWatch metrics for an instance
# IAM: cloudwatch:GetMetricStatistics
# ===========================================================================

def get_rds_metrics(instance_id: str, region: str = _DEFAULT_REGION) -> dict:
    """Fetch last-1h CloudWatch metrics (period=300) for an RDS instance."""
    region = _region(region)
    default = {
        "cpu_pct": 0.0,
        "connections_avg": 0.0,
        "connections_max": 0.0,
        "free_storage_gb": 0.0,
        "read_latency_ms": 0.0,
        "write_latency_ms": 0.0,
        "freeable_memory_mb": 0.0,
        "replica_lag_sec": 0.0,
    }
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return default

    try:
        cw = get_aws_client("cloudwatch", region_name=region)
    except Exception as e:
        log.warning("rds.failed", check="cloudwatch_client", error=str(e))
        return default

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)
    dims = [{"Name": "DBInstanceIdentifier", "Value": instance_id}]

    def _fetch(metric: str, stat: str) -> float:
        """Return one statistic for a metric, or 0.0 on any failure/no data."""
        try:
            resp = cw.get_metric_statistics(
                Namespace="AWS/RDS",
                MetricName=metric,
                Dimensions=dims,
                StartTime=start,
                EndTime=end,
                Period=300,
                Statistics=["Average", "Maximum", "Minimum"],
            )
        except ClientError as e:
            if _is_access_denied(e):
                log.warning("rds.access_denied", check="metric_" + metric, error=str(e))
            else:
                log.warning("rds.failed", check="metric_" + metric, error=str(e))
            return 0.0
        except Exception as e:
            log.warning("rds.failed", check="metric_" + metric, error=str(e))
            return 0.0

        points = resp.get("Datapoints", [])
        if not points:
            return 0.0
        if stat == "Average":
            vals = [p["Average"] for p in points if "Average" in p]
            return sum(vals) / len(vals) if vals else 0.0
        if stat == "Maximum":
            vals = [p["Maximum"] for p in points if "Maximum" in p]
            return max(vals) if vals else 0.0
        if stat == "Minimum":
            vals = [p["Minimum"] for p in points if "Minimum" in p]
            return min(vals) if vals else 0.0
        return 0.0

    cpu = _fetch("CPUUtilization", "Average")
    conn_avg = _fetch("DatabaseConnections", "Average")
    conn_max = _fetch("DatabaseConnections", "Maximum")
    free_storage_bytes = _fetch("FreeStorageSpace", "Minimum")
    read_latency_s = _fetch("ReadLatency", "Average")
    write_latency_s = _fetch("WriteLatency", "Average")
    free_mem_bytes = _fetch("FreeableMemory", "Minimum")
    replica_lag = _fetch("ReplicaLag", "Average")

    return {
        "cpu_pct": round(cpu, 1),
        "connections_avg": round(conn_avg, 1),
        "connections_max": round(conn_max, 1),
        "free_storage_gb": round(free_storage_bytes / _GB, 2),
        # CloudWatch reports RDS latency in seconds -> convert to ms.
        "read_latency_ms": round(read_latency_s * 1000.0, 2),
        "write_latency_ms": round(write_latency_s * 1000.0, 2),
        "freeable_memory_mb": round(free_mem_bytes / _MB, 1),
        "replica_lag_sec": round(replica_lag, 1),
    }


# ===========================================================================
# Storage capacity forecast
# IAM: cloudwatch:GetMetricStatistics (same permission as get_rds_metrics)
# ===========================================================================

_MIN_POINTS_FOR_TREND = 4          # fewer days of data than this and we don't guess
_STABLE_SLOPE_GB_PER_DAY = 0.01    # smaller than this and it's noise, not a real trend


def _linear_trend(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares slope and intercept for (x, y) points — x is a day
    index, y is free storage in GB. No numpy dependency for one line of math."""
    n = len(points)
    sum_x = sum(x for x, _ in points)
    sum_y = sum(y for _, y in points)
    sum_xx = sum(x * x for x, _ in points)
    sum_xy = sum(x * y for x, y in points)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0.0, (sum_y / n if n else 0.0)
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def forecast_storage_capacity(region: str = "", days: int = 14) -> list[dict]:
    """Project when each RDS instance's free storage hits zero, from a plain
    linear trend over the last `days` days of CloudWatch FreeStorageSpace.

    Only ever extrapolates a SHRINKING trend: flat or growing free space
    gets days_until_full=None rather than a nonsensical forecast in the
    wrong direction. Needs at least _MIN_POINTS_FOR_TREND days of data, or
    it says so instead of guessing from too few points.
    """
    region = _region(region)
    instances = get_rds_instances(region)
    if not instances:
        return []

    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return []
    try:
        cw = get_aws_client("cloudwatch", region_name=region)
    except Exception as e:
        log.warning("rds.failed", check="cloudwatch_client", error=str(e))
        return []

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    out: list[dict] = []

    for inst in instances:
        row = {
            "id": inst["id"], "region": region, "allocated_storage_gb": inst["storage_gb"],
            "free_gb_now": 0.0, "trend_gb_per_day": 0.0,
            "days_until_full": None, "status": "unknown", "note": "",
        }
        try:
            resp = cw.get_metric_statistics(
                Namespace="AWS/RDS", MetricName="FreeStorageSpace",
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": inst["id"]}],
                StartTime=start, EndTime=end, Period=86400, Statistics=["Average"],
            )
        except ClientError as e:
            row["status"] = "no_data"
            row["note"] = "access denied" if _is_access_denied(e) else "CloudWatch query failed"
            out.append(row)
            continue
        except Exception as e:
            row["status"] = "no_data"
            row["note"] = str(e)[:120]
            out.append(row)
            continue

        points = sorted(resp.get("Datapoints", []), key=lambda p: p["Timestamp"])
        series = [(i, p["Average"] / _GB) for i, p in enumerate(points)]
        if not series:
            row["status"] = "no_data"
            row["note"] = "no CloudWatch data points in this window"
            out.append(row)
            continue

        row["free_gb_now"] = round(series[-1][1], 2)

        if len(series) < _MIN_POINTS_FOR_TREND:
            row["status"] = "not_enough_data"
            row["note"] = f"only {len(series)} day(s) of data — need {_MIN_POINTS_FOR_TREND}+"
            out.append(row)
            continue

        slope, _intercept = _linear_trend(series)
        row["trend_gb_per_day"] = round(slope, 3)

        if slope >= -_STABLE_SLOPE_GB_PER_DAY:
            row["status"] = "stable"
            out.append(row)
            continue

        days_left = row["free_gb_now"] / abs(slope)
        row["days_until_full"] = round(days_left, 1)
        row["status"] = "critical" if days_left < 14 else ("watch" if days_left < 60 else "stable")
        out.append(row)

    return out


# ===========================================================================
# RDS events
# IAM: rds:DescribeEvents
# ===========================================================================

def get_rds_events(region: str = _DEFAULT_REGION, hours: int = 24) -> list[dict]:
    """Recent RDS events (failovers, restarts, maintenance) for db-instances."""
    region = _region(region)
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return []

    try:
        rds = get_aws_client("rds", region_name=region)
        result = rds.describe_events(Duration=hours * 60, SourceType="db-instance")
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("rds.access_denied", check="describe_events", error=str(e))
            return []
        log.warning("rds.failed", check="describe_events", error=str(e))
        return []
    except Exception as e:
        log.warning("rds.failed", check="describe_events", error=str(e))
        return []

    events: list[dict] = []
    for ev in result.get("Events", []):
        date = ev.get("Date")
        events.append({
            "source": ev.get("SourceIdentifier", ""),
            "message": ev.get("Message", ""),
            "time": date.isoformat() if date else "",
        })
    return events


# ===========================================================================
# Aurora clusters
# IAM: rds:DescribeDBClusters
# ===========================================================================

def get_aurora_clusters(region: str = _DEFAULT_REGION) -> list[dict]:
    """List Aurora clusters with members and reader/writer endpoints."""
    region = _region(region)
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return []

    try:
        rds = get_aws_client("rds", region_name=region)
        result = rds.describe_db_clusters()
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("rds.access_denied", check="describe_clusters", error=str(e))
            return []
        log.warning("rds.failed", check="describe_clusters", error=str(e))
        return []
    except Exception as e:
        log.warning("rds.failed", check="describe_clusters", error=str(e))
        return []

    clusters: list[dict] = []
    for cl in result.get("DBClusters", []):
        clusters.append({
            "id": cl.get("DBClusterIdentifier", ""),
            "engine": cl.get("Engine", ""),
            "status": cl.get("Status", ""),
            "members": len(cl.get("DBClusterMembers", []) or []),
            "reader_endpoint": cl.get("ReaderEndpoint", ""),
            "writer_endpoint": cl.get("Endpoint", ""),
            "multi_az": bool(cl.get("MultiAZ", False)),
        })
    return clusters


# ===========================================================================
# Health analysis (pure -- no boto3)
# ===========================================================================

def analyze_rds_health(instance_id: str, metrics: dict, instance_info: dict) -> list[dict]:
    """Analyze metrics and return a list of issue dicts."""
    issues: list[dict] = []

    cpu = metrics.get("cpu_pct", 0.0)
    if cpu > 80:
        severity = "critical" if cpu > 95 else "warning"
        issues.append({
            "type": "HIGH_CPU",
            "severity": severity,
            "detail": f"CPU at {cpu:.0f}%",
            "fix": "Consider upgrading instance class or optimizing queries",
        })

    free_storage = metrics.get("free_storage_gb", 0.0)
    if free_storage < 5:
        new_storage = (instance_info.get("storage_gb", 0) or 0) + 20
        issues.append({
            "type": "LOW_STORAGE",
            "severity": "critical",
            "detail": f"Only {free_storage:.1f}GB free",
            "fix": (
                f"Run: aws rds modify-db-instance "
                f"--db-instance-identifier {instance_id} "
                f"--allocated-storage {new_storage}"
            ),
        })

    conn_max = metrics.get("connections_max", 0.0)
    if conn_max > 80:
        issues.append({
            "type": "HIGH_CONNECTIONS",
            "severity": "warning",
            "detail": f"Max {conn_max:.0f} connections",
            "fix": "Enable RDS Proxy or increase max_connections parameter",
        })

    read_latency = metrics.get("read_latency_ms", 0.0)
    if read_latency > 20:
        issues.append({
            "type": "HIGH_READ_LATENCY",
            "severity": "warning",
            "detail": f"Read latency {read_latency:.1f}ms",
            "fix": "Check for missing indexes or add read replica",
        })

    replica_lag = metrics.get("replica_lag_sec", 0.0)
    if replica_lag > 60:
        issues.append({
            "type": "REPLICA_LAG",
            "severity": "warning",
            "detail": f"Replica {replica_lag:.0f}s behind",
            "fix": "Check write load on primary, consider reducing replication",
        })

    return issues
