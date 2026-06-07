"""
Eval harness for K8sSkill.diagnose_pod().

Scores each case on three dimensions:
  - problem_type match   = 1 pt  (does Claude map to the right k8s error class?)
  - confidence pass      = 1 pt  (high or medium for real problems; low for healthy)
  - fix_command present  = 1 pt  (non-null for problematic pods)

A case "passes" when all applicable checks succeed.

Run standalone:
    $env:PYTHONPATH = "src"
    uv run python evals/k8s/runner.py
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
    """
    Run all k8s eval cases with mocked kubectl and return results.

    Returns:
        {
          "results": [
            {
              "id", "description", "pod",
              "expected_problem_type", "actual_problem_type",
              "confidence", "fix_command",
              "problem_type_pass", "confidence_pass", "fix_pass",
              "passed"
            }, ...
          ],
          "totals": {
            "cases", "passed", "problem_type_hits",
            "confidence_hits", "fix_hits", "pass_rate",
            "tokens_in", "tokens_out", "cost_usd"
          }
        }
    """
    from agent.core.models import PodInfo
    from agent.observability.costs import get_session_total
    from agent.skills.k8s import K8sSkill

    skill = K8sSkill()
    cases = [
        json.loads(line)
        for line in CASES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    tokens_before = get_session_total()
    results: list[dict] = []

    for case in cases:
        pod_info = PodInfo(**case["pod_info"])
        is_healthy = case["expected_problem_type"] is None

        with patch("agent.skills.k8s.get_pod_logs",   return_value=case["mock_logs"]), \
             patch("agent.skills.k8s.get_pod_events",  return_value=case["mock_events"]), \
             patch("agent.skills.k8s.describe_pod",    return_value=case["mock_describe"]):
            diagnosis = skill.diagnose_pod(pod_info)

        actual_type = diagnosis.problem_type.value
        expected_type = case["expected_problem_type"]
        expected_fix = case["expected_fix_contains"]
        conf = diagnosis.confidence.lower()

        # --- score each dimension ---
        problem_type_pass = (
            True if is_healthy          # no expected type for healthy pods
            else actual_type == expected_type
        )

        confidence_pass = (
            conf == "low"               # healthy pods should have low confidence
            if is_healthy
            else conf in ("high", "medium")
        )

        fix_pass = (
            diagnosis.fix_command is None or conf == "low"
            if is_healthy
            else (
                diagnosis.fix_command is not None
                and expected_fix is not None
                and expected_fix.lower() in (
                    (diagnosis.fix_command or "").lower()
                    + " "
                    + diagnosis.suggested_fix.lower()
                )
            )
        )

        passed = problem_type_pass and confidence_pass and fix_pass

        results.append({
            "id":                   case["id"],
            "description":          case["description"],
            "pod":                  pod_info.name,
            "expected_problem_type": expected_type,
            "actual_problem_type":  actual_type,
            "confidence":           conf,
            "fix_command":          diagnosis.fix_command,
            "problem_type_pass":    problem_type_pass,
            "confidence_pass":      confidence_pass,
            "fix_pass":             fix_pass,
            "passed":               passed,
        })

    tokens_after = get_session_total()
    n = len(results)
    passed_count       = sum(1 for r in results if r["passed"])
    problem_type_hits  = sum(1 for r in results if r["problem_type_pass"])
    confidence_hits    = sum(1 for r in results if r["confidence_pass"])
    fix_hits           = sum(1 for r in results if r["fix_pass"])

    return {
        "results": results,
        "totals": {
            "cases":              n,
            "passed":             passed_count,
            "problem_type_hits":  problem_type_hits,
            "confidence_hits":    confidence_hits,
            "fix_hits":           fix_hits,
            "pass_rate":          passed_count / n if n else 0.0,
            "tokens_in":          tokens_after["input_tokens"]  - tokens_before["input_tokens"],
            "tokens_out":         tokens_after["output_tokens"] - tokens_before["output_tokens"],
            "cost_usd":           round(
                tokens_after["total_cost_usd"] - tokens_before["total_cost_usd"], 6
            ),
        },
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
    console.print(Rule("[bold]K8s Skill Eval Suite[/bold]"))
    console.print()

    with console.status("[bold green]Running 10 eval cases...", spinner="dots"):
        data = run_evals()

    results = data["results"]
    totals  = data["totals"]

    table = Table(
        title=f"K8s Eval Results  [dim]({totals['cases']} cases)[/dim]",
        show_lines=True,
        header_style="bold cyan",
        border_style="dim",
    )
    table.add_column("ID",       width=8)
    table.add_column("Pod",      max_width=28, no_wrap=True)
    table.add_column("Expected", width=22)
    table.add_column("Got",      width=22)
    table.add_column("Conf",     justify="center", width=8)
    table.add_column("Type", justify="center", width=6)
    table.add_column("Conf", justify="center", width=6)
    table.add_column("Fix",  justify="center", width=6)
    table.add_column("Pass", justify="center", width=6)

    for r in results:
        exp = r["expected_problem_type"] or "[dim]healthy[/dim]"
        got = r["actual_problem_type"]
        type_ok = "[green]OK[/green]" if r["problem_type_pass"] else "[red]FAIL[/red]"
        conf_ok = "[green]OK[/green]" if r["confidence_pass"]   else "[red]FAIL[/red]"
        fix_ok  = "[green]OK[/green]" if r["fix_pass"]          else "[red]FAIL[/red]"
        status  = "[bold green]PASS[/bold green]" if r["passed"] else "[bold red]FAIL[/bold red]"
        conf_color = (
            "green"  if r["confidence"] == "high"   else
            "yellow" if r["confidence"] == "medium"  else
            "dim"
        )
        table.add_row(
            r["id"],
            r["pod"][:26],
            exp,
            got,
            f"[{conf_color}]{r['confidence']}[/{conf_color}]",
            type_ok, conf_ok, fix_ok, status,
        )

    console.print(table)
    console.print()

    n       = totals["cases"]
    passed  = totals["passed"]
    pct     = round(passed / n * 100) if n else 0
    color   = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"

    console.print(f"  Pass rate      : [{color}]{passed}/{n} ({pct}%)[/{color}]")
    console.print(f"  Problem type   : {totals['problem_type_hits']}/{n}")
    console.print(f"  Confidence     : {totals['confidence_hits']}/{n}")
    console.print(f"  Fix present    : {totals['fix_hits']}/{n}")
    console.print()
    console.print(Rule(style="dim"))
    console.print(
        f"  Tokens — in: [cyan]{totals['tokens_in']:,}[/cyan]  "
        f"out: [cyan]{totals['tokens_out']:,}[/cyan]   "
        f"Cost: [green]${totals['cost_usd']:.6f}[/green]",
        highlight=False,
    )
    console.print()

    sys.exit(0 if pct >= 80 else 1)


if __name__ == "__main__":
    _main()
