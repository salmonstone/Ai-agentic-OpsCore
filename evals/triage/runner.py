"""
Eval harness for EmailTriageSkill.

Scores each case on two dimensions:
  - Priority match  = 1 point  (high / medium / low correct?)
  - Category match  = 1 point  (action / meeting / info / spam correct?)

A case is "passed" if the priority matches (primary triage quality signal).
Category accuracy is tracked separately as a secondary metric.

Exit codes (standalone run):
  0  if priority accuracy >= 80 %
  1  otherwise

Run standalone:
    $env:PYTHONPATH = "src"
    $env:PYTHONIOENCODING = "utf-8"
    uv run python evals/triage/runner.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

CASES_FILE = Path(__file__).parent / "cases.jsonl"


# ---------------------------------------------------------------------------
# Core runner — returns structured data, prints nothing
# ---------------------------------------------------------------------------

def run_evals() -> dict:
    """
    Run all eval cases and return a result dict:

        {
          "results": [
            {
              "id", "description", "subject",
              "expected_priority", "expected_category",
              "actual_priority",   "actual_category",
              "priority_pass",     "category_pass",
              "passed"             # True iff priority matches
            }, ...
          ],
          "totals": {
            "cases", "priority_hits", "category_hits",
            "full_passes",        # both priority AND category correct
            "total_points",       # priority_hits + category_hits
            "max_points",         # cases * 2
            "priority_accuracy",  # float 0.0–1.0
            "category_accuracy",  # float 0.0–1.0
            "tokens_in", "tokens_out", "cost_usd"
          }
        }
    """
    from agent.observability.costs import get_session_total
    from agent.skills.triage import EmailTriageSkill

    skill = EmailTriageSkill()
    cases = [
        json.loads(line)
        for line in CASES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    tokens_before = get_session_total()
    results: list[dict] = []

    for case in cases:
        raw = skill.run(case["email"])

        ap = str(raw["priority"].value if hasattr(raw["priority"], "value") else raw["priority"])
        ac = str(raw["category"].value if hasattr(raw["category"], "value") else raw["category"])
        ep = case["expected_priority"]
        ec = case["expected_category"]

        results.append({
            "id":                case["id"],
            "description":       case.get("description", ""),
            "subject":           case["email"]["subject"],
            "expected_priority": ep,
            "expected_category": ec,
            "actual_priority":   ap,
            "actual_category":   ac,
            "priority_pass":     ap == ep,
            "category_pass":     ac == ec,
            "passed":            ap == ep,           # primary metric
        })

    tokens_after = get_session_total()
    priority_hits = sum(1 for r in results if r["priority_pass"])
    category_hits = sum(1 for r in results if r["category_pass"])
    full_passes   = sum(1 for r in results if r["priority_pass"] and r["category_pass"])
    n             = len(results)

    return {
        "results": results,
        "totals": {
            "cases":              n,
            "priority_hits":      priority_hits,
            "category_hits":      category_hits,
            "full_passes":        full_passes,
            "total_points":       priority_hits + category_hits,
            "max_points":         n * 2,
            "priority_accuracy":  priority_hits / n if n else 0.0,
            "category_accuracy":  category_hits / n if n else 0.0,
            "tokens_in":          tokens_after["input_tokens"]  - tokens_before["input_tokens"],
            "tokens_out":         tokens_after["output_tokens"] - tokens_before["output_tokens"],
            "cost_usd":           round(tokens_after["total_cost_usd"] - tokens_before["total_cost_usd"], 6),
        },
    }


# ---------------------------------------------------------------------------
# Standalone display (run as a script)
# ---------------------------------------------------------------------------

def _main() -> None:
    from rich.console import Console
    from rich.rule import Rule
    from rich.table import Table

    console = Console()

    _PRIORITY_COLOR = {"high": "bold red", "medium": "yellow", "low": "green"}
    _CAT_ICON       = {"action": "⚡", "meeting": "📅", "info": "ℹ", "spam": "🗑"}

    def _pm(p: str) -> str:
        c = _PRIORITY_COLOR.get(p, "white")
        return f"[{c}]{p}[/{c}]"

    console.print()
    console.print(Rule("[bold]Triage Eval Suite[/bold]"))
    console.print()

    with console.status("[bold green]Running 10 eval cases…", spinner="dots"):
        data = run_evals()

    results = data["results"]
    totals  = data["totals"]

    # Per-case table
    table = Table(
        title=f"Eval Results  [dim]({totals['cases']} cases)[/dim]",
        show_lines=True,
        header_style="bold cyan",
        border_style="dim",
    )
    table.add_column("ID",           width=13)
    table.add_column("Subject",      max_width=32, no_wrap=True)
    table.add_column("Pri exp→got",  justify="center", width=18)
    table.add_column("Cat exp→got",  justify="center", width=22)
    table.add_column("Status",       justify="center", width=8)

    for r in results:
        p_arrow = f"{_pm(r['expected_priority'])} → {_pm(r['actual_priority'])}"
        c_icon  = _CAT_ICON.get(r["expected_category"], "")
        c_arrow = (
            f"{c_icon}{r['expected_category']} → "
            f"{'[green]' if r['category_pass'] else '[red]'}"
            f"{_CAT_ICON.get(r['actual_category'], '')}{r['actual_category']}"
            f"{'[/green]' if r['category_pass'] else '[/red]'}"
        )
        status = "[bold green]PASS[/bold green]" if r["passed"] else "[bold red]FAIL[/bold red]"
        table.add_row(r["id"], r["subject"][:30], p_arrow, c_arrow, status)

    console.print(table)
    console.print()

    # Summary
    ph  = totals["priority_hits"]
    ch  = totals["category_hits"]
    fp  = totals["full_passes"]
    n   = totals["cases"]
    pct = round(ph / n * 100) if n else 0
    color = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"

    console.print(
        f"  Priority accuracy : [{color}]{ph}/{n} ({pct}%)[/{color}]"
    )
    console.print(
        f"  Category accuracy : {ch}/{n} ({round(ch/n*100) if n else 0}%)"
    )
    console.print(
        f"  Full passes       : {fp}/{n} (both correct)"
    )
    console.print(
        f"  Score             : {totals['total_points']}/{totals['max_points']} pts"
    )
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
