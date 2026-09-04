"""
AWS Cost analysis integrations — boto3.

All functions handle AccessDenied gracefully (returns empty data, logs warning).
Requires: boto3, and IAM permissions listed per function.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from agent.integrations.aws import get_aws_client
from agent.observability.logging import get_logger

log = get_logger(__name__)

# Cost Explorer is a global service that only answers in us-east-1.
_CE_REGION = "us-east-1"

# ---------------------------------------------------------------------------
# Approximate on-demand monthly cost ($) by instance type (us-east-1, 730h).
# Used only when a more precise source is unavailable.
# ---------------------------------------------------------------------------
_INSTANCE_MONTHLY: dict[str, float] = {
    "t3.nano":     4.0,
    "t3.micro":    8.0,
    "t3.small":    16.0,
    "t3.medium":   30.0,
    "t3.large":    60.0,
    "t3.xlarge":   120.0,
    "t3.2xlarge":  243.0,
    "t3a.medium":  27.0,
    "t3a.large":   55.0,
    "t3a.xlarge":  110.0,
    "m5.large":    70.0,
    "m5.xlarge":   140.0,
    "m5.2xlarge":  280.0,
    "m5.4xlarge":  560.0,
    "m6i.large":   70.0,
    "m6i.xlarge":  140.0,
    "m6i.2xlarge": 280.0,
    "c5.large":    62.0,
    "c5.xlarge":   124.0,
    "c5.2xlarge":  248.0,
    "r5.large":    92.0,
    "r5.xlarge":   184.0,
    "r5.2xlarge":  368.0,
}

# EBS / storage / network unit costs ($/month).
_EBS_GP2_PER_GB = 0.10
_EBS_GP3_PER_GB = 0.08
_SNAPSHOT_PER_GB = 0.05
_EIP_MONTHLY = 3.60
_IDLE_ALB_MONTHLY = 18.0
_STOPPED_INSTANCE_EBS_MONTHLY = 5.0
_LOGS_PER_GB = 0.50
_ECR_PER_GB = 0.10

_GB = 1024.0 ** 3


def _approx_instance_monthly(instance_type: str) -> float:
    """Best-effort monthly cost for an instance type, falling back to the
    shared EC2 pricing table, then a flat default."""
    if instance_type in _INSTANCE_MONTHLY:
        return _INSTANCE_MONTHLY[instance_type]
    try:
        from agent.integrations.ec2_pricing import HOURS_PER_MONTH, get_instance_price
        pricing = get_instance_price(instance_type)
        if pricing:
            return round(pricing["hourly"] * HOURS_PER_MONTH, 2)
    except Exception:
        pass
    return 50.0


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
        log.warning("aws.boto3_missing", hint="run: uv add boto3")
        return None, None


# ===========================================================================
# Cost Explorer — spend & forecast
# IAM: ce:GetCostAndUsage, ce:GetCostForecast
# ===========================================================================

def get_cost_and_usage(days: int = 30) -> dict:
    """Total spend by service, daily trend, MTD, forecast, last-month compare.

    Returns dict with keys: total, last_month, by_service, daily, forecast,
    month_change_pct. Returns zeroed defaults on permission/availability error.
    """
    default = {
        "total": 0.0,
        "last_month": 0.0,
        "by_service": {},
        "daily": [],
        "forecast": 0.0,
        "month_change_pct": 0.0,
    }
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return default

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    try:
        ce = get_aws_client("ce", region_name=_CE_REGION)
    except Exception as e:
        log.warning("aws.check_failed", check="cost_explorer_client", error=str(e))
        return default

    by_service: dict[str, float] = {}
    daily: list[dict] = []
    total = 0.0

    # --- spend by service + daily trend (one DAILY grouped query) ----------
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        for result in resp.get("ResultsByTime", []):
            day = result["TimePeriod"]["Start"]
            day_total = 0.0
            for group in result.get("Groups", []):
                svc = group["Keys"][0]
                amt = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if amt <= 0:
                    continue
                by_service[svc] = round(by_service.get(svc, 0.0) + amt, 4)
                day_total += amt
                total += amt
            daily.append({"date": day, "amount": round(day_total, 2)})
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("aws.access_denied", check="get_cost_and_usage", error=str(e))
            return default
        log.warning("aws.check_failed", check="get_cost_and_usage", error=str(e))
        return default
    except Exception as e:
        log.warning("aws.check_failed", check="get_cost_and_usage", error=str(e))
        return default

    by_service = {
        k: round(v, 2)
        for k, v in sorted(by_service.items(), key=lambda x: x[1], reverse=True)
        if v >= 0.01
    }

    # --- month-to-date -----------------------------------------------------
    mtd = 0.0
    month_start = today.replace(day=1)
    try:
        if month_start < today:
            r = ce.get_cost_and_usage(
                TimePeriod={"Start": month_start.isoformat(), "End": today.isoformat()},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )
            for result in r.get("ResultsByTime", []):
                mtd += float(result["Total"]["UnblendedCost"]["Amount"])
    except Exception as e:
        log.warning("aws.check_failed", check="mtd", error=str(e))

    # --- last full month (for compare) -------------------------------------
    last_month = 0.0
    first_this_month = today.replace(day=1)
    last_month_end = first_this_month
    last_month_start = (first_this_month - timedelta(days=1)).replace(day=1)
    try:
        r = ce.get_cost_and_usage(
            TimePeriod={
                "Start": last_month_start.isoformat(),
                "End": last_month_end.isoformat(),
            },
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
        )
        for result in r.get("ResultsByTime", []):
            last_month += float(result["Total"]["UnblendedCost"]["Amount"])
    except Exception as e:
        log.warning("aws.check_failed", check="last_month", error=str(e))

    # --- forecast for the rest of this month -------------------------------
    forecast = round(mtd, 2)
    try:
        # End of current month.
        if today.month == 12:
            month_end = today.replace(year=today.year + 1, month=1, day=1)
        else:
            month_end = today.replace(month=today.month + 1, day=1)
        if today < month_end:
            fc = ce.get_cost_forecast(
                TimePeriod={"Start": today.isoformat(), "End": month_end.isoformat()},
                Metric="UNBLENDED_COST",
                Granularity="MONTHLY",
            )
            remaining = float(fc["Total"]["Amount"])
            forecast = round(mtd + remaining, 2)
    except ClientError as e:
        if not _is_access_denied(e):
            log.warning("aws.check_failed", check="forecast", error=str(e))
    except Exception as e:
        log.warning("aws.check_failed", check="forecast", error=str(e))

    month_change_pct = 0.0
    if last_month > 0:
        month_change_pct = round((mtd - last_month) / last_month * 100, 1)

    return {
        "total": round(total, 2),
        "last_month": round(last_month, 2),
        "by_service": by_service,
        "daily": daily,
        "forecast": forecast,
        "month_change_pct": month_change_pct,
        "month_to_date": round(mtd, 2),
    }


# ===========================================================================
# EC2 running instances + CloudWatch CPU
# IAM: ec2:DescribeInstances, cloudwatch:GetMetricStatistics
# ===========================================================================

def get_ec2_instances() -> list[dict]:
    """Running EC2 instances with 7-day avg CPU and approximate monthly cost."""
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return []

    try:
        ec2 = get_aws_client("ec2")
        cw = get_aws_client("cloudwatch")
    except Exception as e:
        log.warning("aws.check_failed", check="ec2_client", error=str(e))
        return []

    try:
        resp = ec2.describe_instances(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        )
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("aws.access_denied", check="get_ec2_instances", error=str(e))
            return []
        log.warning("aws.check_failed", check="get_ec2_instances", error=str(e))
        return []
    except Exception as e:
        log.warning("aws.check_failed", check="get_ec2_instances", error=str(e))
        return []

    now = datetime.now(timezone.utc)
    instances: list[dict] = []
    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            iid = inst["InstanceId"]
            itype = inst.get("InstanceType", "unknown")
            name = ""
            for tag in inst.get("Tags", []):
                if tag.get("Key") == "Name":
                    name = tag.get("Value", "")
                    break
            launch = inst.get("LaunchTime")
            days_running = 0
            launch_str = ""
            if launch:
                launch_str = launch.isoformat()
                days_running = max(0, (now - launch).days)

            cpu_avg = _cpu_average_7d(cw, iid, ClientError)

            instances.append({
                "id": iid,
                "type": itype,
                "state": inst.get("State", {}).get("Name", "running"),
                "name": name,
                "launch_time": launch_str,
                "days_running": days_running,
                "cpu_avg_7d": cpu_avg,
                "monthly_cost": _approx_instance_monthly(itype),
            })
    return instances


def _cpu_average_7d(cw, instance_id: str, ClientError) -> float:
    """7-day average CPU utilization (%) for an instance, or 0.0 on failure."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    try:
        stats = cw.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start,
            EndTime=end,
            Period=3600,
            Statistics=["Average"],
        )
        points = stats.get("Datapoints", [])
        if not points:
            return 0.0
        return round(sum(p["Average"] for p in points) / len(points), 1)
    except ClientError as e:
        if not _is_access_denied(e):
            log.warning("aws.check_failed", check="cpu_average", error=str(e))
        return 0.0
    except Exception as e:
        log.warning("aws.check_failed", check="cpu_average", error=str(e))
        return 0.0


