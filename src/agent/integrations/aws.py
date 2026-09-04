"""
Safe boto3 wrapper for the AI Agentic OS AWS skill.

All AWS API calls go through typed helpers here — no raw boto3 elsewhere.
Never raises exceptions; always returns data or an empty list so callers
can decide what to do with errors.

Credential resolution respects settings.aws_auth_method (see config.py /
get_aws_client() below):
  - iam_role     (default) — EC2/EKS instance role or local IMDS via the
                 standard boto3 chain (env vars, ~/.aws/credentials, IMDS)
  - access_key   — static AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
  - sso_profile  — a named ~/.aws profile (AWS_PROFILE)
"""
from __future__ import annotations

import subprocess
import time
from typing import Any

from agent.core.models import AwsResource, AwsResourceType
from agent.observability.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = 30  # seconds per boto3 call


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def get_aws_client(service: str, region_name: str | None = None):
    """
    Build a boto3 client for `service` using the configured auth method
    (settings.aws_auth_method). Drop-in replacement for
    boto3.client(service, region_name=...) — same signature, same return
    type — just routed through whichever of the three auth methods the
    user configured instead of always taking boto3's default session.
    """
    from agent.config import settings
    session = settings.get_aws_session()
    return session.client(service, region_name=region_name or settings.aws_region)


def _boto3_client(service: str, region: str):
    return get_aws_client(service, region_name=region)


def friendly_aws_error(exc: Exception) -> str:
    """
    Translate the handful of AWS auth failures a user is most likely to hit
    into an actionable message, instead of a raw botocore exception string.
    Falls back to the raw message for anything else.
    """
    text = str(exc)
    if "NoCredentialsError" in type(exc).__name__ or "Unable to locate credentials" in text:
        return (
            "AWS credentials not found.\n"
            "  Run: agent setup — choose your authentication method.\n"
            "  If on EC2/EKS: select IAM Role (recommended).\n"
            "  If local: select Access Keys or SSO Profile."
        )
    if "InvalidClientTokenId" in text:
        return (
            "AWS Access Key is invalid or expired.\n"
            "  Run: agent aws switch-auth to reconfigure your credentials."
        )
    if "AccessDenied" in text or "AccessDeniedException" in text:
        return f"AWS permission denied — the configured identity is missing an IAM permission.\n  {text[:200]}"
    return text[:200]


