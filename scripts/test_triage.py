"""
Live demo: run EmailTriageSkill on 5 emails and display a Rich table.

Makes REAL API calls — requires ANTHROPIC_API_KEY in .env.

Run:
    $env:PYTHONPATH = "src"
    $env:PYTHONIOENCODING = "utf-8"
    uv run python scripts/test_triage.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from rich.console import Console
from rich.rule import Rule
from rich.table import Table

from agent.observability.costs import get_session_total
from agent.skills.triage import EmailTriageSkill

console = Console()

_PRIORITY_COLOR = {"high": "bold red", "medium": "yellow", "low": "dim"}
_CATEGORY_ICON  = {"action": "⚡", "meeting": "📅", "info": "ℹ", "spam": "🗑"}


def main() -> None:
    email_file = Path(__file__).parent.parent / "data" / "mock_emails.json"
    emails = json.loads(email_file.read_text(encoding="utf-8"))[:5]

    console.print()
    console.print(Rule("[bold]Email Triage — live run[/bold]"))
    console.print(f"  Emails: {len(emails)}   Model: claude-haiku-4-5")
    console.print()

    skill = EmailTriageSkill()
    rows: list[dict] = []

    for email in emails:
        console.print(f"  [dim]→ triaging[/dim] {email['sender'][:45]} …", end="")
        result = skill.run(email)
        rows.append({**email, **result})
        console.print(f"  [{result['priority']}]")

    console.print()

    table = Table(
        title="Triage Results",
        show_lines=True,
        header_style="bold cyan",
        border_style="dim",
    )
    table.add_column("From",     max_width=30, no_wrap=True)
    table.add_column("Subject",  max_width=36, no_wrap=True)
    table.add_column("Priority", justify="center", width=10)
    table.add_column("Category", justify="center", width=10)
    table.add_column("Action",   width=14)

    for row in rows:
        p     = row["priority"]
        cat   = row["category"]
        color = _PRIORITY_COLOR.get(p, "white")
        icon  = _CATEGORY_ICON.get(cat, "")
        table.add_row(
            row["sender"].split("@")[-1][:28],
            row["subject"][:34],
            f"[{color}]{p}[/{color}]",
            f"{icon} {cat}",
            row["suggested_action"],
        )

    console.print(table)

    # Cost summary
    totals = get_session_total()
    console.print()
    console.print(Rule("[dim]Session cost[/dim]"))
    console.print(
        f"  Tokens in: [cyan]{totals['input_tokens']:,}[/cyan]  "
        f"out: [cyan]{totals['output_tokens']:,}[/cyan]  "
        f"cost: [green]${totals['total_cost_usd']:.6f}[/green]"
    )
    console.print()


if __name__ == "__main__":
    main()
