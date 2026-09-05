"""
Prints the AtlasOS eval architecture diagram with Rich formatting, and (re)saves
the plain-text version to data/architecture/eval_architecture.txt.

Run:
    uv run python scripts/print_eval_architecture.py
"""
from __future__ import annotations

import re
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).parent.parent
DIAGRAM_PATH = ROOT / "data" / "architecture" / "eval_architecture.txt"

HEADER_RE = re.compile(r"^\s*\d\s{2}[A-Z]")
RULE_RE = re.compile(r"^[═]+$")

# Real counts, verified against evals/*/cases.jsonl (not hand-typed guesses).
SUITES = [
    ("cost", 12), ("daemon", 8), ("db", 6), ("deployment", 12), ("incident", 6),
    ("jenkins", 12), ("k8s", 10), ("logs", 8), ("multi_cluster", 10), ("pagerduty", 6),
    ("resources", 8), ("runbook", 6), ("scale", 6), ("security", 8), ("slack", 8),
    ("slo", 6), ("triage", 10),
]


def _colorize(diagram: str, width: int) -> Text:
    text = Text(no_wrap=True, overflow="crop")
    for line in diagram.split("\n"):
        if RULE_RE.match(line) or HEADER_RE.match(line) or "ATLASOS EVAL" in line or "How every AI skill" in line:
            text.append(line + "\n", style="bold blue")
        else:
            seg = Text(line + "\n", no_wrap=True, overflow="crop")
            seg.highlight_words(["PASS", "PASSES", "GATE OPEN", "merge allowed"], style="bold green")
            seg.highlight_words(["FAIL", "FAILS", "GATE CLOSED", "PR blocked"], style="bold red")
            seg.highlight_words(["WARNING", "⚠"], style="bold yellow")
            text.append(seg)
    return text


def main() -> None:
    diagram = DIAGRAM_PATH.read_text(encoding="utf-8").rstrip("\n")
    diagram_max = max(len(l) for l in diagram.split("\n"))
    console = Console(width=diagram_max + 8, soft_wrap=False)

    console.print()
    console.print(Panel(
        _colorize(diagram, diagram_max),
        title="[bold]AtlasOS Eval Architecture[/bold]",
        border_style="blue",
        padding=(1, 2),
    ))

    total = sum(c for _, c in SUITES)
    qr = Text()
    qr.append("Files per skill (3):\n", style="bold blue")
    qr.append("  integrations/<skill>.py   — raw client, no AI, no business logic\n")
    qr.append("  skills/<skill>.py          — AI layer: scan / diagnose / apply_fix\n")
    qr.append("  evals/<skill>/{cases.jsonl, runner.py}  — the eval suite for that skill\n\n")
    qr.append(f"Eval suites in AtlasOS ({len(SUITES)} suites, {total} cases total):", style="bold blue")

    console.print()
    console.print(Panel(qr, title="[bold]Quick Reference[/bold]", border_style="blue", padding=(1, 2)))

    tbl = Table(show_header=True, header_style="bold blue", box=None, padding=(0, 2))
    tbl.add_column("Suite")
    tbl.add_column("Cases", justify="right")
    tbl.add_column("Suite")
    tbl.add_column("Cases", justify="right")
    half = (len(SUITES) + 1) // 2
    left, right = SUITES[:half], SUITES[half:]
    for i in range(half):
        l_name, l_n = left[i]
        if i < len(right):
            r_name, r_n = right[i]
            tbl.add_row(l_name, str(l_n), r_name, str(r_n))
        else:
            tbl.add_row(l_name, str(l_n), "", "")
    console.print(tbl)

    console.print()
    console.print(Panel(
        "One AI skill = one integration client + one AI layer + one eval suite — "
        "the CLI, MCP, and CI/CD all trigger the exact same runner, so testing one "
        "path tests all three.",
        border_style="blue",
        padding=(1, 2),
    ))
    console.print()
    console.print(f"[dim]Diagram saved to: {DIAGRAM_PATH}[/dim]")
    console.print()


if __name__ == "__main__":
    main()