# ===========================================================================
# Idle resources
# IAM: ec2:DescribeVolumes, ec2:DescribeAddresses, ec2:DescribeSnapshots,
#      ec2:DescribeInstances, elasticloadbalancing:Describe*
# ===========================================================================

def get_idle_resources() -> dict:
    """Find unattached volumes, unused EIPs, old snapshots, idle LBs,
    and stopped instances. Each sub-check degrades independently."""
    result = {
        "unattached_volumes": [],
        "unused_eips": [],
        "old_snapshots": [],
        "idle_load_balancers": [],
        "stopped_instances": [],
        "total_monthly_waste": 0.0,
    }
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return result

    try:
        ec2 = get_aws_client("ec2")
    except Exception as e:
        log.warning("aws.check_failed", check="idle_ec2_client", error=str(e))
        ec2 = None

    waste = 0.0

    # --- unattached EBS volumes -------------------------------------------
    if ec2 is not None:
        try:
            resp = ec2.describe_volumes(
                Filters=[{"Name": "status", "Values": ["available"]}]
            )
            for vol in resp.get("Volumes", []):
                size = vol.get("Size", 0)
                vtype = vol.get("VolumeType", "gp2")
                per_gb = _EBS_GP3_PER_GB if vtype == "gp3" else _EBS_GP2_PER_GB
                cost = round(size * per_gb, 2)
                create = vol.get("CreateTime")
                result["unattached_volumes"].append({
                    "VolumeId": vol["VolumeId"],
                    "Size": size,
                    "VolumeType": vtype,
                    "CreateTime": create.isoformat() if create else "",
                    "cost_per_month": cost,
                })
                waste += cost
        except ClientError as e:
            if _is_access_denied(e):
                log.warning("aws.access_denied", check="describe_volumes", error=str(e))
            else:
                log.warning("aws.check_failed", check="describe_volumes", error=str(e))
        except Exception as e:
            log.warning("aws.check_failed", check="describe_volumes", error=str(e))

    # --- unused Elastic IPs ------------------------------------------------
    if ec2 is not None:
        try:
            resp = ec2.describe_addresses()
            for addr in resp.get("Addresses", []):
                if not addr.get("AssociationId"):
                    result["unused_eips"].append({
                        "AllocationId": addr.get("AllocationId", ""),
                        "PublicIp": addr.get("PublicIp", ""),
                        "cost_per_month": _EIP_MONTHLY,
                    })
                    waste += _EIP_MONTHLY
        except ClientError as e:
            if _is_access_denied(e):
                log.warning("aws.access_denied", check="describe_addresses", error=str(e))
            else:
                log.warning("aws.check_failed", check="describe_addresses", error=str(e))
        except Exception as e:
            log.warning("aws.check_failed", check="describe_addresses", error=str(e))

    # --- old snapshots (>90 days) -----------------------------------------
    if ec2 is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        try:
            paginator = ec2.get_paginator("describe_snapshots")
            for page in paginator.paginate(OwnerIds=["self"]):
                for snap in page.get("Snapshots", []):
                    start = snap.get("StartTime")
                    if not start or start >= cutoff:
                        continue
                    size = snap.get("VolumeSize", 0)
                    cost = round(size * _SNAPSHOT_PER_GB, 2)
                    age_days = (datetime.now(timezone.utc) - start).days
                    result["old_snapshots"].append({
                        "SnapshotId": snap["SnapshotId"],
                        "Size": size,
                        "StartTime": start.isoformat(),
                        "age_days": age_days,
                        "cost_per_month": cost,
                    })
                    waste += cost
        except ClientError as e:
            if _is_access_denied(e):
                log.warning("aws.access_denied", check="describe_snapshots", error=str(e))
            else:
                log.warning("aws.check_failed", check="describe_snapshots", error=str(e))
        except Exception as e:
            log.warning("aws.check_failed", check="describe_snapshots", error=str(e))

    # --- idle load balancers (no healthy targets) -------------------------
    try:
        elbv2 = get_aws_client("elbv2")
    except Exception as e:
        log.warning("aws.check_failed", check="elbv2_client", error=str(e))
        elbv2 = None

    if elbv2 is not None:
        try:
            lbs = elbv2.describe_load_balancers().get("LoadBalancers", [])
            for lb in lbs:
                arn = lb["LoadBalancerArn"]
                name = lb.get("LoadBalancerName", arn.split("/")[-1])
                if _lb_is_idle(elbv2, arn, ClientError):
                    result["idle_load_balancers"].append({
                        "LoadBalancerName": name,
                        "LoadBalancerArn": arn,
                        "Type": lb.get("Type", "application"),
                        "cost_per_month": _IDLE_ALB_MONTHLY,
                    })
                    waste += _IDLE_ALB_MONTHLY
        except ClientError as e:
            if _is_access_denied(e):
                log.warning("aws.access_denied", check="describe_load_balancers", error=str(e))
            else:
                log.warning("aws.check_failed", check="describe_load_balancers", error=str(e))
        except Exception as e:
            log.warning("aws.check_failed", check="describe_load_balancers", error=str(e))

    # --- stopped instances (still pay for EBS) ----------------------------
    if ec2 is not None:
        try:
            resp = ec2.describe_instances(
                Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]
            )
            for reservation in resp.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    name = ""
                    for tag in inst.get("Tags", []):
                        if tag.get("Key") == "Name":
                            name = tag.get("Value", "")
                            break
                    result["stopped_instances"].append({
                        "InstanceId": inst["InstanceId"],
                        "InstanceType": inst.get("InstanceType", "unknown"),
                        "Name": name,
                        "cost_per_month": _STOPPED_INSTANCE_EBS_MONTHLY,
                    })
                    waste += _STOPPED_INSTANCE_EBS_MONTHLY
        except ClientError as e:
            if _is_access_denied(e):
                log.warning("aws.access_denied", check="stopped_instances", error=str(e))
            else:
                log.warning("aws.check_failed", check="stopped_instances", error=str(e))
        except Exception as e:
            log.warning("aws.check_failed", check="stopped_instances", error=str(e))

    result["total_monthly_waste"] = round(waste, 2)
    return result


