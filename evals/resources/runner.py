"""
Eval harness for ResourceMonitorSkill.

Scores each case on three dimensions:
  - Alert detected correctly   = 40 pts  (right alert_type or correct absence)
  - Severity correct           = 30 pts  (critical/warning/none matches)
  - Fix command valid          = 30 pts  (non-None when fix needed; None when not)

Maximum per case: 100 points.
A case "passes" when score >= 70.

Run standalone:
    $env:PYTHONPATH = "src"
    uv run python evals/resources/runner.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

CASES_FILE = Path(__file__).parent / "cases.jsonl"

# Alert type groupings for partial credit
_OOM_ALERTS  = {"OOM_RISK", "MEM_HIGH"}
_CPU_ALERTS  = {"CPU_HIGH"}
_NODE_ALERTS = {"MEM_HIGH", "CPU_HIGH"}


def _load_cases() -> list[dict]:
    with open(CASES_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _score_case(case: dict, alerts: list) -> dict:
    """Score a single case given the list of ResourceAlert objects returned."""
    exp_type = case.get("expected_alert_type")
    exp_sev  = case.get("expected_severity")
    exp_fix  = case.get("expected_fix_needed", False)

    score = 0
    alert_pass = severity_pass = fix_pass = False

    # No alert expected
    if exp_type is None:
        alert_pass   = len(alerts) == 0
        severity_pass = True  # vacuously true
        fix_pass      = True
        if alert_pass:
            score = 100
        else:
            score = 0  # false positive
        return {
            "alert_pass":    alert_pass,
            "severity_pass": severity_pass,
            "fix_pass":      fix_pass,
            "score":         score,
        }

    # Alert expected — check if we got any matching alert
    matching = [a for a in alerts if _type_matches(a.alert_type, exp_type)]
    alert_pass = len(matching) > 0
    if alert_pass:
        score += 40

    # Severity check on the first matching alert
    if matching and exp_sev:
        severity_pass = any(a.severity == exp_sev for a in matching)
        if severity_pass:
            score += 30

    # Fix command check
    if exp_fix:
        # We count as valid if the recommendation string is non-empty
        # (fix_command may be None at alert time, generated at fix-command time)
        fix_pass = any(a.recommendation for a in matching)
        if fix_pass:
            score += 30
    else:
        fix_pass  = True
        score    += 30

    return {
        "alert_pass":    alert_pass,
        "severity_pass": severity_pass,
        "fix_pass":      fix_pass,
        "score":         score,
    }


def _type_matches(actual: str, expected: str) -> bool:
    """Allow partial credit: OOM_RISK and MEM_HIGH are both valid for memory issues."""
    if actual == expected:
        return True
    if expected in _OOM_ALERTS and actual in _OOM_ALERTS:
        return True
    if expected in _CPU_ALERTS and actual in _CPU_ALERTS:
        return True
    return False


def run_evals() -> dict:
    """
    Run all resource eval cases with mocked kubectl and return structured results.

    Returns:
        {
          "results": [
            {
              "id", "scenario", "expected_alert_type", "expected_severity",
              "alerts_generated", "alert_pass", "severity_pass", "fix_pass",
              "score", "passed"
            }, ...
          ],
          "totals": {
            "cases", "passed", "total_score",
            "alert_hits", "severity_hits", "fix_hits"
          }
        }
    """
    from agent.core.models import NodeResourceMetrics, PodResourceMetrics
    from agent.skills.resource_monitor import ResourceMonitorSkill

    cases   = _load_cases()
    results = []
    skill   = ResourceMonitorSkill()

    total_score = alert_hits = severity_hits = fix_hits = passed_count = 0

    for case in cases:
        # Build mock metric objects from case data
        mock_nodes = [NodeResourceMetrics(**n) for n in case["mock_nodes"]]
        mock_pods  = [PodResourceMetrics(**p) for p in case["mock_pods"]]

        # Run through the skill's alert generator (no kubectl needed)
        alerts = skill._generate_alerts(mock_nodes, mock_pods)

        scores = _score_case(case, alerts)
        passed = scores["score"] >= 70

        if scores["alert_pass"]:
            alert_hits += 1
        if scores["severity_pass"]:
            severity_hits += 1
        if scores["fix_pass"]:
            fix_hits += 1
        if passed:
            passed_count += 1
        total_score += scores["score"]

        results.append({
            "id":                  case["id"],
            "scenario":            case["scenario"],
            "expected_alert_type": case.get("expected_alert_type"),
            "expected_severity":   case.get("expected_severity"),
            "alerts_generated":    [a.alert_type for a in alerts],
            "alert_pass":          scores["alert_pass"],
            "severity_pass":       scores["severity_pass"],
            "fix_pass":            scores["fix_pass"],
            "score":               scores["score"],
            "passed":              passed,
        })

    return {
        "results": results,
        "totals": {
            "cases":         len(cases),
            "passed":        passed_count,
            "total_score":   total_score,
            "alert_hits":    alert_hits,
            "severity_hits": severity_hits,
            "fix_hits":      fix_hits,
        },
    }


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    data    = run_evals()
    results = data["results"]
    totals  = data["totals"]

    print(f"\nResource Monitor Eval — {totals['cases']} cases\n")
    print(f"{'ID':<10} {'Scenario':<36} {'Score':>6} {'Pass'}")
    print("-" * 65)
    for r in results:
        icon = "PASS" if r["passed"] else "FAIL"
        print(
            f"{r['id']:<10} {r['scenario'][:34]:<36} "
            f"{r['score']:>5}/100  {icon}"
        )

    n   = totals["cases"]
    pct = round(totals["passed"] / n * 100) if n else 0
    avg = round(totals["total_score"] / n) if n else 0
    print(f"\nPassed: {totals['passed']}/{n}  ({pct}%)   avg score: {avg}/100")
    print(
        f"Alert hits: {totals['alert_hits']}/{n}   "
        f"Severity: {totals['severity_hits']}/{n}   "
        f"Fix valid: {totals['fix_hits']}/{n}"
    )
    sys.exit(0 if pct >= 80 else 1)
