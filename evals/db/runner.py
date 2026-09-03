"""
Eval harness for Database Health (RDS/Aurora) monitoring.

Cases db-01..db-05 exercise `analyze_rds_health` directly with synthetic
metrics. Case db-06 mocks all boto3 RDS calls to raise AccessDenied and
verifies `get_rds_instances` degrades to an empty list without crashing.

A case passes when its expectation is fully met (score 100), else 0.

Run standalone:
    $env:PYTHONPATH = "src"
    uv run python evals/db/runner.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

CASES_FILE = Path(__file__).parent / "cases.jsonl"

# Synthetic metric fixtures keyed by case id. Each is a full metrics dict.
_BASE_METRICS = {
    "cpu_pct": 20.0,
    "connections_avg": 30.0,
    "connections_max": 45.0,
    "free_storage_gb": 50.0,
    "read_latency_ms": 2.0,
    "write_latency_ms": 1.5,
    "freeable_memory_mb": 2048.0,
    "replica_lag_sec": 0.0,
}


def _metrics(**overrides) -> dict:
    m = dict(_BASE_METRICS)
    m.update(overrides)
    return m


_INSTANCE = {
    "id": "test-db",
    "class": "db.t3.medium",
    "engine": "postgres",
    "status": "available",
    "storage_gb": 100,
}


def _load_cases() -> list[dict]:
    with open(CASES_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _run_analyze_case(case: dict) -> dict:
    """Run an analyze_rds_health-backed case and score it."""
    from agent.integrations.rds import analyze_rds_health

    cid = case["id"]
    expected = case["expected"]

    if cid == "db-01":
        metrics = _metrics()
    elif cid == "db-02":
        # "High CPU" critical scenario (>95% triggers critical severity).
        metrics = _metrics(cpu_pct=96.0)
    elif cid == "db-03":
        metrics = _metrics(free_storage_gb=2.0)
    elif cid == "db-04":
        metrics = _metrics(connections_max=190.0)
    elif cid == "db-05":
        metrics = _metrics(replica_lag_sec=90.0)
    else:
        metrics = _metrics()

    issues = analyze_rds_health(_INSTANCE["id"], metrics, _INSTANCE)

    detail = ""
    score = 0
    if "issues" in expected:  # healthy case — expect a specific issue count
        ok = len(issues) == expected["issues"] and (
            _status(issues) == expected.get("status", "healthy")
        )
        score = 100 if ok else 0
        detail = f"issues={len(issues)} status={_status(issues)}"
    else:  # expect a specific issue type + severity
        want_type = expected["issue_type"]
        want_sev = expected["severity"]
        match = next((i for i in issues if i["type"] == want_type), None)
        ok = match is not None and match["severity"] == want_sev
        score = 100 if ok else 0
        got = f"{match['type']}/{match['severity']}" if match else "none"
        detail = f"want={want_type}/{want_sev} got={got}"

    return {"score": score, "detail": detail}


def _status(issues: list[dict]) -> str:
    if any(i.get("severity") == "critical" for i in issues):
        return "critical"
    if any(i.get("severity") == "warning" for i in issues):
        return "warning"
    return "healthy"


def _run_access_denied_case(case: dict) -> dict:
    """db-06: all boto3 calls raise AccessDenied -> empty list, no crash."""
    from botocore.exceptions import ClientError

    err = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}},
        "DescribeDBInstances",
    )

    fake_client = MagicMock()
    fake_client.describe_db_instances.side_effect = err

    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = fake_client

    detail = ""
    score = 0
    try:
        with patch("agent.integrations.rds._import_boto3", return_value=(fake_boto3, ClientError)):
            from agent.integrations.rds import get_rds_instances
            instances = get_rds_instances("us-east-1")
        ok = instances == []
        score = 100 if ok else 0
        detail = f"instances={len(instances)} (no crash)"
    except Exception as exc:  # any crash = fail
        score = 0
        detail = f"crashed: {exc}"

    return {"score": score, "detail": detail}


def run_evals() -> dict:
    """Run all DB-health eval cases. Returns structured results + totals."""
    cases = _load_cases()
    results = []
    passed_count = 0
    total_score = 0

    for case in cases:
        if case["id"] == "db-06":
            scored = _run_access_denied_case(case)
        else:
            scored = _run_analyze_case(case)

        passed = scored["score"] >= 70
        if passed:
            passed_count += 1
        total_score += scored["score"]

        results.append({
            "id": case["id"],
            "scenario": case["name"],
            "detail": scored["detail"],
            "score": scored["score"],
            "passed": passed,
        })

    return {
        "results": results,
        "totals": {
            "cases": len(cases),
            "passed": passed_count,
            "total_score": total_score,
        },
    }


def main() -> bool:
    """Print results table and return True if pass-rate >= 80%."""
    data = run_evals()
    results = data["results"]
    totals = data["totals"]

    print(f"\nDatabase Health Eval — {totals['cases']} cases\n")
    print(f"{'ID':<8} {'Scenario':<22} {'Score':>6}  {'Pass'}  Detail")
    print("-" * 78)
    for r in results:
        icon = "PASS" if r["passed"] else "FAIL"
        print(
            f"{r['id']:<8} {r['scenario'][:20]:<22} "
            f"{r['score']:>5}/100  {icon}  {r['detail']}"
        )

    n = totals["cases"]
    pct = round(totals["passed"] / n * 100) if n else 0
    avg = round(totals["total_score"] / n) if n else 0
    print(f"\nPassed: {totals['passed']}/{n}  ({pct}%)   avg score: {avg}/100")
    return pct >= 80


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