def _lb_is_idle(elbv2, lb_arn: str, ClientError) -> bool:
    """True if the LB has no healthy targets across all its target groups."""
    try:
        tgs = elbv2.describe_target_groups(LoadBalancerArn=lb_arn).get("TargetGroups", [])
    except Exception:
        return False
    if not tgs:
        return True
    for tg in tgs:
        try:
            health = elbv2.describe_target_health(
                TargetGroupArn=tg["TargetGroupArn"]
            ).get("TargetHealthDescriptions", [])
        except Exception:
            continue
        for desc in health:
            if desc.get("TargetHealth", {}).get("State") == "healthy":
                return False
    return True


# ===========================================================================
# EBS gp2 -> gp3 optimization
# IAM: ec2:DescribeVolumes
# ===========================================================================

def get_ebs_optimization() -> list[dict]:
    """Recommend gp2 -> gp3 upgrades (20% cheaper) for in-use volumes."""
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return []

    try:
        ec2 = get_aws_client("ec2")
        resp = ec2.describe_volumes(
            Filters=[{"Name": "status", "Values": ["in-use"]}]
        )
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("aws.access_denied", check="get_ebs_optimization", error=str(e))
            return []
        log.warning("aws.check_failed", check="get_ebs_optimization", error=str(e))
        return []
    except Exception as e:
        log.warning("aws.check_failed", check="get_ebs_optimization", error=str(e))
        return []

    out: list[dict] = []
    for vol in resp.get("Volumes", []):
        if vol.get("VolumeType") != "gp2":
            continue
        size = vol.get("Size", 0)
        savings = round(size * (_EBS_GP2_PER_GB - _EBS_GP3_PER_GB), 2)
        out.append({
            "VolumeId": vol["VolumeId"],
            "Size": size,
            "current_type": "gp2",
            "recommended_type": "gp3",
            "monthly_savings": savings,
        })
    return out


