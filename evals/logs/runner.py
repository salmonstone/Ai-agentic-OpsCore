"""
Eval harness for LogAnalysisSkill.

Mocks kubectl so no live cluster is needed.
Tests that Claude correctly identifies error_type and root cause.

Scoring per case:
  - error_type matches expected                     = 40 pts
  - root_cause contains expected keyword            = 30 pts
  - fix_command generated (when fix expected)       = 20 pts
  - key_log_lines non-empty                         = 10 pts

Pass threshold: 70/100. Run: uv run python evals/logs/runner.py
"""
from __future__ import annotations

import json
import sys
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

CASES_FILE = Path(__file__).parent / "cases.jsonl"


def _load_cases() -> list[dict]:
    with open(CASES_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _make_pod_logs(case: dict):
    from agent.core.models import PodLogs
    pl = PodLogs(pod=case["pod"], namespace=case["namespace"])
    if case["current_logs"]:
        pl.current_logs[case["container_states"][0]["name"]] = case["current_logs"]
    if case["previous_logs"]:
        pl.previous_logs[case["container_states"][0]["name"]] = case["previous_logs"]
        pl.has_previous = bool(case["previous_logs"])
    pl.containers = [cs["name"] for cs in case["container_states"]]
    pl.log_lines_count = len(case["current_logs"].splitlines()) + len(case["previous_logs"].splitlines())
    return pl


def _make_container_states(case: dict):
    from agent.core.models import ContainerState
    return [
        ContainerState(
            name=cs["name"], ready=cs["ready"], restart_count=cs["restart_count"],
            state=cs["state"], last_state=cs.get("last_state", ""),
            exit_code=cs.get("exit_code"), reason=cs.get("reason"),
        )
        for cs in case["container_states"]
    ]


def _score_case(case: dict, analysis) -> dict:
    pts = 0

    # error_type match
    expected_type = case["expected_error_type"].upper()
    actual_type   = (analysis.error_type or "").upper()
    type_pass = actual_type == expected_type
    if type_pass:
        pts += 40

    # root_cause keyword
    expected_kw  = case["expected_root_cause_contains"].lower()
    actual_cause = (analysis.root_cause or "").lower()
    cause_pass = not expected_kw or expected_kw in actual_cause
    if cause_pass:
        pts += 30

    # fix_command generated (when fix expected)
    expected_fix = case["expected_fix_contains"].lower()
    fix_pass = True
    if expected_fix:
        fix_text = ((analysis.fix_command or "") + (analysis.suggested_fix or "")).lower()
        fix_pass = expected_fix in fix_text
    if fix_pass:
        pts += 20

    # key_log_lines present
    lines_pass = len(analysis.key_log_lines or []) > 0
    if lines_pass:
        pts += 10

    detail = (
        f"type={actual_type}({'ok' if type_pass else 'FAIL:{expected_type}'})"
        f" cause={'ok' if cause_pass else f'MISS:{expected_kw}'}"
        f" fix={'ok' if fix_pass else f'MISS:{expected_fix}'}"
        f" lines={'ok' if lines_pass else 'empty'}"
    )
    return {"score": pts, "passed": pts >= 70, "detail": detail}


def run_evals() -> dict:
    from agent.skills.log_analysis import LogAnalysisSkill
    from agent.integrations.kubectl import extract_error_patterns

    cases   = _load_cases()
    skill   = LogAnalysisSkill()
    results = []
    total_score = passed = 0

    for case in cases:
        try:
            pod_logs   = _make_pod_logs(case)
            con_states = _make_container_states(case)
            all_text   = "\n".join(list(pod_logs.current_logs.values()) +
                                    list(pod_logs.previous_logs.values()))
            patterns   = extract_error_patterns(all_text)
            analysis   = skill._call_claude(
                case["pod"], case["namespace"],
                pod_logs, con_states, patterns, "",
            )
            scored = _score_case(case, analysis)
            err    = None
        except Exception as exc:
            scored = {"score": 0, "passed": False, "detail": f"ERROR: {exc}"}
            err    = str(exc)

        total_score += scored["score"]
        if scored["passed"]:
            passed += 1

        results.append({
            "id":       case["id"],
            "scenario": case["scenario"],
            "detail":   scored["detail"],
            "score":    scored["score"],
            "passed":   scored["passed"],
            "error":    err,
        })

    return {
        "results": results,
        "totals": {"cases": len(cases), "passed": passed, "total_score": total_score},
    }


if __name__ == "__main__":
    data   = run_evals()
    totals = data["totals"]
    n      = totals["cases"]
    pct    = round(totals["passed"] / n * 100) if n else 0
    avg    = round(totals["total_score"] / n) if n else 0

    print(f"\nLog Analysis Eval Suite  ({n} cases)")
    print("-" * 70)
    for r in data["results"]:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{mark}] {r['id']}  {r['score']:3d}/100  {r['detail']}")
        if r["error"]:
            print(f"         ERROR: {r['error']}")
    print("-" * 70)
    print(f"  Pass rate: {totals['passed']}/{n} ({pct}%)")
    print(f"  Avg score: {avg}/100")
    sys.exit(0 if pct >= 80 else 1)
