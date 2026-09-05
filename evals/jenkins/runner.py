"""
Eval harness for JenkinsSkill.

Three case kinds, mirroring the three things the skill actually does:
  - "diagnose"      pattern-matched failures — scored on problem_type/fix_action/
                     auto_fixable, no Claude call (fast, free, deterministic).
  - "diagnose_live" a failure with no known regex signature — exercises the
                     live-Claude fallback path. Scored leniently (any of a
                     few plausible problem types) since it's real reasoning.
  - "scan"          exercises JenkinsScanReport aggregation (offline nodes,
                     stuck queue, health score) with mocked Jenkins API calls.
  - "patterns"      exercises detect_patterns() against a mocked incident
                     history — real Claude call, checks at least one pattern
                     is found.

Run standalone:
    $env:PYTHONPATH = "src"
    uv run python evals/jenkins/runner.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

CASES_FILE = Path(__file__).parent / "cases.jsonl"


# ---------------------------------------------------------------------------
# Core runner — returns structured data, prints nothing
# ---------------------------------------------------------------------------

def run_evals() -> dict:
    from agent.core.models import BuildInfo, JenkinsJob, JenkinsNode, Memory, QueueItem
    from agent.observability.costs import get_session_total
    from agent.skills.jenkins import JenkinsSkill

    skill = JenkinsSkill()
    cases = [
        json.loads(line)
        for line in CASES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    tokens_before = get_session_total()
    results: list[dict] = []

    # Never let eval runs write real memory rows.
    with patch("agent.skills.jenkins.remember", return_value=None):
        for case in cases:
            kind = case["kind"]

            if kind in ("diagnose", "diagnose_live"):
                results.append(_run_diagnose_case(skill, case, BuildInfo))
            elif kind == "scan":
                results.append(_run_scan_case(skill, case, JenkinsJob, JenkinsNode, QueueItem))
            elif kind == "patterns":
                results.append(_run_patterns_case(skill, case, Memory))
            else:
                results.append({"id": case["id"], "description": case["description"],
                                 "kind": kind, "passed": False, "detail": f"unknown kind {kind!r}"})

    tokens_after = get_session_total()
    n = len(results)
    passed_count = sum(1 for r in results if r["passed"])

    return {
        "results": results,
        "totals": {
            "cases":  n,
            "passed": passed_count,
            "pass_rate": passed_count / n if n else 0.0,
            "tokens_in":  tokens_after["input_tokens"]  - tokens_before["input_tokens"],
            "tokens_out": tokens_after["output_tokens"] - tokens_before["output_tokens"],
            "cost_usd": round(
                tokens_after["total_cost_usd"] - tokens_before["total_cost_usd"], 6
            ),
        },
    }


def _run_diagnose_case(skill, case: dict, BuildInfo) -> dict:
    build = BuildInfo(
        number=case["build_number"], status=case["mock_status"],
        causes=case["mock_causes"], changes=case["mock_changes"], node=case["mock_node"],
    )
    history = [BuildInfo(**b) for b in case["mock_history"]]

    with patch("agent.skills.jenkins.jk.get_job_config", return_value="(pipeline { ... })"):
        diagnosis = skill.diagnose(case["job_name"], build, case["mock_log"], history, [])

    if case["kind"] == "diagnose":
        type_pass = diagnosis.problem_type.value == case["expected_problem_type"]
        fix_pass  = diagnosis.fix_action.value == case["expected_fix_action"]
        auto_pass = diagnosis.auto_fixable == case["expected_auto_fixable"]
        passed = type_pass and fix_pass and auto_pass
        detail = f"type={diagnosis.problem_type.value} fix={diagnosis.fix_action.value} auto={diagnosis.auto_fixable}"
    else:  # diagnose_live — lenient: any plausible type, and it produced a real fix action
        type_pass = diagnosis.problem_type.value in case["expected_problem_type_any"]
        fix_pass  = diagnosis.fix_action.value != "MANUAL_ONLY" or diagnosis.confidence == "low"
        auto_pass = True
        passed = type_pass and fix_pass
        detail = (f"type={diagnosis.problem_type.value} (any of {case['expected_problem_type_any']}) "
                  f"fix={diagnosis.fix_action.value} conf={diagnosis.confidence}")

    return {
        "id": case["id"], "description": case["description"], "kind": case["kind"],
        "type_pass": type_pass, "fix_pass": fix_pass, "auto_pass": auto_pass,
        "passed": passed, "detail": detail,
    }


def _run_scan_case(skill, case: dict, JenkinsJob, JenkinsNode, QueueItem) -> dict:
    jobs    = [JenkinsJob(**j) for j in case["mock_jobs"]]
    offline = [JenkinsNode(**n) for n in case["mock_offline_nodes"]]
    stuck   = [QueueItem(**q) for q in case["mock_stuck_queue"]]

    with patch("agent.skills.jenkins.jk.get_all_jobs", return_value=jobs), \
         patch("agent.skills.jenkins.jk.get_offline_nodes", return_value=offline), \
         patch("agent.skills.jenkins.jk.get_stuck_queue_items", return_value=stuck):
        report = skill.scan()

    empty_pass = (len(report.diagnoses) == 0) == case["expected_diagnoses_empty"]
    offline_pass = report.offline_nodes == case["expected_offline_nodes"]
    stuck_pass = report.stuck_queue_items == case["expected_stuck_queue_items"]

    score_pass = True
    if "expected_health_score_max" in case:
        score_pass = score_pass and report.health_score <= case["expected_health_score_max"]
    if "expected_health_score_min" in case:
        score_pass = score_pass and report.health_score >= case["expected_health_score_min"]

    passed = empty_pass and offline_pass and stuck_pass and score_pass
    detail = (f"diagnoses={len(report.diagnoses)} offline={report.offline_nodes} "
              f"stuck={report.stuck_queue_items} health={report.health_score}")

    return {
        "id": case["id"], "description": case["description"], "kind": case["kind"],
        "type_pass": empty_pass, "fix_pass": offline_pass and stuck_pass, "auto_pass": score_pass,
        "passed": passed, "detail": detail,
    }


def _run_patterns_case(skill, case: dict, Memory) -> dict:
    incidents = [
        Memory(id=f"mem-{i}", content=text, source="jenkins")
        for i, text in enumerate(case["mock_incidents"])
    ]
    with patch("agent.skills.jenkins.retrieve_context", return_value=incidents):
        patterns = skill.detect_patterns(days=30)

    count_pass = len(patterns) >= case["expected_min_patterns"]
    mentions_job = any(
        case["job_name"] in p.title or case["job_name"] in p.likely_cause
        or case["job_name"] in p.recommendation or case["job_name"] in p.affected_jobs
        for p in patterns
    )
    passed = count_pass and mentions_job
    detail = f"patterns={len(patterns)} mentions_job={mentions_job}"

    return {
        "id": case["id"], "description": case["description"], "kind": case["kind"],
        "type_pass": count_pass, "fix_pass": mentions_job, "auto_pass": True,
        "passed": passed, "detail": detail,
    }


# ---------------------------------------------------------------------------
# Standalone display
# ---------------------------------------------------------------------------

def _main() -> None:
    from rich.console import Console
    from rich.rule import Rule
    from rich.table import Table

    console = Console()
    console.print()
    console.print(Rule("[bold]Jenkins Skill Eval Suite[/bold]"))
    console.print()

    with console.status("[bold green]Running 12 eval cases...", spinner="dots"):
        data = run_evals()

    results = data["results"]
    totals  = data["totals"]

    table = Table(title=f"Jenkins Eval Results  [dim]({totals['cases']} cases)[/dim]",
                  show_lines=True, header_style="bold cyan", border_style="dim")
    table.add_column("ID",    width=12)
    table.add_column("Kind",  width=14)
    table.add_column("Detail", max_width=50)
    table.add_column("Pass",  justify="center", width=6)

    for r in results:
        status = "[bold green]PASS[/bold green]" if r["passed"] else "[bold red]FAIL[/bold red]"
        table.add_row(r["id"], r["kind"], r["detail"], status)

    console.print(table)
    console.print()

    n, passed = totals["cases"], totals["passed"]
    pct = round(passed / n * 100) if n else 0
    color = "green" if pct >= 85 else "yellow" if pct >= 60 else "red"
    console.print(f"  Pass rate : [{color}]{passed}/{n} ({pct}%)[/{color}]")
    console.print()
    console.print(Rule(style="dim"))
    console.print(
        f"  Tokens — in: [cyan]{totals['tokens_in']:,}[/cyan]  "
        f"out: [cyan]{totals['tokens_out']:,}[/cyan]   "
        f"Cost: [green]${totals['cost_usd']:.6f}[/green]",
        highlight=False,
    )
    console.print()

    sys.exit(0 if pct >= 85 else 1)


if __name__ == "__main__":
    _main()