# ===========================================================================
# CloudWatch Logs cost
# IAM: logs:DescribeLogGroups
# ===========================================================================

def get_cloudwatch_logs_cost() -> dict:
    """Find log groups with no retention policy and estimate storage cost."""
    default = {
        "total_stored_gb": 0.0,
        "groups_no_retention": [],
        "estimated_monthly_cost": 0.0,
        "potential_savings": 0.0,
    }
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return default

    try:
        logs = get_aws_client("logs")
    except Exception as e:
        log.warning("aws.check_failed", check="logs_client", error=str(e))
        return default

    total_bytes = 0
    no_retention: list[dict] = []
    no_retention_bytes = 0
    try:
        paginator = logs.get_paginator("describe_log_groups")
        for page in paginator.paginate():
            for grp in page.get("logGroups", []):
                stored = grp.get("storedBytes", 0)
                total_bytes += stored
                if not grp.get("retentionInDays"):
                    gb = round(stored / _GB, 3)
                    no_retention_bytes += stored
                    no_retention.append({
                        "logGroupName": grp.get("logGroupName", ""),
                        "stored_gb": gb,
                        "cost_per_month": round(gb * _LOGS_PER_GB, 2),
                    })
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("aws.access_denied", check="describe_log_groups", error=str(e))
            return default
        log.warning("aws.check_failed", check="describe_log_groups", error=str(e))
        return default
    except Exception as e:
        log.warning("aws.check_failed", check="describe_log_groups", error=str(e))
        return default

    total_gb = round(total_bytes / _GB, 3)
    no_ret_gb = no_retention_bytes / _GB
    return {
        "total_stored_gb": total_gb,
        "groups_no_retention": no_retention,
        "estimated_monthly_cost": round(total_gb * _LOGS_PER_GB, 2),
        # Setting a 30-day retention typically reclaims ~70% of unbounded logs.
        "potential_savings": round(no_ret_gb * _LOGS_PER_GB * 0.7, 2),
    }


# ===========================================================================
# ECR waste — old untagged images
# IAM: ecr:DescribeRepositories, ecr:DescribeImages
# ===========================================================================

