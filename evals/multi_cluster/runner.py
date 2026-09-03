"""
Eval harness for MultiClusterSkill.

Tests environment detection, provider detection, region detection,
broadcast safety, and score calculation — no live cluster needed.

Run standalone:
    $env:PYTHONPATH = "src"
    uv run python evals/multi_cluster/runner.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

CASES_FILE = Path(__file__).parent / "cases.jsonl"


def _load_cases() -> list[dict]:
    with open(CASES_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def run_evals() -> dict:
    from agent.integrations.kubectl import (
        _detect_environment,
        _detect_provider,
        _detect_region,
    )
    from agent.skills.multi_cluster import MultiClusterSkill
    from agent.skills.security import SecurityAuditSkill
    from agent.core.models import SecurityFinding

    cases   = _load_cases()
    results = []
    passed_count = total_score = 0

    for case in cases:
        cid      = case["id"]
        scenario = case["scenario"]
        pts      = 0
        detail   = ""

        # Environment detection
        if "expected_environment" in case:
            got = _detect_environment(case["context_name"])
            ok  = got == case["expected_environment"]
            pts = 100 if ok else 0
            detail = f"got={got} expected={case['expected_environment']}"

        # Provider detection
        elif "expected_provider" in case:
            got = _detect_provider(case["context_name"], "")
            ok  = got == case["expected_provider"]
            pts = 100 if ok else 0
            detail = f"got={got} expected={case['expected_provider']}"

        # Region detection
        elif "expected_region" in case:
            got = _detect_region(case["context_name"])
            ok  = got == case["expected_region"]
            pts = 100 if ok else 0
            detail = f"got={got} expected={case['expected_region']}"

        # Broadcast safety
        elif "expected_blocked" in case:
            skill = MultiClusterSkill()
            blocked = False
            try:
                skill.run_readonly_on_all(case["command"], [])
            except PermissionError:
                blocked = True
            ok  = blocked == case["expected_blocked"]
            pts = 100 if ok else 0
            detail = f"blocked={blocked} expected={case['expected_blocked']}"

        # Score calculation
        elif "expected_score" in case:
            skill    = SecurityAuditSkill()
            findings = [
                SecurityFinding(
                    id=f"X{i}", severity=sev, category="test",
                    title="test", description="test",
                    affected_resource="test",
                )
                for i, sev in enumerate(case["findings_severity"])
            ]
            got = skill.calculate_score(findings)
            ok  = got == case["expected_score"]
            pts = 100 if ok else 0
            detail = f"got={got} expected={case['expected_score']}"

        else:
            ok  = True
            pts = 100
            detail = "no assertion"

        passed = pts >= 70
        if passed:
            passed_count += 1
        total_score += pts

        results.append({
            "id":      cid,
            "scenario": scenario,
            "score":   pts,
            "passed":  passed,
            "detail":  detail,
        })

    return {
        "results": results,
        "totals": {
            "cases":       len(cases),
            "passed":      passed_count,
            "total_score": total_score,
        },
    }


if __name__ == "__main__":
    data    = run_evals()
    results = data["results"]
    totals  = data["totals"]

    print(f"\nMulti-Cluster Eval — {totals['cases']} cases\n")
    print(f"{'ID':<10} {'Scenario':<46} {'Score':>6} {'Detail':<30} {'Pass'}")
    print("-" * 100)
    for r in results:
        icon = "PASS" if r["passed"] else "FAIL"
        print(
            f"{r['id']:<10} {r['scenario'][:44]:<46} "
            f"{r['score']:>5}/100  {r['detail']:<30} {icon}"
        )

    n   = totals["cases"]
    pct = round(totals["passed"] / n * 100) if n else 0
    avg = round(totals["total_score"] / n) if n else 0
    print(f"\nPassed: {totals['passed']}/{n}  ({pct}%)   avg score: {avg}/100")
    sys.exit(0 if pct >= 80 else 1)