def _safe(fn, *args, label: str = "aws", **kwargs):
    """Call fn(*args, **kwargs), log errors, return None on failure."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        log.warning(f"{label}.error", error=friendly_aws_error(exc))
        return None


# ---------------------------------------------------------------------------
# EC2
# ---------------------------------------------------------------------------

_EC2_PROBLEM_STATES = {"stopped", "stopping", "terminated", "shutting-down"}
_EC2_CHECK_FAILED   = {"failed", "insufficient-data"}


def get_unhealthy_ec2(region: str) -> list[AwsResource]:
    """Return EC2 instances that are stopped or failing status checks."""
    ec2 = _boto3_client("ec2", region)
    resources: list[AwsResource] = []

    # --- stopped / terminated instances (excluding intentionally terminated) ---
    resp = _safe(
        ec2.describe_instances,
        Filters=[{"Name": "instance-state-name", "Values": list(_EC2_PROBLEM_STATES)}],
        label="ec2.describe_instances",
    )
    if resp:
        for reservation in resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                iid   = inst["InstanceId"]
                state = inst["State"]["Name"]
                name  = next(
                    (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"),
                    iid,
                )
                resources.append(AwsResource(
                    id=iid,
                    name=name,
                    resource_type=AwsResourceType.EC2,
                    status=state,
                    region=region,
                    metadata={
                        "instance_type":     inst.get("InstanceType", "?"),
                        "launch_time":       str(inst.get("LaunchTime", "")),
                        "availability_zone": inst.get("Placement", {}).get("AvailabilityZone", "?"),
                        "vpc_id":            inst.get("VpcId", "?"),
                        "platform":          inst.get("Platform", "linux"),
                        "state_reason":      inst.get("StateReason", {}).get("Message", ""),
                    },
                ))

    # --- running instances failing system/instance status checks ---
    checks = _safe(
        ec2.describe_instance_status,
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}],
        label="ec2.describe_instance_status",
    )
    if checks:
        for item in checks.get("InstanceStatuses", []):
            sys_ok  = item["SystemStatus"]["Status"]
            inst_ok = item["InstanceStatus"]["Status"]
            if sys_ok in _EC2_CHECK_FAILED or inst_ok in _EC2_CHECK_FAILED:
                iid = item["InstanceId"]
                # fetch name tag separately
                detail = _safe(
                    ec2.describe_instances,
                    InstanceIds=[iid],
                    label="ec2.describe_instances.single",
                )
                inst = (
                    detail["Reservations"][0]["Instances"][0]
                    if detail and detail.get("Reservations")
                    else {}
                )
                name = next(
                    (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"),
                    iid,
                )
                resources.append(AwsResource(
                    id=iid,
                    name=name,
                    resource_type=AwsResourceType.EC2,
                    status=f"status-check-failed (sys={sys_ok}, inst={inst_ok})",
                    region=region,
                    metadata={
                        "system_status":   sys_ok,
                        "instance_status": inst_ok,
                        "instance_type":   inst.get("InstanceType", "?"),
                        "availability_zone": item.get("AvailabilityZone", "?"),
                    },
                ))

    log.info("aws.ec2.unhealthy", region=region, count=len(resources))
    return resources


def get_ec2_detail(instance_id: str, region: str) -> dict[str, Any]:
    """Return detailed info for one EC2 instance for Claude context."""
    ec2   = _boto3_client("ec2", region)
    cw    = _boto3_client("cloudwatch", region)
    lines: list[str] = []

    resp = _safe(ec2.describe_instances, InstanceIds=[instance_id], label="ec2.detail")
    if resp and resp.get("Reservations"):
        inst = resp["Reservations"][0]["Instances"][0]
        lines.append(f"Instance ID    : {instance_id}")
        lines.append(f"Type           : {inst.get('InstanceType')}")
        lines.append(f"State          : {inst['State']['Name']}")
        lines.append(f"State reason   : {inst.get('StateReason', {}).get('Message', 'n/a')}")
        lines.append(f"AZ             : {inst.get('Placement', {}).get('AvailabilityZone')}")
        lines.append(f"VPC            : {inst.get('VpcId')}")
        lines.append(f"Launch time    : {inst.get('LaunchTime')}")
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
        lines.append(f"Tags           : {tags}")

    status = _safe(ec2.describe_instance_status, InstanceIds=[instance_id], label="ec2.status")
    if status and status.get("InstanceStatuses"):
        s = status["InstanceStatuses"][0]
        lines.append(f"System status  : {s['SystemStatus']['Status']}")
        lines.append(f"Instance status: {s['InstanceStatus']['Status']}")

    return {"detail": "\n".join(lines)}


# ---------------------------------------------------------------------------
# RDS
# ---------------------------------------------------------------------------

_RDS_PROBLEM_STATUSES = {
    "stopped", "stopping", "failed", "storage-full",
    "incompatible-parameters", "incompatible-restore",
    "maintenance", "inaccessible-encryption-credentials",
}


def get_unhealthy_rds(region: str) -> list[AwsResource]:
    """Return RDS DB instances that are not 'available'."""
    rds = _boto3_client("rds", region)
    resources: list[AwsResource] = []

    resp = _safe(rds.describe_db_instances, label="rds.describe_db_instances")
    if not resp:
        return []

    for db in resp.get("DBInstances", []):
        status = db["DBInstanceStatus"]
        if status not in _RDS_PROBLEM_STATUSES:
            # Also flag if storage >= 95 % used
            allocated = db.get("AllocatedStorage", 0)
            # CloudWatch would be needed for used storage; skip that for now.
            continue

        dbid = db["DBInstanceIdentifier"]
        resources.append(AwsResource(
            id=dbid,
            name=dbid,
            resource_type=AwsResourceType.RDS,
            status=status,
            region=region,
            metadata={
                "engine":           db.get("Engine", "?"),
                "engine_version":   db.get("EngineVersion", "?"),
                "instance_class":   db.get("DBInstanceClass", "?"),
                "multi_az":         db.get("MultiAZ", False),
                "allocated_storage": db.get("AllocatedStorage", 0),
                "endpoint":         db.get("Endpoint", {}).get("Address", "?"),
                "availability_zone": db.get("AvailabilityZone", "?"),
            },
        ))

    log.info("aws.rds.unhealthy", region=region, count=len(resources))
    return resources


def get_rds_detail(db_identifier: str, region: str) -> dict[str, Any]:
    """Return detailed RDS info + recent events for Claude context."""
    rds   = _boto3_client("rds", region)
    lines: list[str] = []

    resp = _safe(rds.describe_db_instances, DBInstanceIdentifier=db_identifier, label="rds.detail")
    if resp and resp.get("DBInstances"):
        db = resp["DBInstances"][0]
        lines.append(f"DB Identifier  : {db_identifier}")
        lines.append(f"Status         : {db['DBInstanceStatus']}")
        lines.append(f"Engine         : {db.get('Engine')} {db.get('EngineVersion')}")
        lines.append(f"Instance class : {db.get('DBInstanceClass')}")
        lines.append(f"Multi-AZ       : {db.get('MultiAZ')}")
        lines.append(f"Storage (GB)   : {db.get('AllocatedStorage')}")
        lines.append(f"Storage type   : {db.get('StorageType')}")
        lines.append(f"Endpoint       : {db.get('Endpoint', {}).get('Address', 'n/a')}")

    events = _safe(
        rds.describe_events,
        SourceIdentifier=db_identifier,
        SourceType="db-instance",
        Duration=1440,
        label="rds.events",
    )
    if events and events.get("Events"):
        lines.append("\n--- RECENT EVENTS (last 24h) ---")
        for ev in events["Events"][-10:]:
            lines.append(f"  {ev.get('Date', '')}  {ev.get('Message', '')}")

    return {"detail": "\n".join(lines)}


# ---------------------------------------------------------------------------
# ALB (Application Load Balancer)
# ---------------------------------------------------------------------------

def get_unhealthy_alb(region: str) -> list[AwsResource]:
    """Return ALBs that have unhealthy or no registered targets."""
    elb = _boto3_client("elbv2", region)
    resources: list[AwsResource] = []

    lbs = _safe(elb.describe_load_balancers, label="elb.describe_load_balancers")
    if not lbs:
        return []

    for lb in lbs.get("LoadBalancers", []):
        if lb.get("Type") != "application":
            continue
        lb_arn  = lb["LoadBalancerArn"]
        lb_name = lb["LoadBalancerName"]
        lb_state = lb.get("State", {}).get("Code", "?")

        tgs = _safe(
            elb.describe_target_groups,
            LoadBalancerArn=lb_arn,
            label="elb.describe_target_groups",
        )
        if not tgs:
            continue

        for tg in tgs.get("TargetGroups", []):
            tg_arn  = tg["TargetGroupArn"]
            tg_name = tg["TargetGroupName"]

            health = _safe(
                elb.describe_target_health,
                TargetGroupArn=tg_arn,
                label="elb.describe_target_health",
            )
            if not health:
                continue

            descriptions = health.get("TargetHealthDescriptions", [])
            if not descriptions:
                resources.append(AwsResource(
                    id=tg_arn,
                    name=f"{lb_name}/{tg_name}",
                    resource_type=AwsResourceType.ALB,
                    status="no-targets",
                    region=region,
                    metadata={
                        "lb_name":    lb_name,
                        "lb_state":   lb_state,
                        "tg_name":    tg_name,
                        "protocol":   tg.get("Protocol", "?"),
                        "port":       tg.get("Port", "?"),
                        "target_count": 0,
                    },
                ))
                continue

            unhealthy = [
                d for d in descriptions
                if d.get("TargetHealth", {}).get("State") not in ("healthy", "unused")
            ]
            if unhealthy:
                resources.append(AwsResource(
                    id=tg_arn,
                    name=f"{lb_name}/{tg_name}",
                    resource_type=AwsResourceType.ALB,
                    status=f"{len(unhealthy)}/{len(descriptions)} unhealthy",
                    region=region,
                    metadata={
                        "lb_name":        lb_name,
                        "lb_state":       lb_state,
                        "tg_name":        tg_name,
                        "protocol":       tg.get("Protocol", "?"),
                        "port":           tg.get("Port", "?"),
                        "target_count":   len(descriptions),
                        "unhealthy_count": len(unhealthy),
                        "unhealthy_reasons": [
                            d.get("TargetHealth", {}).get("Description", "")
                            for d in unhealthy
                        ],
                    },
                ))

    log.info("aws.alb.unhealthy", region=region, count=len(resources))
    return resources


def get_alb_detail(tg_arn: str, region: str) -> dict[str, Any]:
    """Return target health details for Claude context."""
    elb   = _boto3_client("elbv2", region)
    lines: list[str] = []

    health = _safe(elb.describe_target_health, TargetGroupArn=tg_arn, label="elb.health.detail")
    if health:
        lines.append("--- TARGET HEALTH ---")
        for d in health.get("TargetHealthDescriptions", []):
            target = d.get("Target", {})
            state  = d.get("TargetHealth", {}).get("State", "?")
            reason = d.get("TargetHealth", {}).get("Description", "")
            lines.append(f"  {target.get('Id', '?')}:{target.get('Port', '?')}  state={state}  {reason}")

    return {"detail": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Full inventory collectors (ALL resources, not just unhealthy)
# ---------------------------------------------------------------------------

def get_all_ec2(region: str) -> list[dict]:
    """Return ALL EC2 instances with state, type, IPs, tags."""
    ec2 = _boto3_client("ec2", region)
    instances: list[dict] = []

    resp = _safe(ec2.describe_instances, label="ec2.all")
    if not resp:
        return []

    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            tags  = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
            state = inst["State"]["Name"]
            instances.append({
                "id":                inst["InstanceId"],
                "name":              tags.get("Name", inst["InstanceId"]),
                "state":             state,
                "instance_type":     inst.get("InstanceType", "?"),
                "private_ip":        inst.get("PrivateIpAddress", ""),
                "public_ip":         inst.get("PublicIpAddress", ""),
                "availability_zone": inst.get("Placement", {}).get("AvailabilityZone", ""),
                "vpc_id":            inst.get("VpcId", ""),
                "launch_time":       str(inst.get("LaunchTime", "")),
                "tags":              tags,
                "region":            region,
                "is_healthy":        state == "running",
            })

    log.info("aws.ec2.all", region=region, count=len(instances))
    return instances


def get_elastic_ips(region: str) -> list[dict]:
    """Return all Elastic IPs — attached and unattached."""
    ec2 = _boto3_client("ec2", region)
    eips: list[dict] = []

    resp = _safe(ec2.describe_addresses, label="ec2.eip")
    if not resp:
        return []

    for addr in resp.get("Addresses", []):
        tags          = {t["Key"]: t["Value"] for t in addr.get("Tags", [])}
        instance_id   = addr.get("InstanceId", "")
        association   = addr.get("AssociationId", "")
        is_attached   = bool(instance_id or association)
        eips.append({
            "public_ip":      addr.get("PublicIp", ""),
            "allocation_id":  addr.get("AllocationId", ""),
            "association_id": association,
            "instance_id":    instance_id,
            "network_interface_id": addr.get("NetworkInterfaceId", ""),
            "private_ip":     addr.get("PrivateIpAddress", ""),
            "domain":         addr.get("Domain", "vpc"),
            "name":           tags.get("Name", addr.get("PublicIp", "")),
            "tags":           tags,
            "region":         region,
            "is_attached":    is_attached,
            "is_healthy":     is_attached,
        })

    log.info("aws.eip.all", region=region, count=len(eips))
    return eips


def get_all_load_balancers(region: str) -> list[dict]:
    """Return ALL load balancers (ALB + NLB + Gateway) with target health summary."""
    elb = _boto3_client("elbv2", region)
    lbs: list[dict] = []

    resp = _safe(elb.describe_load_balancers, label="elb.all")
    if not resp:
        return []

    for lb in resp.get("LoadBalancers", []):
        lb_arn    = lb["LoadBalancerArn"]
        lb_name   = lb["LoadBalancerName"]
        lb_type   = lb.get("Type", "?")
        lb_state  = lb.get("State", {}).get("Code", "?")
        lb_dns    = lb.get("DNSName", "")

        # Get target group health summary
        total_targets   = 0
        healthy_targets = 0
        tg_names: list[str] = []

        tgs = _safe(elb.describe_target_groups, LoadBalancerArn=lb_arn, label="elb.tgs")
        if tgs:
            for tg in tgs.get("TargetGroups", []):
                tg_names.append(tg["TargetGroupName"])
                health = _safe(
                    elb.describe_target_health,
                    TargetGroupArn=tg["TargetGroupArn"],
                    label="elb.tg.health",
                )
                if health:
                    descs = health.get("TargetHealthDescriptions", [])
                    total_targets   += len(descs)
                    healthy_targets += sum(
                        1 for d in descs
                        if d.get("TargetHealth", {}).get("State") == "healthy"
                    )

        is_healthy = lb_state == "active" and (
            total_targets == 0 or healthy_targets == total_targets
        )

        lbs.append({
            "arn":              lb_arn,
            "name":             lb_name,
            "type":             lb_type,
            "state":            lb_state,
            "dns_name":         lb_dns,
            "total_targets":    total_targets,
            "healthy_targets":  healthy_targets,
            "target_groups":    tg_names,
            "region":           region,
            "is_healthy":       is_healthy,
        })

    log.info("aws.elb.all", region=region, count=len(lbs))
    return lbs


def get_security_groups(region: str) -> list[dict]:
    """Return all security groups with open-to-world rules flagged."""
    ec2 = _boto3_client("ec2", region)
    sgs: list[dict] = []

    resp = _safe(ec2.describe_security_groups, label="ec2.sg")
    if not resp:
        return []

    for sg in resp.get("SecurityGroups", []):
        sg_id   = sg["GroupId"]
        sg_name = sg.get("GroupName", sg_id)
        vpc_id  = sg.get("VpcId", "")
        desc    = sg.get("Description", "")

        open_rules: list[dict] = []
        for rule in sg.get("IpPermissions", []):
            proto    = rule.get("IpProtocol", "-1")
            from_p   = rule.get("FromPort", 0)
            to_p     = rule.get("ToPort", 65535)
            cidrs    = [r["CidrIp"] for r in rule.get("IpRanges", [])]
            cidr6s   = [r["CidrIpv6"] for r in rule.get("Ipv6Ranges", [])]
            all_cidrs = cidrs + cidr6s
            is_open  = "0.0.0.0/0" in all_cidrs or "::/0" in all_cidrs
            if is_open:
                port_str = (
                    "ALL" if proto == "-1"
                    else f"{from_p}" if from_p == to_p
                    else f"{from_p}-{to_p}"
                )
                open_rules.append({
                    "proto":    proto if proto != "-1" else "ALL",
                    "port":     port_str,
                    "cidrs":    all_cidrs,
                })

        tags    = {t["Key"]: t["Value"] for t in sg.get("Tags", [])}
        display = tags.get("Name", sg_name)

        sgs.append({
            "id":          sg_id,
            "name":        display,
            "group_name":  sg_name,
            "vpc_id":      vpc_id,
            "description": desc,
            "open_rules":  open_rules,
            "inbound_count": len(sg.get("IpPermissions", [])),
            "tags":        tags,
            "region":      region,
            "is_healthy":  len(open_rules) == 0,
        })

    log.info("aws.sg.all", region=region, count=len(sgs))
    return sgs


# ---------------------------------------------------------------------------
# Fix runner  (AWS CLI subprocess — same pattern as kubectl apply_fix)
# ---------------------------------------------------------------------------

class AwsFixResult:
    def __init__(self, success: bool, output: str, error: str, duration_ms: float):
        self.success     = success
        self.output      = output
        self.error       = error
        self.duration_ms = duration_ms


def run_aws_fix(command: str) -> AwsFixResult:
    """
    Run an AWS CLI fix command approved by the user.
    Strips leading 'aws' token if present, then runs via subprocess.
    """
    import shutil

    parts = command.strip().split()
    if parts and parts[0] == "aws":
        parts = parts[1:]
    full_cmd = ["aws"] + parts

    if not shutil.which("aws"):
        return AwsFixResult(
            success=False,
            output="",
            error="aws CLI not found on PATH",
            duration_ms=0.0,
        )

    log.info("aws.fix.run", command=command)
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        success     = proc.returncode == 0

        if success:
            log.info("aws.fix.ok", duration_ms=duration_ms)
        else:
            log.warning("aws.fix.failed", returncode=proc.returncode, stderr=proc.stderr[:200])

        return AwsFixResult(
            success=success,
            output=proc.stdout,
            error=proc.stderr,
            duration_ms=duration_ms,
        )
    except subprocess.TimeoutExpired:
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        log.error("aws.fix.timeout", timeout=_TIMEOUT)
        return AwsFixResult(success=False, output="", error=f"timed out after {_TIMEOUT}s", duration_ms=duration_ms)
    except Exception as exc:
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        log.error("aws.fix.exception", error=str(exc))
        return AwsFixResult(success=False, output="", error=str(exc), duration_ms=duration_ms)