def get_ecr_waste() -> dict:
    """Find old untagged ECR images and estimate storage cost."""
    default = {
        "total_repos": 0,
        "old_untagged_images": [],
        "total_size_gb": 0.0,
        "estimated_cost": 0.0,
    }
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return default

    try:
        ecr = get_aws_client("ecr")
    except Exception as e:
        log.warning("aws.check_failed", check="ecr_client", error=str(e))
        return default

    try:
        repos = []
        paginator = ecr.get_paginator("describe_repositories")
        for page in paginator.paginate():
            repos.extend(page.get("repositories", []))
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("aws.access_denied", check="describe_repositories", error=str(e))
            return default
        log.warning("aws.check_failed", check="describe_repositories", error=str(e))
        return default
    except Exception as e:
        log.warning("aws.check_failed", check="describe_repositories", error=str(e))
        return default

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    old_images: list[dict] = []
    total_bytes = 0

    for repo in repos:
        name = repo.get("repositoryName", "")
        try:
            img_paginator = ecr.get_paginator("describe_images")
            for page in img_paginator.paginate(repositoryName=name):
                for img in page.get("imageDetails", []):
                    pushed = img.get("imagePushedAt")
                    if not pushed or pushed >= cutoff:
                        continue
                    tags = img.get("imageTags", [])
                    is_untagged = (not tags) or tags == ["<none>"]
                    if not is_untagged:
                        continue
                    size = img.get("imageSizeInBytes", 0)
                    total_bytes += size
                    old_images.append({
                        "repository": name,
                        "digest": img.get("imageDigest", ""),
                        "size_gb": round(size / _GB, 3),
                        "pushed_at": pushed.isoformat(),
                        "age_days": (datetime.now(timezone.utc) - pushed).days,
                    })
        except ClientError as e:
            if _is_access_denied(e):
                log.warning("aws.access_denied", check="describe_images", error=str(e))
            else:
                log.warning("aws.check_failed", check="describe_images", error=str(e))
        except Exception as e:
            log.warning("aws.check_failed", check="describe_images", error=str(e))

    total_gb = round(total_bytes / _GB, 3)
    return {
        "total_repos": len(repos),
        "old_untagged_images": old_images,
        "total_size_gb": total_gb,
        "estimated_cost": round(total_gb * _ECR_PER_GB, 2),
    }


# ===========================================================================
# Rightsizing recommendations
# IAM: ce:GetRightsizingRecommendation
# ===========================================================================

def get_rightsizing_recommendations() -> list[dict]:
    """EC2 rightsizing recommendations from Cost Explorer."""
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return []

    try:
        ce = get_aws_client("ce", region_name=_CE_REGION)
        resp = ce.get_rightsizing_recommendation(Service="AmazonEC2")
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("aws.access_denied", check="get_rightsizing", error=str(e))
            return []
        log.warning("aws.check_failed", check="get_rightsizing", error=str(e))
        return []
    except Exception as e:
        log.warning("aws.check_failed", check="get_rightsizing", error=str(e))
        return []

    out: list[dict] = []
    for rec in resp.get("RightsizingRecommendations", []):
        current = rec.get("CurrentInstance", {})
        iid = current.get("ResourceId", "")
        current_type = (
            current.get("ResourceDetails", {})
            .get("EC2ResourceDetails", {})
            .get("InstanceType", "")
        )
        savings = float(rec.get("EstimatedMonthlySavings", 0) or 0)

        recommended_type = ""
        action = rec.get("RightsizingType", "")
        if action == "Modify":
            targets = rec.get("ModifyRecommendationDetail", {}).get("TargetInstances", [])
            if targets:
                recommended_type = (
                    targets[0].get("ResourceDetails", {})
                    .get("EC2ResourceDetails", {})
                    .get("InstanceType", "")
                )
                if not savings:
                    savings = float(targets[0].get("EstimatedMonthlySavings", 0) or 0)
        elif action == "Terminate":
            recommended_type = "terminate"
            if not savings:
                savings = float(
                    rec.get("TerminateRecommendationDetail", {})
                    .get("EstimatedMonthlySavings", 0) or 0
                )

        out.append({
            "instance_id": iid,
            "current_type": current_type,
            "recommended_type": recommended_type or "review",
            "estimated_monthly_savings": round(savings, 2),
        })
    return out


# ===========================================================================
# Reserved vs on-demand
# IAM: ce:GetCostAndUsage, ce:GetSavingsPlansCoverage
# ===========================================================================

