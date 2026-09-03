"""Database health monitoring skill — RDS/Aurora."""
from __future__ import annotations

from agent.integrations.rds import (
    analyze_rds_health,
    get_aurora_clusters,
    get_rds_instances,
    get_rds_metrics,
)
from agent.observability.logging import get_logger

log = get_logger(__name__)

_DEFAULT_REGION = "us-east-1"


def _status_from_issues(issues: list[dict]) -> str:
    """Derive overall status from the most severe issue present."""
    if any(i.get("severity") == "critical" for i in issues):
        return "critical"
    if any(i.get("severity") == "warning" for i in issues):
        return "warning"
    return "healthy"


def scan_all_databases(region: str = _DEFAULT_REGION) -> list[dict]:
    """Scan all RDS instances + Aurora clusters and analyze their health.

    For critical issues an incident is opened and a Slack alert is sent.
    Returns one result dict per instance.
    """
    instances = get_rds_instances(region)
    try:
        clusters = get_aurora_clusters(region)
    except Exception as exc:
        log.warning("db_health.clusters_failed", error=str(exc))
        clusters = []

    # Index clusters by id so callers/tests can correlate (members count, etc.).
    cluster_index = {c["id"]: c for c in clusters}

    results: list[dict] = []
    for instance in instances:
        instance_id = instance.get("id", "")
        try:
            metrics = get_rds_metrics(instance_id, region)
        except Exception as exc:
            log.warning("db_health.metrics_failed", instance=instance_id, error=str(exc))
            metrics = {}

        issues = analyze_rds_health(instance_id, metrics, instance)
        status = _status_from_issues(issues)

        incident_id = ""
        critical_issues = [i for i in issues if i.get("severity") == "critical"]
        if critical_issues:
            incident_id = _raise_incident(instance, metrics, critical_issues)

        results.append({
            "instance": instance,
            "metrics": metrics,
            "issues": issues,
            "status": status,
            "incident_id": incident_id,
            "cluster": cluster_index.get(instance_id),
        })

    return results


def _raise_incident(instance: dict, metrics: dict, critical_issues: list[dict]) -> str:
    """Open an incident and send a Slack alert for critical DB issues."""
    instance_id = instance.get("id", "")
    region = instance.get("region", _DEFAULT_REGION)
    summary = "; ".join(f"{i['type']}: {i['detail']}" for i in critical_issues)

    incident_id = ""
    try:
        from agent.skills import incident
        incident_id = incident.open_incident(
            title=f"RDS critical: {instance_id}",
            severity="critical",
            service=instance_id,
            namespace=f"rds/{region}",
            cause=summary,
        )
    except Exception as exc:
        log.warning("db_health.incident_failed", instance=instance_id, error=str(exc))

    try:
        from agent.integrations.slack import send_alert_generic
        send_alert_generic(
            title=f"Database critical: {instance_id}",
            message=summary,
            severity="critical",
            fields={
                "Instance": instance_id,
                "Engine": instance.get("engine", ""),
                "Region": region,
                "CPU": f"{metrics.get('cpu_pct', 0):.0f}%",
                "Free Storage": f"{metrics.get('free_storage_gb', 0):.1f}GB",
            },
            fix_command=critical_issues[0].get("fix"),
        )
    except Exception as exc:
        log.warning("db_health.slack_failed", instance=instance_id, error=str(exc))

    return incident_id


def get_storage_forecast(instance_id: str, metrics: dict) -> dict:
    """Estimate days until storage is full from free space and write activity.

    Heuristic: high write latency implies heavy write load (~1GB/day),
    otherwise assume a slow ~0.25GB/day baseline.
    """
    free_gb = metrics.get("free_storage_gb", 0.0)
    write_latency = metrics.get("write_latency_ms", 0.0)

    daily_consumption = 1.0 if write_latency > 10 else 0.25
    days_until_full = int(free_gb / daily_consumption) if daily_consumption > 0 else 9999

    if days_until_full < 7:
        recommendation = (
            f"URGENT: ~{days_until_full} days until full. "
            f"Increase allocated storage now (apply_storage_fix)."
        )
    elif days_until_full < 30:
        recommendation = (
            f"~{days_until_full} days until full. "
            f"Plan a storage increase for {instance_id}."
        )
    else:
        recommendation = f"Storage healthy (~{days_until_full} days headroom)."

    return {
        "days_until_full": days_until_full,
        "recommendation": recommendation,
    }


def apply_storage_fix(
    instance_id: str,
    additional_gb: int = 20,
    region: str = _DEFAULT_REGION,
) -> bool:
    """Increase allocated storage for an RDS instance. Only call when confirmed.

    Returns True on success.
    """
    from agent.integrations.rds import _import_boto3, _is_access_denied, _region

    region = _region(region)
    boto3, ClientError = _import_boto3()
    if boto3 is None:
        return False

    # Look up current allocated storage so we add to it.
    current_storage = 0
    for inst in get_rds_instances(region):
        if inst.get("id") == instance_id:
            current_storage = inst.get("storage_gb", 0) or 0
            break

    if current_storage <= 0:
        log.warning("db_health.storage_fix_no_instance", instance=instance_id)
        return False

    new_storage = current_storage + additional_gb
    try:
        rds = boto3.client("rds", region_name=region)
        rds.modify_db_instance(
            DBInstanceIdentifier=instance_id,
            AllocatedStorage=new_storage,
            ApplyImmediately=True,
        )
        log.info(
            "db_health.storage_fixed",
            instance=instance_id,
            old=current_storage,
            new=new_storage,
        )
        return True
    except ClientError as e:
        if _is_access_denied(e):
            log.warning("rds.access_denied", check="modify_db_instance", error=str(e))
        else:
            log.warning("rds.failed", check="modify_db_instance", error=str(e))
        return False
    except Exception as e:
        log.warning("rds.failed", check="modify_db_instance", error=str(e))
        return False
