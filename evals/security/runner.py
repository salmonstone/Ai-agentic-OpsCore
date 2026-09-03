"""
Eval harness for SecurityAuditSkill.

Scores each case on three dimensions:
  - Score in expected range  = 50 pts
  - production_ready correct = 30 pts
  - Findings count non-zero when expected = 20 pts

Maximum per case: 100 points. Pass threshold: 70.

Run standalone:
    $env:PYTHONPATH = "src"
    uv run python evals/security/runner.py
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


def _score_case(case: dict, score: int, prod_ready: bool, finding_count: int) -> dict:
    lo, hi = case["expected_score_range"]
    exp_prod = case["expected_production_ready"]
    exp_has_findings = len(case["findings"]) > 0

    pts = 0
    score_pass = lo <= score <= hi
    prod_pass  = prod_ready == exp_prod
    count_pass = (finding_count > 0) == exp_has_findings

    if score_pass:
        pts += 50
    if prod_pass:
        pts += 30
    if count_pass:
        pts += 20

    return {
        "score_pass":  score_pass,
        "prod_pass":   prod_pass,
        "count_pass":  count_pass,
        "total_score": pts,
        "passed":      pts >= 70,
    }


def run_evals() -> dict:
    from agent.core.models import SecurityFinding
    from agent.skills.security import SecurityAuditSkill

    cases   = _load_cases()
    skill   = SecurityAuditSkill()
    results = []
    passed_count = total_score = 0

    for case in cases:
        findings = [SecurityFinding(**f) for f in case["findings"]]
        computed_score = skill.calculate_score(findings)
        prod_ready     = computed_score > 80
        scores         = _score_case(case, computed_score, prod_ready, len(findings))

        if scores["passed"]:
            passed_count += 1
        total_score += scores["total_score"]

        results.append({
            "id":             case["id"],
            "scenario":       case["scenario"],
            "computed_score": computed_score,
            "expected_range": case["expected_score_range"],
            "prod_ready":     prod_ready,
            "findings_count": len(findings),
            **scores,
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

    print(f"\nSecurity Audit Eval — {totals['cases']} cases\n")
    print(f"{'ID':<10} {'Scenario':<42} {'Computed':>9} {'Expected':>12} {'Score':>6} {'Pass'}")
    print("-" * 88)
    for r in results:
        lo, hi = r["expected_range"]
        icon   = "PASS" if r["passed"] else "FAIL"
        print(
            f"{r['id']:<10} {r['scenario'][:40]:<42} "
            f"{r['computed_score']:>8}/100 "
            f"[{lo}-{hi}]{' ':>4} "
            f"{r['total_score']:>5}/100  {icon}"
        )

    n   = totals["cases"]
    pct = round(totals["passed"] / n * 100) if n else 0
    avg = round(totals["total_score"] / n) if n else 0
    print(f"\nPassed: {totals['passed']}/{n}  ({pct}%)   avg score: {avg}/100")
    sys.exit(0 if pct >= 80 else 1)