def get_reserved_vs_ondemand() -> dict:
    """On-demand EC2 spend, savings-plan coverage, and potential reservation
    savings (~35% of on-demand)."""
    default = {
        "ondemand_monthly": 0.0,
        "potential_reserved_savings": 0.0,
        "coverage_pct": 0.0,
    }
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return default

    try:
        ce = get_aws_client("ce", region_name=_CE_REGION)
    except Exception as e:
        log.warning("aws.check_failed", check="ce_client_ri", error=str(e))
        return default

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=30)

    ondemand = 0.0
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={
                "And": [
                    {"Dimensions": {"Key": "SERVICE",
                                    "Values": ["Amazon Elastic Compute Cloud - Compute"]}},
                    {"Dimensions": {"Key": "PURCHASE_TYPE",
                                    "Values": ["On Demand Instances"]}},
                ]
            },
        )
        for result in resp.get("ResultsByTime", []):
            ondemand += float(result["Total"]["UnblendedCost"]["Amount"])
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("aws.access_denied", check="ondemand_spend", error=str(e))
            return default
        log.warning("aws.check_failed", check="ondemand_spend", error=str(e))
        return default
    except Exception as e:
        log.warning("aws.check_failed", check="ondemand_spend", error=str(e))
        return default

    coverage_pct = 0.0
    try:
        cov = ce.get_savings_plans_coverage(
            TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
            Granularity="MONTHLY",
        )
        totals = cov.get("SavingsPlansCoverages", [])
        if totals:
            c = totals[0].get("Coverage", {})
            coverage_pct = round(float(c.get("CoveragePercentage", 0) or 0), 1)
    except Exception as e:
        log.warning("aws.check_failed", check="savings_plan_coverage", error=str(e))

    return {
        "ondemand_monthly": round(ondemand, 2),
        "potential_reserved_savings": round(ondemand * 0.35, 2),
        "coverage_pct": coverage_pct,
    }


# ===========================================================================
# Spend anomaly detection
# IAM: ce:GetCostAndUsage
# ===========================================================================

def get_spend_anomalies(days: int = 30) -> dict:
    """
    Detect spend anomalies using statistical analysis.
    Returns spikes (days > mean + 1.5*stddev) and trending services.
    Always uses us-east-1 for Cost Explorer.
    """
    default = {
        "daily_totals": [],
        "mean_daily": 0.0,
        "stddev_daily": 0.0,
        "anomaly_days": [],
        "trending_up": [],
        "trending_down": [],
    }
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return default

    try:
        ce = get_aws_client("ce", region_name=_CE_REGION)
    except Exception as e:
        log.warning("aws.check_failed", check="anomalies_ce_client", error=str(e))
        return default

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    # date -> {service: amount}
    per_day_service: dict[str, dict[str, float]] = {}
    daily_totals: list[dict] = []

    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("aws.access_denied", check="get_spend_anomalies", error=str(e))
            return default
        log.warning("aws.check_failed", check="get_spend_anomalies", error=str(e))
        return default
    except Exception as e:
        log.warning("aws.check_failed", check="get_spend_anomalies", error=str(e))
        return default

    for result in resp.get("ResultsByTime", []):
        day = result["TimePeriod"]["Start"]
        svc_map: dict[str, float] = {}
        day_total = 0.0
        for group in result.get("Groups", []):
            svc = group["Keys"][0]
            amt = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amt <= 0:
                continue
            svc_map[svc] = round(svc_map.get(svc, 0.0) + amt, 4)
            day_total += amt
        per_day_service[day] = svc_map
        daily_totals.append({"date": day, "amount": round(day_total, 2)})

    daily_totals.sort(key=lambda d: d["date"])
    amounts = [d["amount"] for d in daily_totals]
    if len(amounts) < 2:
        out = dict(default)
        out["daily_totals"] = daily_totals
        if amounts:
            out["mean_daily"] = round(amounts[0], 2)
        return out

    mean_daily = statistics.mean(amounts)
    stddev_daily = statistics.stdev(amounts)
    threshold = mean_daily + 1.5 * stddev_daily

    ordered_days = [d["date"] for d in daily_totals]

    # --- anomaly days + per-service culprits -------------------------------
    anomaly_days: list[dict] = []
    for idx, d in enumerate(daily_totals):
        if d["amount"] <= threshold or stddev_daily == 0:
            continue
        # Each anomaly day: which services spiked vs their own 7-day average.
        prior_days = ordered_days[max(0, idx - 7):idx]
        culprits: list[dict] = []
        svc_map = per_day_service.get(d["date"], {})
        for svc, amt in svc_map.items():
            history = [
                per_day_service.get(pd, {}).get(svc, 0.0) for pd in prior_days
            ]
            svc_avg = sum(history) / len(history) if history else 0.0
            delta = round(amt - svc_avg, 2)
            if delta > 0.01 and amt > svc_avg * 1.2:
                culprits.append({
                    "service": svc,
                    "amount": round(amt, 2),
                    "avg": round(svc_avg, 2),
                    "delta": delta,
                })
        culprits.sort(key=lambda c: c["delta"], reverse=True)
        pct_above = (
            round((d["amount"] - mean_daily) / mean_daily * 100, 1)
            if mean_daily > 0 else 0.0
        )
        anomaly_days.append({
            "date": d["date"],
            "amount": d["amount"],
            "pct_above_mean": pct_above,
            "culprit_services": culprits[:5],
        })

    # --- trend detection: last 7 days vs prior 7 days per service ----------
    recent_days = ordered_days[-7:]
    prior_window = ordered_days[-14:-7]
    services = set()
    for svc_map in per_day_service.values():
        services.update(svc_map.keys())

    trending_up: list[dict] = []
    trending_down: list[dict] = []
    if recent_days and prior_window:
        for svc in services:
            recent_vals = [per_day_service.get(d, {}).get(svc, 0.0) for d in recent_days]
            prior_vals = [per_day_service.get(d, {}).get(svc, 0.0) for d in prior_window]
            recent_avg = sum(recent_vals) / len(recent_vals)
            prior_avg = sum(prior_vals) / len(prior_vals)
            if prior_avg <= 0:
                continue
            change_pct = round((recent_avg - prior_avg) / prior_avg * 100, 1)
            entry = {
                "service": svc,
                "recent_avg": round(recent_avg, 2),
                "prior_avg": round(prior_avg, 2),
                "change_pct": change_pct,
            }
            if change_pct > 20:
                trending_up.append(entry)
            elif change_pct < -20:
                trending_down.append(entry)

    trending_up.sort(key=lambda x: x["change_pct"], reverse=True)
    trending_down.sort(key=lambda x: x["change_pct"])

    return {
        "daily_totals": daily_totals,
        "mean_daily": round(mean_daily, 2),
        "stddev_daily": round(stddev_daily, 2),
        "anomaly_days": anomaly_days,
        "trending_up": trending_up,
        "trending_down": trending_down,
    }


