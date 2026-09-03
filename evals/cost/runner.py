"""AWS cost optimization eval runner.

Mocks all boto3 calls with realistic data so the cost-optimisation pipeline can
be exercised offline. Each case maps to a scenario that shapes the mock client.

Run standalone:
    $env:PYTHONPATH = "src"
    uv run python evals/cost/runner.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

CASES_FILE = Path(__file__).parent / "cases.jsonl"


def _now():
    return datetime.now(timezone.utc)


def _client_error(operation: str):
    """Build a botocore ClientError representing AccessDenied."""
    from botocore.exceptions import ClientError
    return ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized"}},
        operation,
    )


def _make_boto3_mock(scenario: str):
    """Return (mock_boto3_module, ClientError) for the given scenario.

    The returned object mimics the boto3 module: boto3.client(name) -> MagicMock
    configured per service for this scenario.
    """
    from botocore.exceptions import ClientError

    deny = scenario == "permission_denied"

    # --- EC2 ----------------------------------------------------------------
    ec2 = MagicMock()

    def _ec2_describe_volumes(*args, **kwargs):
        if deny:
            raise _client_error("DescribeVolumes")
        filters = kwargs.get("Filters", [])
        status = ""
        for f in filters:
            if f.get("Name") == "status":
                status = f.get("Values", [""])[0]
        if status == "available":
            if scenario == "idle_ebs_volumes":
                return {"Volumes": [
                    {"VolumeId": "vol-001", "Size": 40, "VolumeType": "gp2",
                     "CreateTime": _now() - timedelta(days=100), "State": "available"},
                    {"VolumeId": "vol-002", "Size": 50, "VolumeType": "gp3",
                     "CreateTime": _now() - timedelta(days=60), "State": "available"},
                    {"VolumeId": "vol-003", "Size": 30, "VolumeType": "gp2",
                     "CreateTime": _now() - timedelta(days=10), "State": "available"},
                ]}
            return {"Volumes": []}
        # in-use
        if scenario == "gp2_upgrade":
            return {"Volumes": [
                {"VolumeId": "vol-100", "Size": 100, "VolumeType": "gp2", "State": "in-use"},
                {"VolumeId": "vol-101", "Size": 200, "VolumeType": "gp2", "State": "in-use"},
                {"VolumeId": "vol-102", "Size": 50, "VolumeType": "gp2", "State": "in-use"},
            ]}
        return {"Volumes": []}

    def _ec2_describe_addresses(*args, **kwargs):
        if deny:
            raise _client_error("DescribeAddresses")
        if scenario == "unused_eips":
            return {"Addresses": [
                {"AllocationId": "eipalloc-1", "PublicIp": "1.2.3.4"},
                {"AllocationId": "eipalloc-2", "PublicIp": "5.6.7.8"},
                {"AllocationId": "eipalloc-3", "PublicIp": "9.9.9.9",
                 "AssociationId": "eipassoc-x"},  # attached, ignored
            ]}
        return {"Addresses": []}

    def _ec2_describe_instances(*args, **kwargs):
        if deny:
            raise _client_error("DescribeInstances")
        return {"Reservations": []}

    ec2.describe_volumes.side_effect = _ec2_describe_volumes
    ec2.describe_addresses.side_effect = _ec2_describe_addresses
    ec2.describe_instances.side_effect = _ec2_describe_instances

    # ec2 paginator (snapshots)
    def _ec2_get_paginator(name):
        pag = MagicMock()
        if name == "describe_snapshots":
            def _paginate(*a, **k):
                if deny:
                    raise _client_error("DescribeSnapshots")
                return iter([{"Snapshots": []}])
            pag.paginate.side_effect = _paginate
        return pag
    ec2.get_paginator.side_effect = _ec2_get_paginator

    # --- CloudWatch ---------------------------------------------------------
    cw = MagicMock()
    def _cw_get_metric_statistics(*args, **kwargs):
        if deny:
            raise _client_error("GetMetricStatistics")
        return {"Datapoints": [{"Average": 4.0}]}
    cw.get_metric_statistics.side_effect = _cw_get_metric_statistics

    # --- ELBv2 --------------------------------------------------------------
    elbv2 = MagicMock()
    def _elb_describe_lbs(*args, **kwargs):
        if deny:
            raise _client_error("DescribeLoadBalancers")
        return {"LoadBalancers": []}
    elbv2.describe_load_balancers.side_effect = _elb_describe_lbs

    # --- CloudWatch Logs ----------------------------------------------------
    logs = MagicMock()
    def _logs_get_paginator(name):
        pag = MagicMock()
        def _paginate(*a, **k):
            if deny:
                raise _client_error("DescribeLogGroups")
            if scenario == "log_retention":
                return iter([{"logGroups": [
                    {"logGroupName": "/aws/lambda/foo", "storedBytes": 5 * 1024**3},
                    {"logGroupName": "/aws/eks/bar", "storedBytes": 2 * 1024**3},
                    {"logGroupName": "/has/retention", "storedBytes": 1 * 1024**3,
                     "retentionInDays": 30},
                ]}])
            return iter([{"logGroups": []}])
        pag.paginate.side_effect = _paginate
        return pag
    logs.get_paginator.side_effect = _logs_get_paginator

    # --- ECR ----------------------------------------------------------------
    ecr = MagicMock()
    def _ecr_get_paginator(name):
        pag = MagicMock()
        if name == "describe_repositories":
            def _paginate(*a, **k):
                if deny:
                    raise _client_error("DescribeRepositories")
                if scenario == "old_ecr_images":
                    return iter([{"repositories": [{"repositoryName": "app"}]}])
                return iter([{"repositories": []}])
            pag.paginate.side_effect = _paginate
        elif name == "describe_images":
            def _paginate(*a, **k):
                if deny:
                    raise _client_error("DescribeImages")
                if scenario == "old_ecr_images":
                    old = _now() - timedelta(days=120)
                    return iter([{"imageDetails": [
                        {"imageDigest": f"sha256:{i}", "imageSizeInBytes": 200 * 1024**2,
                         "imagePushedAt": old, "imageTags": []}
                        for i in range(5)
                    ]}])
                return iter([{"imageDetails": []}])
            pag.paginate.side_effect = _paginate
        return pag
    ecr.get_paginator.side_effect = _ecr_get_paginator

    # --- Cost Explorer ------------------------------------------------------
    ce = MagicMock()
    def _ce_get_cost_and_usage(*args, **kwargs):
        if deny:
            raise _client_error("GetCostAndUsage")
        group_by = kwargs.get("GroupBy") or []
        granularity = kwargs.get("Granularity", "")
        group_type = group_by[0].get("Type") if group_by else None

        # --- TAG grouping (get_cost_by_tag) --------------------------------
        if group_type == "TAG":
            if scenario == "tag_analysis":
                # CE returns tag values prefixed: "Environment$prod".
                return {"ResultsByTime": [
                    {"TimePeriod": {"Start": "2026-06-01"},
                     "Groups": [
                         {"Keys": ["Environment$prod"],
                          "Metrics": {"UnblendedCost": {"Amount": "500.0"}}},
                         {"Keys": ["Environment$dev"],
                          "Metrics": {"UnblendedCost": {"Amount": "200.0"}}},
                         {"Keys": ["Environment$"],
                          "Metrics": {"UnblendedCost": {"Amount": "300.0"}}},
                     ]},
                ]}
            return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-06-01"}, "Groups": []}]}

        # --- DAILY service grouping (get_cost_and_usage + get_spend_anomalies) ---
        if granularity == "DAILY" and group_type == "DIMENSION":
            base = datetime(2026, 6, 1)
            if scenario == "spend_anomaly_detected":
                # 14 steady days at ~$10, day 15 spikes to ~$30 (3x mean).
                results = []
                for i in range(15):
                    day = (base + timedelta(days=i)).strftime("%Y-%m-%d")
                    if i == 14:
                        groups = [
                            {"Keys": ["Amazon EC2"],
                             "Metrics": {"UnblendedCost": {"Amount": "25.0"}}},
                            {"Keys": ["Data Transfer"],
                             "Metrics": {"UnblendedCost": {"Amount": "5.0"}}},
                        ]
                    else:
                        groups = [
                            {"Keys": ["Amazon EC2"],
                             "Metrics": {"UnblendedCost": {"Amount": "8.0"}}},
                            {"Keys": ["Data Transfer"],
                             "Metrics": {"UnblendedCost": {"Amount": "2.0"}}},
                        ]
                    results.append({"TimePeriod": {"Start": day}, "Groups": groups})
                return {"ResultsByTime": results}
            if scenario == "no_anomalies_clean":
                results = []
                for i in range(15):
                    day = (base + timedelta(days=i)).strftime("%Y-%m-%d")
                    results.append({"TimePeriod": {"Start": day}, "Groups": [
                        {"Keys": ["Amazon EC2"],
                         "Metrics": {"UnblendedCost": {"Amount": "10.0"}}},
                    ]})
                return {"ResultsByTime": results}

        # --- generic grouped daily (legacy cost cases) ---------------------
        if group_by:
            return {"ResultsByTime": [
                {"TimePeriod": {"Start": "2026-06-01"},
                 "Groups": [
                     {"Keys": ["Amazon EC2"], "Metrics": {"UnblendedCost": {"Amount": "120.0"}}},
                     {"Keys": ["Amazon S3"], "Metrics": {"UnblendedCost": {"Amount": "10.0"}}},
                 ]},
            ]}

        # --- flat monthly (on-demand spend, MTD, last month) ---------------
        if scenario == "savings_plan_modeling":
            return {"ResultsByTime": [
                {"Total": {"UnblendedCost": {"Amount": "1000.0"}}},
            ]}
        return {"ResultsByTime": [
            {"Total": {"UnblendedCost": {"Amount": "130.0"}}},
        ]}
    ce.get_cost_and_usage.side_effect = _ce_get_cost_and_usage

    def _ce_get_cost_forecast(*args, **kwargs):
        if deny:
            raise _client_error("GetCostForecast")
        return {"Total": {"Amount": "200.0"}}
    ce.get_cost_forecast.side_effect = _ce_get_cost_forecast

    def _ce_rightsizing(*args, **kwargs):
        if deny:
            raise _client_error("GetRightsizingRecommendation")
        if scenario == "ec2_rightsize":
            return {"RightsizingRecommendations": [{
                "RightsizingType": "Modify",
                "CurrentInstance": {
                    "ResourceId": "i-abc123",
                    "ResourceDetails": {"EC2ResourceDetails": {"InstanceType": "m5.2xlarge"}},
                },
                "EstimatedMonthlySavings": "140.0",
                "ModifyRecommendationDetail": {"TargetInstances": [{
                    "ResourceDetails": {"EC2ResourceDetails": {"InstanceType": "m5.large"}},
                    "EstimatedMonthlySavings": "140.0",
                }]},
            }]}
        return {"RightsizingRecommendations": []}
    ce.get_rightsizing_recommendation.side_effect = _ce_rightsizing

    def _ce_sp_coverage(*args, **kwargs):
        if deny:
            raise _client_error("GetSavingsPlansCoverage")
        return {"SavingsPlansCoverages": [
            {"Coverage": {"CoveragePercentage": "20.0"}},
        ]}
    ce.get_savings_plans_coverage.side_effect = _ce_sp_coverage

    def _ce_sp_purchase_rec(*args, **kwargs):
        if deny:
            raise _client_error("GetSavingsPlansPurchaseRecommendation")
        if scenario == "savings_plan_modeling":
            term = kwargs.get("TermInYears", "ONE_YEAR")
            # 1-year saves $150/mo; 3-year saves more.
            if term == "THREE_YEARS":
                monthly, pct, hourly = "281.10", "45.0", "0.63"
            else:
                monthly, pct, hourly = "150.00", "30.0", "0.82"
            return {"SavingsPlansPurchaseRecommendation": {
                "SavingsPlansPurchaseRecommendationSummary": {
                    "EstimatedMonthlySavingsAmount": monthly,
                    "EstimatedSavingsPercentage": pct,
                    "HourlyCommitmentToPurchase": hourly,
                },
                "SavingsPlansPurchaseRecommendationDetails": [],
            }}
        return {"SavingsPlansPurchaseRecommendation": {}}
    ce.get_savings_plans_purchase_recommendation.side_effect = _ce_sp_purchase_rec

    clients = {
        "ec2": ec2, "cloudwatch": cw, "elbv2": elbv2,
        "logs": logs, "ecr": ecr, "ce": ce,
    }

    boto3_mock = MagicMock()
    boto3_mock.client.side_effect = lambda name, **kw: clients.get(name, MagicMock())
    return boto3_mock, ClientError


def _gather(scenario: str) -> dict:
    """Run the relevant aws_cost functions under the mocked boto3 and collect
    a result bundle used for assertions."""
    from agent.integrations import aws_cost

    boto3_mock, ClientError = _make_boto3_mock(scenario)

    with patch.object(aws_cost, "_import_boto3", return_value=(boto3_mock, ClientError)):
        idle = aws_cost.get_idle_resources()
        ebs = aws_cost.get_ebs_optimization()
        logs = aws_cost.get_cloudwatch_logs_cost()
        ecr = aws_cost.get_ecr_waste()
        rightsizing = aws_cost.get_rightsizing_recommendations()
        cost = aws_cost.get_cost_and_usage(30)
        reserved = aws_cost.get_reserved_vs_ondemand()
        anomalies = aws_cost.get_spend_anomalies(30)
        savings_plans = aws_cost.get_savings_plan_recommendations()
        cost_by_tag = aws_cost.get_cost_by_tag()

    return {
        "idle": idle, "ebs": ebs, "logs": logs, "ecr": ecr,
        "rightsizing": rightsizing, "cost": cost, "reserved": reserved,
        "anomalies": anomalies, "savings_plans": savings_plans,
        "cost_by_tag": cost_by_tag,
    }


def _check(case: dict, bundle: dict) -> tuple[bool, str]:
    exp = case.get("expected", {})
    idle = bundle["idle"]

    if exp.get("finds_idle_volumes"):
        vols = idle["unattached_volumes"]
        if len(vols) != exp.get("volume_count", len(vols)):
            return False, f"expected {exp.get('volume_count')} volumes, got {len(vols)}"
        if idle["total_monthly_waste"] <= exp.get("total_waste_gt", 0):
            return False, f"waste {idle['total_monthly_waste']} not > {exp.get('total_waste_gt')}"

    if exp.get("finds_gp2_volumes"):
        if not bundle["ebs"]:
            return False, "no gp2 volumes found"
        if exp.get("recommends_gp3") and not all(
            o["recommended_type"] == "gp3" for o in bundle["ebs"]
        ):
            return False, "not all recommend gp3"

    if exp.get("finds_no_retention"):
        groups = bundle["logs"]["groups_no_retention"]
        if len(groups) < exp.get("group_count_ge", 1):
            return False, f"only {len(groups)} no-retention groups"

    if exp.get("finds_rightsizing"):
        if not bundle["rightsizing"]:
            return False, "no rightsizing recommendations"

    if exp.get("finds_unused_eips"):
        eips = idle["unused_eips"]
        if len(eips) < exp.get("eip_count_ge", 1):
            return False, f"only {len(eips)} unused EIPs"

    if exp.get("finds_old_ecr"):
        if not bundle["ecr"]["old_untagged_images"]:
            return False, "no old ECR images found"

    if "total_waste_lt" in exp:
        if idle["total_monthly_waste"] >= exp["total_waste_lt"]:
            return False, f"waste {idle['total_monthly_waste']} not < {exp['total_waste_lt']}"

    if exp.get("no_crash") or exp.get("completes"):
        # Permission-denied scenario: everything must degrade to empty, no raise.
        if idle["total_monthly_waste"] != 0.0:
            return False, "expected zero waste under access-denied"
        if bundle["ebs"] or bundle["rightsizing"]:
            return False, "expected empty results under access-denied"

    # cost-09: anomaly day flagged
    if exp.get("finds_anomaly"):
        days_found = bundle["anomalies"].get("anomaly_days", [])
        if len(days_found) < exp.get("anomaly_count_ge", 1):
            return False, f"expected >= {exp.get('anomaly_count_ge', 1)} anomalies, got {len(days_found)}"

    # cost-10: steady spend, no false positives
    if "anomaly_count" in exp:
        days_found = bundle["anomalies"].get("anomaly_days", [])
        if len(days_found) != exp["anomaly_count"]:
            return False, f"expected {exp['anomaly_count']} anomalies, got {len(days_found)}"

    # cost-11: savings plan recommendation generated
    if exp.get("has_sp_recommendation"):
        sp = bundle["savings_plans"]
        if not sp.get("recommendations"):
            return False, "no savings plan recommendations"
        if sp.get("max_monthly_savings", 0) <= exp.get("max_savings_gt", 0):
            return False, f"max_monthly_savings {sp.get('max_monthly_savings')} not > {exp.get('max_savings_gt')}"

    # cost-12: untagged spend quantified
    if exp.get("finds_untagged"):
        cbt = bundle["cost_by_tag"]
        if cbt.get("untagged_monthly", 0) <= 0:
            return False, "no untagged spend found"
        if cbt.get("untagged_pct", 0) <= exp.get("untagged_pct_gt", 0):
            return False, f"untagged_pct {cbt.get('untagged_pct')} not > {exp.get('untagged_pct_gt')}"

    return True, "ok"


def run_case(case: dict) -> dict:
    """Run a single eval case. Returns {id, name, passed, reason}."""
    try:
        bundle = _gather(case["name"])
        passed, reason = _check(case, bundle)
        return {"id": case["id"], "name": case["name"], "passed": passed, "reason": reason}
    except Exception as exc:  # any raise == failure (esp. for graceful-degradation case)
        return {"id": case["id"], "name": case["name"], "passed": False,
                "reason": f"EXCEPTION: {exc}"}


# Adapter so the existing `agent eval cost` CLI (expects run_evals) keeps working.
def run_evals() -> dict:
    cases = [
        json.loads(l)
        for l in CASES_FILE.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    results = []
    passed = 0
    for case in cases:
        r = run_case(case)
        results.append({
            "id": r["id"],
            "scenario": case.get("description", r["name"]),
            "detail": r["reason"],
            "score": 100 if r["passed"] else 0,
            "passed": r["passed"],
            "error": None if r["passed"] else r["reason"],
        })
        if r["passed"]:
            passed += 1
    return {
        "results": results,
        "totals": {"cases": len(cases), "passed": passed,
                   "total_score": passed * 100},
    }


def main():
    cases = [
        json.loads(l)
        for l in CASES_FILE.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    results = []
    for case in cases:
        result = run_case(case)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  [{status}]  {case['id']}  {case['name']}")
        if not result["passed"]:
            print(f"         -> {result['reason']}")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n  {passed}/{len(results)} passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