# ===========================================================================
# Savings Plan modeling
# IAM: ce:GetSavingsPlansPurchaseRecommendation, ce:GetSavingsPlansCoverage,
#      ce:GetCostAndUsage
# ===========================================================================

def get_savings_plans_coverage(days: int = 30) -> float:
    """Returns % of compute spend covered by Savings Plans (0-100)."""
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return 0.0

    try:
        ce = get_aws_client("ce", region_name=_CE_REGION)
    except Exception as e:
        log.warning("aws.check_failed", check="sp_coverage_client", error=str(e))
        return 0.0

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)
    try:
        cov = ce.get_savings_plans_coverage(
            TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
            Granularity="MONTHLY",
        )
        totals = cov.get("SavingsPlansCoverages", [])
        if totals:
            c = totals[0].get("Coverage", {})
            return round(float(c.get("CoveragePercentage", 0) or 0), 1)
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("aws.access_denied", check="get_savings_plans_coverage", error=str(e))
        else:
            log.warning("aws.check_failed", check="get_savings_plans_coverage", error=str(e))
    except Exception as e:
        log.warning("aws.check_failed", check="get_savings_plans_coverage", error=str(e))
    return 0.0


def _extract_sp_recommendation(resp: dict, term_label: str) -> dict | None:
    """Pull the headline numbers out of a SavingsPlansPurchaseRecommendation."""
    rec = resp.get("SavingsPlansPurchaseRecommendation", {})
    details = rec.get("SavingsPlansPurchaseRecommendationDetails", [])
    summary = rec.get("SavingsPlansPurchaseRecommendationSummary", {})

    monthly_savings = float(
        summary.get("EstimatedMonthlySavingsAmount", 0) or 0
    )
    savings_pct = float(summary.get("EstimatedSavingsPercentage", 0) or 0)
    hourly_commitment = float(summary.get("HourlyCommitmentToPurchase", 0) or 0)

    # Fall back to first detail line when the summary is sparse.
    if (not monthly_savings or not hourly_commitment) and details:
        d0 = details[0]
        if not monthly_savings:
            monthly_savings = float(d0.get("EstimatedMonthlySavingsAmount", 0) or 0)
        if not savings_pct:
            savings_pct = float(d0.get("EstimatedSavingsPercentage", 0) or 0)
        if not hourly_commitment:
            hourly_commitment = float(d0.get("HourlyCommitmentToPurchase", 0) or 0)

    if monthly_savings <= 0 and hourly_commitment <= 0:
        return None

    return {
        "term": term_label,
        "payment": "no-upfront",
        "hourly_commitment": round(hourly_commitment, 4),
        "monthly_commitment": round(hourly_commitment * 730, 2),
        "estimated_monthly_savings": round(monthly_savings, 2),
        "estimated_savings_pct": round(savings_pct, 1),
    }


def get_savings_plan_recommendations() -> dict:
    """
    Model Compute Savings Plan purchases for maximum savings.
    Uses Cost Explorer savings plan recommendations API.
    """
    default = {
        "current_ondemand_monthly": 0.0,
        "existing_sp_coverage_pct": 0.0,
        "recommendations": [],
        "max_monthly_savings": 0.0,
        "recommendation": "",
    }
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return default

    try:
        ce = get_aws_client("ce", region_name=_CE_REGION)
    except Exception as e:
        log.warning("aws.check_failed", check="sp_rec_client", error=str(e))
        return default

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=30)

    # --- current on-demand EC2 spend (last 30 days) ------------------------
    ondemand = 0.0
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={
                "And": [
                    {"Dimensions": {"Key": "SERVICE",
                                    "Values": ["Amazon Elastic Compute Cloud - Compute"]}},
                    {"Dimensions": {"Key": "PURCHASE_TYPE",
                                    "Values": ["On Demand Instances"]}},
                ]
            },
        )
        for result in resp.get("ResultsByTime", []):
            ondemand += float(result["Total"]["UnblendedCost"]["Amount"])
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("aws.access_denied", check="sp_ondemand", error=str(e))
            return default
        log.warning("aws.check_failed", check="sp_ondemand", error=str(e))
    except Exception as e:
        log.warning("aws.check_failed", check="sp_ondemand", error=str(e))

    existing_coverage = get_savings_plans_coverage(30)

    # --- 1-year + 3-year COMPUTE_SP recommendations ------------------------
    recommendations: list[dict] = []
    for term, label in (("ONE_YEAR", "1-year"), ("THREE_YEARS", "3-year")):
        try:
            resp = ce.get_savings_plans_purchase_recommendation(
                SavingsPlansType="COMPUTE_SP",
                TermInYears=term,
                PaymentOption="NO_UPFRONT",
                LookbackPeriodInDays="THIRTY_DAYS",
            )
            extracted = _extract_sp_recommendation(resp, label)
            if extracted:
                recommendations.append(extracted)
        except ClientError as e:
            if _is_access_denied(e):
                log.warning("aws.access_denied", check=f"sp_rec_{term}", error=str(e))
            else:
                log.warning("aws.check_failed", check=f"sp_rec_{term}", error=str(e))
        except Exception as e:
            log.warning("aws.check_failed", check=f"sp_rec_{term}", error=str(e))

    max_savings = 0.0
    recommendation_text = ""
    if recommendations:
        best = max(recommendations, key=lambda r: r["estimated_monthly_savings"])
        max_savings = best["estimated_monthly_savings"]
        recommendation_text = (
            f"Purchase {best['term']} Compute SP at "
            f"${best['hourly_commitment']:.2f}/hr "
            f"(saves ${max_savings:,.2f}/mo, {best['estimated_savings_pct']}%)"
        )

    return {
        "current_ondemand_monthly": round(ondemand, 2),
        "existing_sp_coverage_pct": existing_coverage,
        "recommendations": recommendations,
        "max_monthly_savings": round(max_savings, 2),
        "recommendation": recommendation_text,
    }


# ===========================================================================
# Cost breakdown by tag
# IAM: ce:GetCostAndUsage
# ===========================================================================

def get_cost_by_tag(tag_key: str = "Environment", days: int = 30) -> dict:
    """
    Break down costs by tag value (prod vs dev vs staging).
    Identifies untagged spend and dev resources running 24/7.
    """
    default = {
        "by_tag": {},
        "untagged_pct": 0.0,
        "untagged_monthly": 0.0,
        "tag_key": tag_key,
        "recommendation": "",
    }
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return default

    try:
        ce = get_aws_client("ce", region_name=_CE_REGION)
    except Exception as e:
        log.warning("aws.check_failed", check="tag_ce_client", error=str(e))
        return default

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "TAG", "Key": tag_key}],
        )
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("aws.access_denied", check="get_cost_by_tag", error=str(e))
            return default
        log.warning("aws.check_failed", check="get_cost_by_tag", error=str(e))
        return default
    except Exception as e:
        log.warning("aws.check_failed", check="get_cost_by_tag", error=str(e))
        return default

    by_tag: dict[str, float] = {}
    total = 0.0
    for result in resp.get("ResultsByTime", []):
        for group in result.get("Groups", []):
            raw_key = group["Keys"][0] if group.get("Keys") else ""
            # Cost Explorer returns tag values as "Environment$prod"; strip prefix.
            if "$" in raw_key:
                value = raw_key.split("$", 1)[1]
            else:
                value = raw_key
            if not value:
                value = "untagged"
            amt = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amt <= 0:
                continue
            by_tag[value] = round(by_tag.get(value, 0.0) + amt, 2)
            total += amt

    untagged_monthly = by_tag.get("untagged", 0.0)
    untagged_pct = round(untagged_monthly / total * 100, 1) if total > 0 else 0.0

    recommendation = ""
    if untagged_monthly > 0:
        recommendation = (
            f"{untagged_pct}% of spend (${untagged_monthly:,.0f}/mo) is untagged "
            f"— impossible to attribute costs"
        )

    return {
        "by_tag": dict(sorted(by_tag.items(), key=lambda x: x[1], reverse=True)),
        "untagged_pct": untagged_pct,
        "untagged_monthly": round(untagged_monthly, 2),
        "tag_key": tag_key,
        "recommendation": recommendation,
    }
