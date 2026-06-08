"""
AI Agentic OS — command-line interface.

Entry point: agent <command> [options]

Commands:
  triage              Triage emails from a JSON file
  memory search TEXT  Semantic search over the memory store
  memory list         Show recent memories
  eval triage         Run the email triage eval suite
"""
from __future__ import annotations

# Must be set before numpy/OpenBLAS loads — prevents OOM crash on Windows
# where the paging file is too small for OpenBLAS multi-threaded allocations.
import os
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_MAIN_FREE", "1")
os.environ.setdefault("GOTO_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Ensure the Windows console can render Unicode (e.g. arrows in Claude output).
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

# Make the project root importable so `evals/` can be imported as a package.
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import typer
from rich.console import Console
from rich.markup import escape as _escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

# ---------------------------------------------------------------------------
# App + sub-apps
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="agent",
    help="AI Agentic OS — business command center.",
    add_completion=False,
    no_args_is_help=True,
)

memory_app = typer.Typer(
    help="Inspect and search the persistent memory store.",
    no_args_is_help=True,
)
app.add_typer(memory_app, name="memory")

eval_app = typer.Typer(
    help="Run evaluation suites against live models.",
    no_args_is_help=True,
)
app.add_typer(eval_app, name="eval")

gmail_app = typer.Typer(
    help="Gmail integration — fetch and triage real emails.",
    no_args_is_help=True,
)
app.add_typer(gmail_app, name="gmail")

k8s_app = typer.Typer(
    help="Kubernetes cluster diagnosis — scan, diagnose, and fix unhealthy pods.",
    no_args_is_help=True,
)
app.add_typer(k8s_app, name="k8s")

aws_app = typer.Typer(
    help="AWS account diagnosis — scan EC2, RDS, and ALB for problems and fix them.",
    no_args_is_help=True,
)
app.add_typer(aws_app, name="aws")

ingress_app = typer.Typer(
    help="Nginx ingress diagnosis — external IP, routing, TLS, and backend problems.",
    no_args_is_help=True,
)
app.add_typer(ingress_app, name="ingress")

tls_app = typer.Typer(
    help="TLS/certificate diagnosis — expiry, cert-manager, ACME challenges, secrets.",
    no_args_is_help=True,
)
app.add_typer(tls_app, name="tls")

dns_app = typer.Typer(
    help="DNS diagnosis — CoreDNS health, resolution tests, ndots, external-dns.",
    no_args_is_help=True,
)
app.add_typer(dns_app, name="dns")

domain_app = typer.Typer(
    help="Domain HTTPS — provision Let's Encrypt certs, patch ingress, watch issuance.",
    no_args_is_help=True,
)
app.add_typer(domain_app, name="domain")

network_app = typer.Typer(
    help="Network diagnosis — CNI, kube-proxy, NetworkPolicy, pod connectivity.",
    no_args_is_help=True,
)
app.add_typer(network_app, name="network")

console = Console()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRIORITY_COLOR = {"high": "bold red", "medium": "yellow", "low": "green"}
_CATEGORY_ICON  = {"action": "⚡", "meeting": "📅", "info": "ℹ", "spam": "🗑"}

_EMAIL_FILE = Path("data/mock_emails.json")


def _priority_markup(p: str) -> str:
    color = _PRIORITY_COLOR.get(p, "white")
    return f"[{color}]{p}[/{color}]"


def _print_error(msg: str) -> None:
    console.print(Panel(f"[bold red]Error:[/bold red] {msg}", border_style="red"))


def _cost_footer(totals: dict) -> None:
    console.print(Rule(style="dim"))
    console.print(
        f"  Tokens — in: [cyan]{totals['input_tokens']:,}[/cyan]  "
        f"out: [cyan]{totals['output_tokens']:,}[/cyan]   "
        f"Cost: [green]${totals['total_cost_usd']:.6f}[/green]"
        f"  ([dim]{totals['calls']} call(s)[/dim])",
        highlight=False,
    )
    console.print()


# ---------------------------------------------------------------------------
# --version callback
# ---------------------------------------------------------------------------

def _version_callback(value: bool) -> None:
    if value:
        console.print("Agentic OS [bold]v0.1.0[/bold]")
        raise typer.Exit()


@app.callback()
def _root_callback(
    version: bool = typer.Option(
        False,
        "--version", "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    pass


# ---------------------------------------------------------------------------
# Command: agent usage
# ---------------------------------------------------------------------------

@app.command()
def usage() -> None:
    """Show Claude API token usage and cost for this session."""
    from agent.observability.costs import get_session_total

    totals = get_session_total()

    console.print()
    console.print(Rule("[bold]Claude API Usage — This Session[/bold]"))
    console.print()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Label", style="dim", width=20)
    table.add_column("Value", justify="right")

    table.add_row("API calls",     f"[cyan]{totals['calls']}[/cyan]")
    table.add_row("Tokens in",     f"[cyan]{totals['input_tokens']:,}[/cyan]")
    table.add_row("Tokens out",    f"[cyan]{totals['output_tokens']:,}[/cyan]")
    table.add_row("Total tokens",  f"[cyan]{totals['input_tokens'] + totals['output_tokens']:,}[/cyan]")
    table.add_row("Cost (USD)",    f"[bold green]${totals['total_cost_usd']:.6f}[/bold green]")

    console.print(table)
    console.print()

    if totals["calls"] == 0:
        console.print("  [dim]No API calls made yet in this session.[/dim]")
        console.print()

    console.print(Rule(style="dim"))
    console.print(
        "  [dim]Session resets each time you start a new[/dim] agent [dim]process.[/dim]",
        highlight=False,
    )
    console.print(
        "  [dim]Full account usage:[/dim] https://console.anthropic.com/settings/usage",
        highlight=False,
    )
    console.print()


# ---------------------------------------------------------------------------
# Command: agent triage
# ---------------------------------------------------------------------------

@app.command()
def triage(
    email_file: Path = typer.Option(
        _EMAIL_FILE,
        "--file", "-f",
        help="JSON file containing the list of emails to triage.",
        show_default=True,
    ),
    limit: int = typer.Option(
        20,
        "--limit", "-n",
        help="Maximum number of emails to process.",
        show_default=True,
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose", "-v",
        help="Show full details (subject, draft reply) per email.",
    ),
) -> None:
    """Triage emails and show priority, category, and suggested action."""
    try:
        if not email_file.exists():
            _print_error(f"Email file not found: {email_file}")
            raise typer.Exit(1)

        emails = json.loads(email_file.read_text(encoding="utf-8"))
        emails = emails[:limit]
        total = len(emails)

        if total == 0:
            console.print("[dim]No emails to process.[/dim]")
            return

        from agent.observability.costs import get_session_total
        from agent.skills.triage import EmailTriageSkill

        skill = EmailTriageSkill()
        rows: list[tuple[dict, dict]] = []

        with console.status("", spinner="dots") as status:
            for i, email in enumerate(emails, 1):
                status.update(
                    f"[bold green]Triaging {i}/{total}:[/bold green] "
                    f"[dim]{email.get('sender', '')[:40]}[/dim]"
                )
                result = skill.run(email)
                rows.append((email, result))

        # Build table
        table = Table(
            title=f"Email Triage  [dim]({total} emails)[/dim]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        table.add_column("From",     max_width=28, no_wrap=True)
        table.add_column("Priority", justify="center", width=10)
        table.add_column("Category", width=10)
        table.add_column("Action",   width=10)

        if verbose:
            table.add_column("Subject", max_width=34, no_wrap=True)
            table.add_column("Draft reply", max_width=40)

        counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0}

        for email, result in rows:
            p   = str(result["priority"].value if hasattr(result["priority"], "value") else result["priority"])
            cat = str(result["category"].value if hasattr(result["category"], "value") else result["category"])
            counts[p] = counts.get(p, 0) + 1
            icon = _CATEGORY_ICON.get(cat, "")

            row_cells = [
                email.get("sender", "")[:26],
                _priority_markup(p),
                f"{icon} {cat}",
                result.get("suggested_action", ""),
            ]
            if verbose:
                row_cells.append(email.get("subject", "")[:32])
                draft = result.get("draft_reply") or "[dim]—[/dim]"
                row_cells.append(draft[:38] if draft != "[dim]—[/dim]" else draft)

            table.add_row(*row_cells)

        console.print()
        console.print(table)
        console.print()

        # Summary
        console.print(
            f"  Summary: "
            f"[bold red]{counts.get('high', 0)} high[/bold red]  "
            f"[yellow]{counts.get('medium', 0)} medium[/yellow]  "
            f"[green]{counts.get('low', 0)} low[/green]"
        )

        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent memory search TEXT
# ---------------------------------------------------------------------------

@memory_app.command("search")
def memory_search(
    query: str = typer.Argument(..., help="Search query text."),
    limit: int = typer.Option(10, "--limit", "-n", help="Max results.", show_default=True),
) -> None:
    """Semantic search over stored memories."""
    try:
        from agent.memory.embeddings import search_similar
        from agent.memory.store import get_recent

        hits = search_similar(query, limit=limit)

        if not hits:
            console.print("[dim]No matching memories found.[/dim]")
            return

        # Cross-reference with SQLite to get created_at.
        all_recent = get_recent(limit=500)
        id_to_memory = {m.id: m for m in all_recent}

        table = Table(
            title=f'Memory Search: "{query}"',
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        table.add_column("Score",      justify="center", width=7)
        table.add_column("Content",    max_width=52)
        table.add_column("Source",     width=12)
        table.add_column("Created",    width=17)

        for hit in hits:
            mem = id_to_memory.get(hit["id"])
            score = round(1.0 - hit["distance"], 3)
            score_color = "green" if score >= 0.7 else "yellow" if score >= 0.4 else "dim"
            table.add_row(
                f"[{score_color}]{score:.3f}[/{score_color}]",
                hit["content"][:50],
                mem.source if mem else hit["metadata"].get("source", "?"),
                mem.created_at.strftime("%Y-%m-%d %H:%M") if mem else "—",
            )

        console.print()
        console.print(table)
        console.print(f"\n  [dim]{len(hits)} result(s) for '{query}'[/dim]\n")

    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent memory list
# ---------------------------------------------------------------------------

@memory_app.command("list")
def memory_list(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of memories to show.", show_default=True),
) -> None:
    """Show the most recently stored memories."""
    try:
        from agent.memory.store import get_recent

        memories = get_recent(limit=limit)

        if not memories:
            console.print("[dim]Memory store is empty.[/dim]")
            return

        table = Table(
            title=f"Recent Memories  [dim](latest {len(memories)})[/dim]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        table.add_column("#",         width=4,  justify="right", style="dim")
        table.add_column("Content",   max_width=54)
        table.add_column("Source",    width=12)
        table.add_column("Created",   width=17)

        for i, mem in enumerate(memories, 1):
            table.add_row(
                str(i),
                mem.content[:52],
                mem.source,
                mem.created_at.strftime("%Y-%m-%d %H:%M"),
            )

        console.print()
        console.print(table)
        console.print()

    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent eval triage
# ---------------------------------------------------------------------------

@eval_app.command("triage")
def eval_triage() -> None:
    """Run the email triage eval suite against the live model."""
    try:
        from evals.triage.runner import run_evals  # type: ignore[import]

        console.print()
        console.print(Rule("[bold]Triage Eval Suite[/bold]"))
        console.print()

        with console.status("[bold green]Running eval cases…", spinner="dots"):
            data = run_evals()

        results = data["results"]
        totals  = data["totals"]

        _CAT_ICON = {"action": "⚡", "meeting": "📅", "info": "ℹ", "spam": "🗑"}

        table = Table(
            title=f"Eval Results  [dim]({totals['cases']} cases)[/dim]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        table.add_column("ID",          width=13)
        table.add_column("Subject",     max_width=32, no_wrap=True)
        table.add_column("Priority",    justify="center", width=18)
        table.add_column("Category",    justify="center", width=22)
        table.add_column("Status",      justify="center", width=8)

        for r in results:
            p_arrow = (
                f"{_priority_markup(r['expected_priority'])} → "
                f"{_priority_markup(r['actual_priority'])}"
            )
            ci = _CAT_ICON.get(r["expected_category"], "")
            c_got_color = "green" if r["category_pass"] else "red"
            c_arrow = (
                f"{ci}{r['expected_category']} → "
                f"[{c_got_color}]{_CAT_ICON.get(r['actual_category'], '')}"
                f"{r['actual_category']}[/{c_got_color}]"
            )
            status = "[bold green]PASS[/bold green]" if r["passed"] else "[bold red]FAIL[/bold red]"
            table.add_row(r["id"], r["subject"][:30], p_arrow, c_arrow, status)

        console.print(table)
        console.print()

        ph    = totals["priority_hits"]
        ch    = totals["category_hits"]
        fp    = totals["full_passes"]
        n     = totals["cases"]
        pct   = round(ph / n * 100) if n else 0
        color = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"

        console.print(f"  Priority accuracy : [{color}]{ph}/{n} ({pct}%)[/{color}]")
        console.print(f"  Category accuracy : {ch}/{n} ({round(ch/n*100) if n else 0}%)")
        console.print(f"  Full passes       : {fp}/{n} (both correct)")
        console.print(f"  Score             : {totals['total_points']}/{totals['max_points']} pts")
        console.print()

        _cost_footer({
            "calls":         0,
            "input_tokens":  totals["tokens_in"],
            "output_tokens": totals["tokens_out"],
            "total_cost_usd": totals["cost_usd"],
        })

        if pct < 80:
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent eval k8s
# ---------------------------------------------------------------------------

@eval_app.command("k8s")
def eval_k8s() -> None:
    """Run the Kubernetes diagnosis eval suite against the live model."""
    try:
        from evals.k8s.runner import run_evals  # type: ignore[import]

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
        table.add_column("Pod",      max_width=26, no_wrap=True)
        table.add_column("Expected", width=22)
        table.add_column("Got",      width=22)
        table.add_column("Conf",     justify="center", width=8)
        table.add_column("Type",     justify="center", width=6)
        table.add_column("Conf",     justify="center", width=6)
        table.add_column("Fix",      justify="center", width=6)
        table.add_column("Pass",     justify="center", width=6)

        for r in results:
            exp = r["expected_problem_type"] or "[dim]healthy[/dim]"
            got = _escape(r["actual_problem_type"])
            type_ok = "[green]OK[/green]"   if r["problem_type_pass"] else "[red]FAIL[/red]"
            conf_ok = "[green]OK[/green]"   if r["confidence_pass"]   else "[red]FAIL[/red]"
            fix_ok  = "[green]OK[/green]"   if r["fix_pass"]          else "[red]FAIL[/red]"
            status  = (
                "[bold green]PASS[/bold green]" if r["passed"]
                else "[bold red]FAIL[/bold red]"
            )
            conf_val = r["confidence"]
            conf_color = (
                "green"  if conf_val == "high"   else
                "yellow" if conf_val == "medium"  else
                "dim"
            )
            table.add_row(
                r["id"],
                r["pod"][:24],
                exp,
                got,
                f"[{conf_color}]{conf_val}[/{conf_color}]",
                type_ok, conf_ok, fix_ok, status,
            )

        console.print(table)
        console.print()

        n      = totals["cases"]
        passed = totals["passed"]
        pct    = round(passed / n * 100) if n else 0
        color  = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"

        console.print(f"  Pass rate    : [{color}]{passed}/{n} ({pct}%)[/{color}]")
        console.print(f"  Problem type : {totals['problem_type_hits']}/{n}")
        console.print(f"  Confidence   : {totals['confidence_hits']}/{n}")
        console.print(f"  Fix present  : {totals['fix_hits']}/{n}")
        console.print()

        _cost_footer({
            "calls":          0,
            "input_tokens":   totals["tokens_in"],
            "output_tokens":  totals["tokens_out"],
            "total_cost_usd": totals["cost_usd"],
        })

        if pct < 80:
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent gmail jobs
# ---------------------------------------------------------------------------

@gmail_app.command("jobs")
def gmail_jobs(
    fetch_limit: int = typer.Option(
        30,
        "--fetch", "-f",
        help="How many inbox emails to pull before filtering.",
        show_default=True,
    ),
    result_limit: int = typer.Option(
        5,
        "--limit", "-n",
        help="Max job-related emails to show.",
        show_default=True,
    ),
) -> None:
    """Scan inbox for job/interview emails and show a structured summary."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.job_triage import JobTriageSkill

        console.print()
        with console.status("[bold cyan]Scanning inbox for job emails…", spinner="dots"):
            result = JobTriageSkill().run({
                "fetch_limit":  fetch_limit,
                "result_limit": result_limit,
            })

        relevant = result.get("relevant", [])
        scanned  = result.get("total_scanned", 0)
        found    = result.get("found", 0)

        console.print()

        if not relevant:
            console.print(Panel(
                "[dim]No new job or interview related emails found in the latest inbox scan.[/dim]",
                title="Job Inbox Summary",
                border_style="dim",
            ))
            console.print()
            _cost_footer(get_session_total())
            return

        _PRIORITY_STYLE = {
            "Critical": "bold red",
            "High":     "bold yellow",
            "Medium":   "cyan",
        }
        _CATEGORY_ICON = {
            "Interview":          "🎯",
            "Assessment":         "💻",
            "Recruiter Outreach": "🤝",
            "Application Update": "📋",
            "Offer":              "🎉",
            "Rejection":          "❌",
            "Other":              "📧",
        }

        console.print(Rule(f"[bold cyan]Job Inbox Summary[/bold cyan]  [dim]{found} found / {scanned} scanned[/dim]"))
        console.print()

        for i, email in enumerate(relevant, 1):
            priority = email.get("priority", "Medium")
            category = email.get("category", "Other")
            company  = email.get("company") or "—"
            action   = email.get("action_required")
            style    = _PRIORITY_STYLE.get(priority, "white")
            icon     = _CATEGORY_ICON.get(category, "📧")

            # Format timestamp nicely
            ts_raw = email.get("received_time", "")
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "")).strftime("%d %b %Y, %H:%M")
            except Exception:
                ts = str(ts_raw)[:16]

            header = (
                f"[bold]Email {i}[/bold]  "
                f"[{style}]{priority}[/{style}]  "
                f"{icon} {category}"
            )
            body_lines = [
                f"[dim]Time   :[/dim]  {ts}",
                f"[dim]Company:[/dim]  {company}",
                f"[dim]From   :[/dim]  {email.get('sender', '')}",
                f"[dim]Subject:[/dim]  {email.get('subject', '')}",
                f"[dim]Summary:[/dim]  {email.get('summary', '')}",
            ]
            if action:
                body_lines.append(f"\n[bold yellow]⚠ Action Required:[/bold yellow]  {action}")

            console.print(Panel(
                "\n".join(body_lines),
                title=header,
                border_style=style,
                padding=(0, 1),
            ))
            console.print()

        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent gmail auth
# ---------------------------------------------------------------------------

@gmail_app.command("auth")
def gmail_auth() -> None:
    """
    Authenticate with Gmail via OAuth 2.0 (run once).

    Opens a browser window — sign in with your Google account and grant
    access.  The token is saved to data/gmail_token.json for future runs.

    Prerequisites:
      1. Go to console.cloud.google.com
      2. Create a project → Enable the Gmail API
      3. Credentials → Create OAuth 2.0 Client ID (Desktop app)
      4. Download JSON → save as  data/gmail_credentials.json
    """
    try:
        from agent.skills.gmail import TOKEN_FILE, get_gmail_service

        console.print()
        console.print(Panel(
            "[bold cyan]Gmail OAuth Setup[/bold cyan]\n\n"
            "A browser will open asking you to sign in to Google.\n"
            "Grant the requested permissions to continue.\n\n"
            "[dim]Token will be saved to:[/dim] " + str(TOKEN_FILE),
            border_style="cyan",
        ))
        console.print()

        get_gmail_service()   # triggers the flow if no token exists
        console.print(f"[bold green]✓ Authentication successful![/bold green]  "
                      f"Token saved to [cyan]{TOKEN_FILE}[/cyan]")
        console.print()

    except FileNotFoundError as e:
        _print_error(str(e))
        raise typer.Exit(1)
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent gmail triage
# ---------------------------------------------------------------------------

@gmail_app.command("triage")
def gmail_triage(
    limit: int = typer.Option(
        10,
        "--limit", "-n",
        help="Max number of emails to fetch and triage.",
        show_default=True,
    ),
    mark_read: bool = typer.Option(
        False,
        "--mark-read",
        help="Mark fetched emails as read in Gmail.",
    ),
    query: str = typer.Option(
        "is:unread in:inbox",
        "--query", "-q",
        help="Gmail search query.",
        show_default=True,
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose", "-v",
        help="Show subject and draft reply per email.",
    ),
) -> None:
    """Fetch real emails from Gmail and run AI triage on each one."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.gmail import GmailSkill
        from agent.skills.triage import EmailTriageSkill

        # Step 1 — fetch emails from Gmail
        console.print()
        with console.status("[bold cyan]Fetching emails from Gmail…", spinner="dots"):
            gmail_result = GmailSkill().run({"limit": limit, "query": query, "mark_read": mark_read})

        emails = gmail_result["emails"]
        total  = gmail_result["count"]

        if total == 0:
            console.print(f"[dim]No emails matched query:[/dim] {query}")
            return

        console.print(f"  Fetched [bold cyan]{total}[/bold cyan] email(s) from Gmail.")
        console.print()

        # Step 2 — triage each email
        skill = EmailTriageSkill()
        rows: list[tuple[dict, dict]] = []

        with console.status("", spinner="dots") as status:
            for i, email in enumerate(emails, 1):
                status.update(
                    f"[bold green]Triaging {i}/{total}:[/bold green] "
                    f"[dim]{email.get('sender', '')[:40]}[/dim]"
                )
                result = skill.run(email)
                rows.append((email, result))

        # Step 3 — render table
        table = Table(
            title=f"Gmail Triage  [dim]({total} emails)[/dim]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        table.add_column("From",     max_width=28, no_wrap=True)
        table.add_column("Priority", justify="center", width=10)
        table.add_column("Category", width=10)
        table.add_column("Action",   width=10)

        if verbose:
            table.add_column("Subject",    max_width=34, no_wrap=True)
            table.add_column("Draft reply", max_width=40)

        counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0}

        for email, result in rows:
            p   = str(result["priority"].value if hasattr(result["priority"], "value") else result["priority"])
            cat = str(result["category"].value if hasattr(result["category"], "value") else result["category"])
            counts[p] = counts.get(p, 0) + 1
            icon = _CATEGORY_ICON.get(cat, "")

            row_cells = [
                email.get("sender", "")[:26],
                _priority_markup(p),
                f"{icon} {cat}",
                result.get("suggested_action", ""),
            ]
            if verbose:
                row_cells.append(email.get("subject", "")[:32])
                draft = result.get("draft_reply") or "[dim]—[/dim]"
                row_cells.append(draft[:38] if draft != "[dim]—[/dim]" else draft)

            table.add_row(*row_cells)

        console.print(table)
        console.print()
        console.print(
            f"  Summary: "
            f"[bold red]{counts.get('high', 0)} high[/bold red]  "
            f"[yellow]{counts.get('medium', 0)} medium[/yellow]  "
            f"[green]{counts.get('low', 0)} low[/green]"
        )

        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Helpers: k8s
# ---------------------------------------------------------------------------

_CONFIDENCE_COLOR = {"high": "green", "medium": "yellow", "low": "red"}


def _confidence_markup(c: str) -> str:
    color = _CONFIDENCE_COLOR.get(c.lower(), "white")
    return f"[{color}]{c.upper()}[/{color}]"


def _print_diagnosis(diagnosis) -> None:
    """Render a structured diagnosis panel to the console."""
    conf_color = _CONFIDENCE_COLOR.get(diagnosis.confidence.lower(), "white")
    divider = "[dim]" + "-" * 58 + "[/dim]"

    lines = [
        f"[dim]Problem:   [/dim] [bold cyan]{_escape(diagnosis.problem_type.value)}[/bold cyan]",
        f"[dim]Root Cause:[/dim] {_escape(diagnosis.root_cause)}",
        f"[dim]Confidence:[/dim] [bold {conf_color}]{_escape(diagnosis.confidence.upper())}[/bold {conf_color}]",
        divider,
        "[bold]EXPLANATION[/bold]",
        _escape(diagnosis.explanation),
        divider,
        "[bold]SUGGESTED FIX[/bold]",
        _escape(diagnosis.suggested_fix),
    ]

    if diagnosis.fix_command:
        lines += [
            divider,
            "[bold]COMMAND[/bold]",
            f"[bold cyan]{_escape(diagnosis.fix_command)}[/bold cyan]",
        ]

    console.print(Panel(
        "\n".join(lines),
        title=f"[bold]DIAGNOSIS:[/bold] [cyan]{_escape(diagnosis.pod)}[/cyan]",
        border_style="cyan",
        padding=(1, 2),
    ))


# ---------------------------------------------------------------------------
# Command: agent k8s scan
# ---------------------------------------------------------------------------

@k8s_app.command("scan")
def k8s_scan(
    namespace: str = typer.Option(
        "all",
        "--namespace", "-n",
        help="Namespace to scan, or 'all' for every namespace.",
        show_default=True,
    ),
) -> None:
    """Scan the cluster for unhealthy pods and show a summary table."""
    try:
        from agent.skills.k8s import K8sSkill

        skill = K8sSkill()
        console.print()

        with console.status("[bold cyan]Scanning cluster...", spinner="dots"):
            diagnoses = skill.scan_cluster(namespace)

        console.print()

        if not diagnoses:
            console.print(Panel(
                "[bold green]All pods healthy![/bold green]  No problems detected.",
                border_style="green",
            ))
            console.print()
            return

        table = Table(
            title=f"[bold red]{len(diagnoses)} problem(s) found[/bold red]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        table.add_column("Pod",        max_width=36, no_wrap=True)
        table.add_column("Problem",    width=22)
        table.add_column("Namespace",  width=16)
        table.add_column("Restarts",   justify="right", width=9)
        table.add_column("Confidence", justify="center", width=11)

        for d in diagnoses:
            table.add_row(
                d.pod,
                f"[bold red]{d.problem_type.value}[/bold red]",
                d.namespace,
                "",            # restarts not stored in PodDiagnosis
                _confidence_markup(d.confidence),
            )

        console.print(table)
        console.print()

        # Hint for next step
        first = diagnoses[0]
        console.print(
            f"  [dim]Run:[/dim] [bold]agent k8s diagnose {first.pod} -n {first.namespace}[/bold]"
        )
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent k8s diagnose POD_NAME
# ---------------------------------------------------------------------------

@k8s_app.command("diagnose")
def k8s_diagnose(
    pod_name: str = typer.Argument(..., help="Name of the pod to diagnose."),
    namespace: str = typer.Option(
        ...,
        "--namespace", "-n",
        help="Namespace the pod lives in.",
    ),
    auto_fix: bool = typer.Option(
        False,
        "--auto-fix",
        help="Apply the suggested fix automatically without prompting.",
    ),
) -> None:
    """Diagnose a specific pod using Claude and optionally apply the fix."""
    try:
        from agent.core.models import PodInfo
        from agent.integrations.kubectl import get_pods
        from agent.skills.k8s import K8sSkill

        skill = K8sSkill()
        console.print()

        # Resolve PodInfo for this pod
        with console.status("[bold cyan]Gathering pod information...", spinner="dots"):
            all_pods = get_pods(namespace)
            pod_info = next((p for p in all_pods if p.name == pod_name), None)

        if pod_info is None:
            # Build a minimal PodInfo so diagnose still works with live kubectl
            pod_info = PodInfo(
                name=pod_name,
                namespace=namespace,
                status="Unknown",
                ready="0/1",
                restarts=0,
                age="?",
                node="<none>",
            )

        with console.status("[bold cyan]Analyzing with Claude...", spinner="dots"):
            diagnosis = skill.diagnose_pod(pod_info)

        console.print()
        _print_diagnosis(diagnosis)
        console.print()

        if not diagnosis.fix_command:
            console.print("[dim]No automated fix command available for this issue.[/dim]")
            console.print()
            return

        # Determine whether to apply fix
        apply = auto_fix
        if not auto_fix:
            apply = typer.confirm("Apply this fix?", default=False)

        if not apply:
            console.print("[dim]Fix skipped.[/dim]")
            console.print()
            return

        # Apply the fix
        with console.status("[bold yellow]Applying fix...", spinner="dots"):
            success = skill.apply_fix(diagnosis)

        if success:
            console.print("[bold green]Fix applied.[/bold green]  Monitoring pod...")
        else:
            console.print("[bold red]Fix command failed.[/bold red]  Check the output above.")
            console.print()
            return

        # Wait and re-check pod status
        for remaining in range(10, 0, -1):
            console.print(f"  [dim]Waiting {remaining}s for pod to stabilise...[/dim]", end="\r")
            time.sleep(1)

        console.print(" " * 50, end="\r")  # clear the countdown line

        with console.status("[bold cyan]Checking pod status...", spinner="dots"):
            refreshed = get_pods(namespace)
            updated = next((p for p in refreshed if p.name == pod_name), None)

        if updated:
            if updated.status == "Running":
                console.print(f"[bold green]Pod status: {updated.status}[/bold green]")
            else:
                console.print(f"[bold red]Still failing — status: {updated.status}[/bold red]")
        else:
            console.print("[dim]Pod not found after fix (may have been deleted/replaced).[/dim]")

        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent k8s history
# ---------------------------------------------------------------------------

@k8s_app.command("history")
def k8s_history(
    limit: int = typer.Option(
        10,
        "--limit", "-n",
        help="Number of past diagnoses to show.",
        show_default=True,
    ),
) -> None:
    """Show past Kubernetes diagnoses stored in memory."""
    try:
        from agent.memory.store import get_by_source

        diagnoses = get_by_source("k8s-diagnose")[:limit]
        fixes     = {
            m.metadata.get("pod"): m
            for m in get_by_source("k8s-fix")
        }

        console.print()

        if not diagnoses:
            console.print("[dim]No past Kubernetes diagnoses found in memory.[/dim]")
            console.print()
            return

        table = Table(
            title=f"K8s Diagnosis History  [dim](latest {len(diagnoses)})[/dim]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        table.add_column("Pod",         max_width=30, no_wrap=True)
        table.add_column("Problem",     width=22)
        table.add_column("Confidence",  justify="center", width=11)
        table.add_column("Fix Applied", justify="center", width=11)
        table.add_column("Timestamp",   width=17)

        for mem in diagnoses:
            meta      = mem.metadata
            pod_name  = meta.get("pod", "?")
            problem   = meta.get("problem_type", "?")
            conf      = meta.get("confidence", "?")
            fix_mem   = fixes.get(pod_name)
            fix_ok    = fix_mem.metadata.get("success") if fix_mem else None
            fix_label = (
                "[green]yes[/green]" if fix_ok is True else
                "[red]failed[/red]"  if fix_ok is False else
                "[dim]—[/dim]"
            )
            ts = mem.created_at.strftime("%Y-%m-%d %H:%M")

            table.add_row(pod_name, problem, _confidence_markup(conf), fix_label, ts)

        console.print(table)
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent k8s heal  (scan + diagnose + interactive fix for every problem)
# ---------------------------------------------------------------------------

@k8s_app.command("heal")
def k8s_heal(
    namespace: str = typer.Option(
        "all",
        "--namespace", "-n",
        help="Namespace to scan, or 'all'.",
        show_default=True,
    ),
) -> None:
    """Scan the cluster, diagnose every problem, and interactively apply fixes."""
    try:
        from agent.integrations.kubectl import get_pods, get_problematic_pods
        from agent.skills.k8s import K8sSkill

        skill = K8sSkill()
        console.print()
        console.print(Rule("[bold cyan]K8s Cluster Heal[/bold cyan]"))
        console.print()

        # ── Step 1: fast scan (no Claude) ─────────────────────────────────
        with console.status("[bold cyan]Scanning cluster...", spinner="dots"):
            pods = get_problematic_pods()
            if namespace != "all":
                pods = [p for p in pods if p.namespace == namespace]

        if not pods:
            console.print(Panel(
                "[bold green]All pods healthy![/bold green]  Nothing to fix.",
                border_style="green",
            ))
            console.print()
            return

        console.print(f"  Found [bold red]{len(pods)}[/bold red] problem(s). Diagnosing each one...\n")

        # ── Step 2: diagnose (Claude) + fix loop ──────────────────────────
        fixed = skipped = failed = 0

        for i, pod in enumerate(pods, 1):
            console.print(Rule(
                f"[dim]{i}/{len(pods)}[/dim]  "
                f"[cyan]{pod.name}[/cyan]  "
                f"[dim]{pod.namespace}[/dim]"
            ))
            console.print()

            with console.status("[bold cyan]Analyzing with Claude...", spinner="dots"):
                diagnosis = skill.diagnose_pod(pod)

            _print_diagnosis(diagnosis)
            console.print()

            if not diagnosis.fix_command:
                console.print("[dim]No automated fix available — manual investigation needed.[/dim]\n")
                skipped += 1
                continue

            # Ask permission
            apply = typer.confirm(
                f"  Apply fix for [cyan]{diagnosis.pod}[/cyan]?",
                default=False,
            )

            if not apply:
                console.print("  [dim]Skipped.[/dim]\n")
                skipped += 1
                continue

            # Apply
            with console.status("[bold yellow]Applying fix...", spinner="dots"):
                success = skill.apply_fix(diagnosis)

            if success:
                console.print("  [bold green]Fix applied.[/bold green]  Waiting 10s for pod to settle...\n")
                for remaining in range(10, 0, -1):
                    console.print(f"  [dim]{remaining}s...[/dim]", end="\r")
                    time.sleep(1)
                console.print(" " * 20, end="\r")

                # Re-check pod
                with console.status("[bold cyan]Checking pod status...", spinner="dots"):
                    refreshed = get_pods(pod.namespace)
                    updated   = next((p for p in refreshed if p.name == pod.name), None)

                if updated:
                    if updated.status == "Running":
                        console.print(f"  [bold green]Pod status: {updated.status}[/bold green]\n")
                    else:
                        console.print(f"  [yellow]Pod status: {updated.status} (may still be starting)[/yellow]\n")
                else:
                    console.print("  [dim]Pod not found — may have been replaced.\n[/dim]")

                fixed += 1
            else:
                console.print("  [bold red]Fix command failed.[/bold red]  Check logs above.\n")
                failed += 1

        # ── Step 3: summary ───────────────────────────────────────────────
        console.print(Rule(style="dim"))
        console.print(
            f"\n  [bold]Summary:[/bold]  "
            f"[green]{fixed} fixed[/green]  "
            f"[dim]{skipped} skipped[/dim]  "
            f"[red]{failed} failed[/red]\n"
        )

        if fixed > 0:
            console.print("  Run [bold]agent k8s scan[/bold] to confirm everything is healthy.\n")

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent k8s watch
# ---------------------------------------------------------------------------

@k8s_app.command("watch")
def k8s_watch(
    namespace: str = typer.Option(
        "all",
        "--namespace", "-n",
        help="Namespace to watch, or 'all'.",
        show_default=True,
    ),
    interval: int = typer.Option(
        60,
        "--interval", "-i",
        help="Seconds between scans.",
        show_default=True,
    ),
) -> None:
    """Continuously scan the cluster and alert on new problems (Ctrl+C to stop)."""
    try:
        from agent.integrations.kubectl import get_problematic_pods

        console.print()
        console.print(Panel(
            f"[bold cyan]K8s Watch[/bold cyan]  namespace=[green]{namespace}[/green]  "
            f"interval=[green]{interval}s[/green]\n"
            "[dim]Press Ctrl+C to stop.[/dim]",
            border_style="cyan",
        ))
        console.print()

        seen_pods: set[str] = set()

        while True:
            ts = datetime.now().strftime("%H:%M:%S")

            with console.status(f"[dim]{ts}  Scanning...[/dim]", spinner="dots"):
                pods = get_problematic_pods()
                if namespace != "all":
                    pods = [p for p in pods if p.namespace == namespace]

            pod_keys = {f"{p.namespace}/{p.name}" for p in pods}
            new_pods  = pod_keys - seen_pods

            if not pods:
                console.print(f"  [dim]{ts}[/dim]  [green]All pods healthy[/green]")
            else:
                for p in pods:
                    key = f"{p.namespace}/{p.name}"
                    is_new = key in new_pods
                    label  = "[bold red] NEW[/bold red]" if is_new else ""
                    console.print(
                        f"  [dim]{ts}[/dim]  "
                        f"[red]{p.status}[/red]  "
                        f"[cyan]{p.name}[/cyan]  "
                        f"[dim]{p.namespace}[/dim]  "
                        f"restarts=[yellow]{p.restarts}[/yellow]"
                        f"{label}"
                    )
                if new_pods:
                    console.print(
                        f"\n  [bold red]ALERT:[/bold red] {len(new_pods)} new problem(s) detected. "
                        f"Run [bold]agent k8s diagnose <pod> -n <ns>[/bold] to investigate.\n"
                    )

            seen_pods = pod_keys
            time.sleep(interval)

    except KeyboardInterrupt:
        console.print("\n[dim]Watch stopped.[/dim]\n")
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent k8s overview
# ---------------------------------------------------------------------------

_STATUS_COLOR = {
    "Running":     "green",
    "Pending":     "yellow",
    "Failed":      "red",
    "Terminating": "dark_orange",
    "Unknown":     "dim",
    "Succeeded":   "dim",
}

_RESTART_COLOR = {
    "zero":   "green",
    "low":    "yellow",
    "medium": "dark_orange",
    "high":   "bold red",
}


def _restart_markup(n: int) -> str:
    if n == 0:
        color = "green"
    elif n <= 5:
        color = "yellow"
    elif n <= 20:
        color = "dark_orange"
    else:
        color = "bold red"
    return f"[{color}]{n}[/{color}]"


def _status_markup(s: str) -> str:
    color = _STATUS_COLOR.get(s, "white")
    return f"[{color}]{s}[/{color}]"


def _ns_health_label(ns) -> str:
    if ns.unhealthy_pods == 0:
        return f"[green]✓ Healthy[/green]    ({ns.healthy_pods}/{ns.total_pods} running)"
    elif ns.unhealthy_pods < ns.total_pods:
        return f"[yellow]⚠ Warning[/yellow]    ({ns.healthy_pods}/{ns.total_pods} running)"
    else:
        return f"[red]✗ Critical[/red]   ({ns.healthy_pods}/{ns.total_pods} running)"


def _time_ago(iso_str: str) -> str:
    try:
        from datetime import timezone
        dt   = datetime.fromisoformat(iso_str)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        return f"{secs // 3600}h ago"
    except Exception:
        return "just now"


@k8s_app.command("overview")
def k8s_overview(
    namespace: Optional[str] = typer.Option(
        None,
        "--namespace", "-n",
        help="Show only this namespace.",
    ),
    unhealthy: bool = typer.Option(
        False,
        "--unhealthy",
        help="Show only problem pods.",
    ),
) -> None:
    """Complete structured overview of the entire Kubernetes cluster."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.cluster_overview import ClusterOverviewSkill

        console.print()

        with console.status("[bold cyan]Gathering cluster data...", spinner="dots"):
            overview = ClusterOverviewSkill().get_overview()

        # ── Filter by namespace if requested ──────────────────────────────
        ns_list = overview.namespaces
        if namespace:
            ns_list = [n for n in ns_list if n.name == namespace]
            if not ns_list:
                _print_error(f"Namespace '{namespace}' not found.")
                raise typer.Exit(1)

        # ── Cluster summary panel ─────────────────────────────────────────
        console.print()
        node_color = "green" if overview.healthy_nodes == overview.total_nodes else "red"
        pod_color  = "green" if overview.unhealthy_pods == 0 else "red"

        summary_lines = [
            f"  [dim]Namespaces :[/dim]  [cyan]{overview.total_namespaces}[/cyan]"
            f"          [dim]Nodes    :[/dim]  [{node_color}]{overview.healthy_nodes}/{overview.total_nodes}[/{node_color}]",
            f"  [dim]Total Pods :[/dim]  [cyan]{overview.total_pods}[/cyan]"
            f"          [dim]Healthy  :[/dim]  [green]{overview.healthy_pods} ✓[/green]",
            f"  [dim]Unhealthy  :[/dim]  [{pod_color}]{overview.unhealthy_pods} {'✗' if overview.unhealthy_pods else '✓'}[/{pod_color}]"
            f"          [dim]Generated:[/dim]  [dim]{_time_ago(overview.generated_at)}[/dim]",
        ]

        if overview.analysis:
            summary_lines += [
                "",
                "  [bold dim]AI ANALYSIS[/bold dim]",
                *[f"  {_escape(line)}" for line in overview.analysis.splitlines()],
            ]

        console.print(Panel(
            "\n".join(summary_lines),
            title=f"[bold]CLUSTER OVERVIEW[/bold]  [cyan]{_escape(overview.cluster_name)}[/cyan]",
            border_style="cyan",
            padding=(1, 1),
        ))
        console.print()

        # ── Per-namespace tables ──────────────────────────────────────────
        for ns in ns_list:
            pods = ns.pods
            if unhealthy:
                from agent.integrations.kubectl import _is_pod_healthy  # type: ignore[attr-defined]
                pods = [p for p in pods if not _is_pod_healthy(p)]
                if not pods:
                    continue

            if not pods:
                continue

            ns_color = "green" if ns.unhealthy_pods == 0 else ("yellow" if ns.unhealthy_pods < ns.total_pods else "red")

            table = Table(
                title=f"[bold {ns_color}]NAMESPACE: {ns.name}[/bold {ns_color}]  [dim]({ns.total_pods} pods)[/dim]",
                show_lines=True,
                header_style="bold cyan",
                border_style="dim",
                title_justify="left",
            )
            table.add_column("Pod Name",  max_width=36, no_wrap=True)
            table.add_column("Status",    width=14)
            table.add_column("Ready",     justify="center", width=7)
            table.add_column("Restarts",  justify="right", width=9)
            table.add_column("Age",       width=6)
            table.add_column("Image",     max_width=34, no_wrap=True)

            for pod in pods:
                # Use first image only (most pods have one container)
                image = pod.images[0] if pod.images else "[dim]—[/dim]"
                # Trim registry prefix for readability
                if "/" in image:
                    image = image.split("/")[-1]
                table.add_row(
                    _escape(pod.name),
                    _status_markup(pod.status),
                    pod.ready,
                    _restart_markup(pod.restarts),
                    pod.age,
                    _escape(image),
                )

            console.print(table)
            console.print()

        # ── Namespace health summary ──────────────────────────────────────
        if not namespace:
            summary_table = Table(
                title="[bold]CLUSTER HEALTH SUMMARY[/bold]",
                show_lines=True,
                header_style="bold cyan",
                border_style="dim",
            )
            summary_table.add_column("Namespace",  width=24)
            summary_table.add_column("Health",     width=38)

            for ns in overview.namespaces:
                if ns.total_pods == 0:
                    continue
                summary_table.add_row(_escape(ns.name), _ns_health_label(ns))

            console.print(summary_table)
            console.print()

        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent k8s full-scan
# ---------------------------------------------------------------------------

_AREA_COLOR = {
    "nodes":       "cyan",
    "pods":        "green",
    "dns":         "blue",
    "network":     "magenta",
    "pvcs":        "yellow",
    "jobs":        "dark_orange",
    "hpa":         "cyan",
    "ingress":     "blue",
    "rbac":        "red",
    "tls":         "bold yellow",
    "domain_live": "bold green",
}


@k8s_app.command("full-scan")
def k8s_full_scan(
    areas: Optional[str] = typer.Option(
        None, "--areas", "-a",
        help="Comma-separated areas to scan. Default: all. "
             "Options: nodes,pods,dns,network,pvcs,jobs,hpa,ingress,rbac,tls",
    ),
    fix: bool = typer.Option(
        False, "--fix",
        help="Ask permission and apply each fix command after scan.",
    ),
    critical_only: bool = typer.Option(
        False, "--critical",
        help="Show only critical issues.",
    ),
) -> None:
    """
    Full cluster health audit — all areas in parallel, one Claude call.

    Runs 9 collectors simultaneously (nodes, pods, DNS, network, PVCs,
    jobs, HPA, ingress, RBAC), sends everything to Claude in one prompt,
    then deep-dives only on critical issues that need it.
    """
    try:
        from agent.integrations.collectors import COLLECTORS
        from agent.observability.costs import get_session_total
        from agent.skills.full_scan import FullScanSkill

        area_list = [a.strip() for a in areas.split(",")] if areas else None

        console.print()
        console.print(Rule("[bold cyan]Full Cluster Scan[/bold cyan]"))
        console.print()

        selected = area_list or list(COLLECTORS.keys())
        console.print(
            f"  Scanning [bold]{len(selected)}[/bold] area(s) in parallel: "
            + "  ".join(
                f"[{_AREA_COLOR.get(a,'white')}]{a}[/{_AREA_COLOR.get(a,'white')}]"
                for a in selected
            )
        )
        console.print()

        with console.status(
            "[bold cyan]Collecting data from all areas simultaneously...[/bold cyan]",
            spinner="dots",
        ):
            report = FullScanSkill().scan(areas=area_list)

        # ── Collection summary ────────────────────────────────────────────
        elapsed = f"{report.collection_ms / 1000:.1f}s"
        console.print(
            f"  Collection: [green]{len(report.areas_scanned)} OK[/green]  "
            + (f"[red]{len(report.areas_failed)} failed ({', '.join(report.areas_failed)})[/red]  " if report.areas_failed else "")
            + f"[dim]{elapsed}[/dim]"
        )
        console.print()

        # ── AI analysis panel ─────────────────────────────────────────────
        if report.analysis:
            console.print(Panel(
                _escape(report.analysis),
                title="[bold]AI ANALYSIS[/bold]",
                border_style="cyan",
                padding=(0, 1),
            ))
            console.print()

        # ── Issue counts ──────────────────────────────────────────────────
        critical = [i for i in report.issues if i.severity == "critical"]
        warnings = [i for i in report.issues if i.severity == "warning"]
        infos    = [i for i in report.issues if i.severity == "info"]

        console.print(
            f"  Issues found:  "
            f"[bold red]{len(critical)} critical[/bold red]   "
            f"[yellow]{len(warnings)} warnings[/yellow]   "
            f"[cyan]{len(infos)} info[/cyan]"
        )
        console.print()

        if not report.issues:
            console.print(Panel(
                "[bold green]Cluster is healthy![/bold green]  No issues found across all scanned areas.",
                border_style="green",
            ))
            console.print()
            _cost_footer(get_session_total())
            return

        # ── Issues display ────────────────────────────────────────────────
        display_issues = critical + warnings + ([] if critical_only else infos)

        for i, issue in enumerate(display_issues, 1):
            sev_color = _SEVERITY_COLOR.get(issue.severity, "white")
            sev_icon  = _SEVERITY_ICON.get(issue.severity, "•")
            area_color = _AREA_COLOR.get(issue.area, "white")
            divider    = "[dim]" + "-" * 56 + "[/dim]"

            lines = [
                f"[dim]Area     :[/dim] [{area_color}]{_escape(issue.area)}[/{area_color}]"
                f"   [dim]Severity:[/dim] [{sev_color}]{issue.severity.upper()}[/{sev_color}]",
                f"[dim]Resource :[/dim] [cyan]{_escape(issue.resource)}[/cyan]"
                + (f"  [dim]{_escape(issue.namespace)}[/dim]" if issue.namespace else ""),
                f"[dim]Problem  :[/dim] {_escape(issue.description)}",
                divider,
                f"[dim]Fix      :[/dim] {_escape(issue.fix)}",
            ]
            if issue.fix_command:
                lines += [
                    divider,
                    f"[dim]Command  :[/dim] [bold cyan]{_escape(issue.fix_command)}[/bold cyan]",
                ]
            if issue.deep_dive:
                lines.append("[dim]          (deep-dive analysis applied)[/dim]")

            console.print(Panel(
                "\n".join(lines),
                title=f"[{sev_color}]{sev_icon} {i}/{len(display_issues)}  [{issue.severity.upper()}][/{sev_color}]",
                border_style=sev_color,
                padding=(1, 2),
            ))
            console.print()

        # ── Fix loop ──────────────────────────────────────────────────────
        if fix:
            from agent.integrations.kubectl import apply_fix as kubectl_apply_fix

            fixable = [i for i in display_issues if i.fix_command]
            if not fixable:
                console.print("[dim]No automated fix commands available.[/dim]\n")
            else:
                console.print(Rule("[bold yellow]Fix Mode[/bold yellow]"))
                console.print()
                fixed = skipped = failed = 0

                for issue in fixable:
                    sev_color = _SEVERITY_COLOR.get(issue.severity, "white")
                    console.print(
                        f"  [{sev_color}]{issue.severity.upper()}[/{sev_color}]  "
                        f"[bold]{_escape(issue.area)}[/bold]  "
                        f"[cyan]{_escape(issue.resource)}[/cyan]"
                    )
                    console.print(f"  [dim]Fix:[/dim] {_escape(issue.fix)}")
                    console.print(f"  [dim]Cmd:[/dim] [cyan]{_escape(issue.fix_command)}[/cyan]")
                    console.print()

                    if not typer.confirm("  Apply this fix?", default=False):
                        console.print("  [dim]Skipped.[/dim]\n")
                        skipped += 1
                        continue

                    with console.status("[bold yellow]Applying...[/bold yellow]", spinner="dots"):
                        result = kubectl_apply_fix(issue.fix_command)

                    if result.success:
                        console.print("  [bold green]✓ Applied.[/bold green]\n")
                        fixed += 1
                    else:
                        console.print(f"  [bold red]✗ Failed:[/bold red] {_escape(result.error[:100])}\n")
                        failed += 1

                console.print(Rule(style="dim"))
                console.print(
                    f"\n  [bold]Result:[/bold]  "
                    f"[green]{fixed} fixed[/green]  "
                    f"[dim]{skipped} skipped[/dim]  "
                    f"[red]{failed} failed[/red]\n"
                )

        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent k8s node-health
# ---------------------------------------------------------------------------

_SEVERITY_COLOR = {"critical": "bold red", "warning": "yellow", "info": "cyan"}
_SEVERITY_ICON  = {"critical": "✗", "warning": "⚠", "info": "ℹ"}


@k8s_app.command("node-health")
def k8s_node_health(
    fix: bool = typer.Option(
        False, "--fix",
        help="After showing issues, ask permission and apply each fix.",
    ),
) -> None:
    """
    Scan node memory, disk, and PVC health — diagnose with Claude.

    Shows a full report: node metrics, PVC status, top memory pods,
    and a prioritised list of issues. With --fix, asks permission
    before applying each fix command.
    """
    try:
        from agent.integrations.kubectl import apply_fix as kubectl_apply_fix
        from agent.integrations.node_inspector import (
            fix_delete_pod,
            fix_expand_pvc,
            fix_truncate_logs_in_pod,
        )
        from agent.observability.costs import get_session_total
        from agent.skills.node_healer import NodeHealerSkill

        console.print()
        console.print(Rule("[bold cyan]Node Health Scan[/bold cyan]"))
        console.print()

        with console.status("[bold cyan]Collecting node data...", spinner="dots"):
            report = NodeHealerSkill().scan()

        # ── Node metrics table ────────────────────────────────────────────
        if report.node_metrics:
            nm_table = Table(
                title="[bold]NODE METRICS[/bold]",
                show_lines=True, header_style="bold cyan",
                border_style="dim", title_justify="left",
            )
            nm_table.add_column("Node",        max_width=50, no_wrap=True)
            nm_table.add_column("CPU Usage",   justify="right", width=12)
            nm_table.add_column("CPU %",       justify="right", width=8)
            nm_table.add_column("Memory",      justify="right", width=12)
            nm_table.add_column("Memory %",    justify="right", width=10)

            for nm in report.node_metrics:
                cpu_color = "red" if nm.cpu_percent > 80 else "yellow" if nm.cpu_percent > 60 else "green"
                mem_color = "red" if nm.memory_percent > 80 else "yellow" if nm.memory_percent > 60 else "green"
                # shorten long node names
                node_display = nm.name if len(nm.name) <= 48 else nm.name[:20] + "…" + nm.name[-20:]
                nm_table.add_row(
                    _escape(node_display),
                    nm.cpu_cores,
                    f"[{cpu_color}]{nm.cpu_percent:.0f}%[/{cpu_color}]",
                    nm.memory_bytes,
                    f"[{mem_color}]{nm.memory_percent:.0f}%[/{mem_color}]",
                )

            console.print(nm_table)
            console.print()
        else:
            console.print("  [dim]metrics-server not available — install it for CPU/memory data.[/dim]\n")

        # ── PVC table ─────────────────────────────────────────────────────
        if report.pvcs:
            pvc_table = Table(
                title="[bold]PERSISTENT VOLUMES (PVCs)[/bold]",
                show_lines=True, header_style="bold cyan",
                border_style="dim", title_justify="left",
            )
            pvc_table.add_column("Namespace",     width=18)
            pvc_table.add_column("PVC Name",      max_width=28, no_wrap=True)
            pvc_table.add_column("Status",        justify="center", width=10)
            pvc_table.add_column("Capacity",      justify="right",  width=10)
            pvc_table.add_column("Storage Class", width=18)
            pvc_table.add_column("Access",        width=14)

            for pvc in report.pvcs:
                st_color = "green" if pvc.status == "Bound" else "red"
                pvc_table.add_row(
                    _escape(pvc.namespace),
                    _escape(pvc.name),
                    f"[{st_color}]{pvc.status}[/{st_color}]",
                    pvc.capacity,
                    _escape(pvc.storage_class),
                    ", ".join(pvc.access_modes),
                )

            console.print(pvc_table)
            console.print()

        # ── Top pods table ────────────────────────────────────────────────
        if report.top_pods:
            tp_table = Table(
                title="[bold]TOP PODS BY MEMORY[/bold]",
                show_lines=True, header_style="bold cyan",
                border_style="dim", title_justify="left",
            )
            tp_table.add_column("Namespace",  width=18)
            tp_table.add_column("Pod",        max_width=36, no_wrap=True)
            tp_table.add_column("CPU",        justify="right", width=10)
            tp_table.add_column("Memory",     justify="right", width=12)

            for pod in report.top_pods[:10]:
                tp_table.add_row(
                    _escape(pod.get("namespace", "?")),
                    _escape(pod.get("name", "?")),
                    pod.get("cpu", "?"),
                    f"[cyan]{pod.get('memory', '?')}[/cyan]",
                )

            console.print(tp_table)
            console.print()

        # ── AI analysis panel ─────────────────────────────────────────────
        if report.analysis:
            console.print(Panel(
                _escape(report.analysis),
                title="[bold]AI ANALYSIS[/bold]",
                border_style="cyan",
                padding=(0, 1),
            ))
            console.print()

        # ── Issues ────────────────────────────────────────────────────────
        if not report.issues:
            console.print(Panel(
                "[bold green]All nodes healthy![/bold green]  No issues detected.",
                border_style="green",
            ))
            console.print()
            _cost_footer(get_session_total())
            return

        console.print(Rule(f"[bold red]{len(report.issues)} Issue(s) Found[/bold red]"))
        console.print()

        for i, issue in enumerate(report.issues, 1):
            sev_color = _SEVERITY_COLOR.get(issue.severity, "white")
            sev_icon  = _SEVERITY_ICON.get(issue.severity, "•")
            divider   = "[dim]" + "-" * 56 + "[/dim]"

            lines = [
                f"[dim]Type     :[/dim] [{sev_color}]{_escape(issue.issue_type)}[/{sev_color}]",
                f"[dim]Resource :[/dim] [cyan]{_escape(issue.resource)}[/cyan]"
                + (f"  [dim]{_escape(issue.namespace)}[/dim]" if issue.namespace else ""),
                f"[dim]Problem  :[/dim] {_escape(issue.description)}",
                divider,
                f"[dim]Fix      :[/dim] {_escape(issue.fix)}",
            ]
            if issue.fix_command:
                lines += [
                    divider,
                    f"[dim]Command  :[/dim] [bold cyan]{_escape(issue.fix_command)}[/bold cyan]",
                ]

            console.print(Panel(
                "\n".join(lines),
                title=f"[{sev_color}]{sev_icon}  Issue {i}  [{issue.severity.upper()}][/{sev_color}]",
                border_style=sev_color,
                padding=(1, 2),
            ))
            console.print()

        # ── Interactive fix loop ──────────────────────────────────────────
        if fix:
            console.print(Rule("[bold yellow]Fix Mode — Asking permission for each fix[/bold yellow]"))
            console.print()

            fixed = skipped = failed = 0

            for issue in report.issues:
                if not issue.fix_command:
                    console.print(
                        f"  [dim]Skipping[/dim] [cyan]{issue.issue_type}[/cyan] "
                        "[dim]— no automated fix command[/dim]"
                    )
                    skipped += 1
                    continue

                sev_color = _SEVERITY_COLOR.get(issue.severity, "white")
                console.print(
                    f"\n  [{sev_color}]{issue.severity.upper()}[/{sev_color}]  "
                    f"[bold]{_escape(issue.issue_type)}[/bold]  "
                    f"[cyan]{_escape(issue.resource)}[/cyan]"
                )
                console.print(f"  [dim]Fix:[/dim] {_escape(issue.fix)}")
                console.print(f"  [dim]Cmd:[/dim] [cyan]{_escape(issue.fix_command)}[/cyan]")
                console.print()

                apply = typer.confirm("  Apply this fix?", default=False)
                if not apply:
                    console.print("  [dim]Skipped.[/dim]")
                    skipped += 1
                    continue

                with console.status("[bold yellow]Applying...", spinner="dots"):
                    result = kubectl_apply_fix(issue.fix_command)

                if result.success:
                    console.print("  [bold green]✓ Applied successfully.[/bold green]")
                    fixed += 1
                else:
                    console.print(f"  [bold red]✗ Failed:[/bold red] {_escape(result.error[:120])}")
                    failed += 1

            console.print()
            console.print(Rule(style="dim"))
            console.print(
                f"\n  [bold]Result:[/bold]  "
                f"[green]{fixed} fixed[/green]  "
                f"[dim]{skipped} skipped[/dim]  "
                f"[red]{failed} failed[/red]\n"
            )

        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent k8s autofix
# ---------------------------------------------------------------------------

@k8s_app.command("autofix")
def k8s_autofix(
    namespace: str = typer.Option(
        "all", "--namespace", "-n",
        help="Namespace to watch, or 'all'.",
        show_default=True,
    ),
    watch: bool = typer.Option(
        False, "--watch", "-w",
        help="Keep running — fix new crashes as they appear (Ctrl+C to stop).",
    ),
    interval: int = typer.Option(
        30, "--interval", "-i",
        help="Seconds between scans in watch mode.",
        show_default=True,
    ),
) -> None:
    """
    Scan for crashing pods and automatically fix them.

    Two-phase fix: tries Claude's suggested command first, then falls back
    to fetching the full deployment YAML and asking Claude for an exact
    kubectl patch command.
    """
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.autohealer import AutoHealerSkill

        skill = AutoHealerSkill()
        console.print()

        def _run_once() -> int:
            """Run one heal pass. Returns number of pods fixed."""
            console.print(Rule("[bold cyan]AutoFix — Scanning cluster...[/bold cyan]"))
            console.print()

            with console.status("[bold cyan]Detecting problem pods...", spinner="dots"):
                results = skill.heal_all(namespace)

            if not results:
                console.print(Panel(
                    "[bold green]All pods healthy![/bold green]  Nothing to fix.",
                    border_style="green",
                ))
                console.print()
                return 0

            fixed = 0
            for r in results:
                phase       = r.get("phase", "?")
                success     = r.get("success", False)
                fix_applied = r.get("fix_applied")
                new_status  = r.get("new_status", "?")
                explanation = r.get("explanation", "")

                status_icon  = "[bold green]FIXED[/bold green]" if success else "[bold red]FAILED[/bold red]"
                phase_label  = "[dim]phase1[/dim]" if phase == "phase1" else "[yellow]phase2 (deep patch)[/yellow]"
                conf_color   = _CONFIDENCE_COLOR.get(r.get("confidence", "low"), "white")

                lines = [
                    f"[dim]Problem    :[/dim] [red]{_escape(r['problem_type'])}[/red]"
                    f"   [dim]confidence:[/dim] [{conf_color}]{r['confidence']}[/{conf_color}]",
                    f"[dim]Fix phase  :[/dim] {phase_label}",
                    f"[dim]Explanation:[/dim] {_escape(explanation or '—')}",
                ]
                if fix_applied:
                    lines += [
                        "[dim]" + "-" * 56 + "[/dim]",
                        f"[dim]Command    :[/dim] [cyan]{_escape(fix_applied)}[/cyan]",
                    ]
                if new_status:
                    ns_color = "green" if new_status == "Running" else "yellow"
                    lines.append(f"[dim]New status :[/dim] [{ns_color}]{_escape(new_status)}[/{ns_color}]")

                console.print(Panel(
                    "\n".join(lines),
                    title=f"{status_icon}  [bold]{_escape(r['pod'])}[/bold]  [dim]{_escape(r['namespace'])}[/dim]",
                    border_style="green" if success else "red",
                    padding=(1, 2),
                ))
                console.print()

                if success:
                    fixed += 1

            console.print(Rule(style="dim"))
            console.print(
                f"\n  [bold]Result:[/bold]  "
                f"[green]{fixed} fixed[/green]  "
                f"[red]{len(results) - fixed} failed[/red]\n"
            )
            return fixed

        if not watch:
            _run_once()
            _cost_footer(get_session_total())
            return

        # ── Watch mode ────────────────────────────────────────────────────
        console.print(Panel(
            f"[bold cyan]AutoFix Watch[/bold cyan]  "
            f"namespace=[green]{namespace}[/green]  "
            f"interval=[green]{interval}s[/green]\n"
            "[dim]Monitoring cluster — fixes applied automatically. Ctrl+C to stop.[/dim]",
            border_style="cyan",
        ))
        console.print()

        healed_pods: set[str] = set()

        while True:
            from agent.integrations.kubectl import get_problematic_pods as _gpods
            with console.status("[dim]Scanning...[/dim]", spinner="dots"):
                problem_pods = _gpods()
                if namespace != "all":
                    problem_pods = [p for p in problem_pods if p.namespace == namespace]

            new_problems = [
                p for p in problem_pods
                if f"{p.namespace}/{p.name}" not in healed_pods
            ]

            ts = datetime.now().strftime("%H:%M:%S")
            if not new_problems:
                console.print(f"  [dim]{ts}[/dim]  [green]All healthy — nothing to fix[/green]")
            else:
                console.print(
                    f"  [dim]{ts}[/dim]  "
                    f"[bold red]{len(new_problems)} new problem(s) detected — healing...[/bold red]"
                )
                for pod in new_problems:
                    with console.status(
                        f"[bold cyan]Healing {pod.name}...[/bold cyan]", spinner="dots"
                    ):
                        r = skill.heal_pod(pod)

                    key = f"{pod.namespace}/{pod.name}"
                    if r["success"]:
                        console.print(
                            f"    [green]✓ Fixed[/green]  [cyan]{pod.name}[/cyan]"
                            f"  [dim]via {r['phase']}[/dim]  "
                            f"new status: [green]{r.get('new_status', '?')}[/green]"
                        )
                        healed_pods.add(key)
                    else:
                        console.print(
                            f"    [red]✗ Failed[/red]  [cyan]{pod.name}[/cyan]  "
                            f"[dim]{r.get('explanation', '')[:60]}[/dim]"
                        )

            time.sleep(interval)

    except KeyboardInterrupt:
        console.print("\n[dim]AutoFix watch stopped.[/dim]\n")
    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent k8s pods
# ---------------------------------------------------------------------------

@k8s_app.command("pods")
def k8s_pods(
    namespace: Optional[str] = typer.Option(
        None,
        "--namespace", "-n",
        help="Filter to a single namespace.",
    ),
    status_filter: Optional[str] = typer.Option(
        None,
        "--status", "-s",
        help="Filter by status (e.g. Running, Pending, CrashLoopBackOff).",
    ),
    sort_by: str = typer.Option(
        "namespace",
        "--sort",
        help="Sort by: namespace | status | restarts | age",
        show_default=True,
    ),
) -> None:
    """Show every pod across the entire cluster with full details, plus node info."""
    try:
        from agent.integrations.kubectl import (
            _get_cluster_name,
            _is_pod_healthy,
            get_nodes_detail,
            get_pods,
        )

        console.print()

        # ── Gather data ───────────────────────────────────────────────────
        with console.status("[bold cyan]Fetching cluster data...", spinner="dots"):
            cluster_name = _get_cluster_name()
            nodes        = get_nodes_detail()
            all_pods     = get_pods("all" if not namespace else namespace)

        # ── Apply filters ─────────────────────────────────────────────────
        if status_filter:
            all_pods = [p for p in all_pods if p.status.lower() == status_filter.lower()]

        # ── Sort ──────────────────────────────────────────────────────────
        sort_key = {
            "namespace": lambda p: (p.namespace, p.name),
            "status":    lambda p: (p.status, p.namespace, p.name),
            "restarts":  lambda p: (-p.restarts, p.namespace, p.name),
            "age":       lambda p: (p.namespace, p.name),
        }.get(sort_by, lambda p: (p.namespace, p.name))
        all_pods.sort(key=sort_key)

        total   = len(all_pods)
        healthy = sum(1 for p in all_pods if _is_pod_healthy(p))

        # ── Cluster + node summary panel ──────────────────────────────────
        node_ready   = sum(1 for n in nodes if n.status == "Ready")
        node_color   = "green" if node_ready == len(nodes) else "red"
        pod_color    = "green" if healthy == total else "yellow" if healthy > 0 else "red"

        summary_lines = [
            f"  [dim]Cluster    :[/dim]  [cyan]{_escape(cluster_name)}[/cyan]",
            f"  [dim]Nodes      :[/dim]  [{node_color}]{node_ready}/{len(nodes)} Ready[/{node_color}]",
            f"  [dim]Total Pods :[/dim]  [{pod_color}]{healthy}/{total} healthy[/{pod_color}]"
            + (f"   [dim](filtered to {_escape(namespace)})[/dim]" if namespace else ""),
        ]
        console.print(Panel(
            "\n".join(summary_lines),
            title="[bold]CLUSTER INFO[/bold]",
            border_style="cyan",
            padding=(0, 1),
        ))
        console.print()

        # ── Node details table ────────────────────────────────────────────
        node_table = Table(
            title=f"[bold]NODES[/bold]  [dim]({len(nodes)} total)[/dim]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
            title_justify="left",
        )
        node_table.add_column("Node Name",      max_width=50, no_wrap=True)
        node_table.add_column("Status",         width=10)
        node_table.add_column("Roles",          width=12)
        node_table.add_column("Age",            width=6)
        node_table.add_column("Version",        width=18)
        node_table.add_column("Instance Type",  width=14)
        node_table.add_column("CPU",            justify="right", width=8)
        node_table.add_column("Memory",         justify="right", width=12)

        for n in nodes:
            n_color = "green" if n.status == "Ready" else "bold red"
            node_table.add_row(
                _escape(n.name),
                f"[{n_color}]{n.status}[/{n_color}]",
                ", ".join(n.roles),
                n.age,
                _escape(n.kubelet_version),
                _escape(n.instance_type),
                n.allocatable_cpu,
                _escape(n.allocatable_memory),
            )

        console.print(node_table)
        console.print()

        # ── All pods flat table ───────────────────────────────────────────
        pod_table = Table(
            title=f"[bold]ALL PODS[/bold]  [dim]({total} pods"
                  + (f", filtered: {status_filter}" if status_filter else "")
                  + ")[/dim]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
            title_justify="left",
        )
        pod_table.add_column("Namespace",  width=18, no_wrap=True)
        pod_table.add_column("Pod Name",   max_width=38, no_wrap=True)
        pod_table.add_column("Status",     width=16)
        pod_table.add_column("Ready",      justify="center", width=7)
        pod_table.add_column("Restarts",   justify="right",  width=9)
        pod_table.add_column("Age",        width=6)
        pod_table.add_column("Node",       max_width=22, no_wrap=True)
        pod_table.add_column("IP",         width=16)
        pod_table.add_column("Image",      max_width=30, no_wrap=True)

        prev_ns = None
        for pod in all_pods:
            # Dim repeated namespace names for readability
            ns_label = (
                f"[cyan]{_escape(pod.namespace)}[/cyan]"
                if pod.namespace != prev_ns
                else "[dim]↳[/dim]"
            )
            prev_ns = pod.namespace

            image = pod.images[0] if pod.images else "—"
            if "/" in image:
                image = image.split("/")[-1]

            # Shorten node name (EKS node names are very long)
            node_display = pod.node
            if len(node_display) > 20:
                node_display = node_display[:10] + "…" + node_display[-8:]

            pod_table.add_row(
                ns_label,
                _escape(pod.name),
                _status_markup(pod.status),
                pod.ready,
                _restart_markup(pod.restarts),
                pod.age,
                _escape(node_display),
                _escape(pod.ip) if pod.ip else "[dim]—[/dim]",
                _escape(image),
            )

        console.print(pod_table)
        console.print()

        # ── Quick stats ───────────────────────────────────────────────────
        from collections import Counter
        status_counts = Counter(p.status for p in all_pods)
        ns_counts     = Counter(p.namespace for p in all_pods)

        stats_table = Table(
            show_header=False, box=None, padding=(0, 3), title_justify="left"
        )
        stats_table.add_column("Label", style="dim", width=18)
        stats_table.add_column("Value")

        # Status breakdown
        status_parts = []
        for st, count in sorted(status_counts.items(), key=lambda x: -x[1]):
            color = _STATUS_COLOR.get(st, "white")
            status_parts.append(f"[{color}]{st}[/{color}] ×{count}")
        stats_table.add_row("By status", "   ".join(status_parts))

        # Namespace breakdown
        ns_parts = [
            f"[cyan]{ns}[/cyan]={cnt}"
            for ns, cnt in sorted(ns_counts.items(), key=lambda x: -x[1])
        ]
        stats_table.add_row("By namespace", "   ".join(ns_parts))

        console.print(Rule("[bold dim]QUICK STATS[/bold dim]"))
        console.print(stats_table)
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Helpers: aws
# ---------------------------------------------------------------------------

_AWS_RESOURCE_COLOR = {
    "EC2":     "cyan",
    "RDS":     "magenta",
    "ALB":     "yellow",
    "EKSNode": "blue",
}

_AWS_PROBLEM_COLOR = {
    "Ec2Stopped":           "red",
    "Ec2StatusFailed":      "bold red",
    "Ec2CpuCreditExhausted": "yellow",
    "RdsStopped":           "red",
    "RdsStorageFull":       "bold red",
    "RdsConnectionsAtMax":  "yellow",
    "AlbUnhealthyTargets":  "red",
    "AlbNoTargets":         "bold red",
    "Unknown":              "dim",
}


def _print_aws_diagnosis(diagnosis) -> None:
    """Render an AWS diagnosis panel to the console."""
    conf_color  = _CONFIDENCE_COLOR.get(diagnosis.confidence.lower(), "white")
    rtype_color = _AWS_RESOURCE_COLOR.get(diagnosis.resource_type.value, "white")
    prob_color  = _AWS_PROBLEM_COLOR.get(diagnosis.problem_type.value, "white")
    divider     = "[dim]" + "-" * 58 + "[/dim]"

    lines = [
        f"[dim]Resource :[/dim] [{rtype_color}]{_escape(diagnosis.resource_type.value)}[/{rtype_color}]  "
        f"[dim]{_escape(diagnosis.resource_id)}[/dim]",
        f"[dim]Problem  :[/dim] [{prob_color}]{_escape(diagnosis.problem_type.value)}[/{prob_color}]",
        f"[dim]Region   :[/dim] {_escape(diagnosis.region)}",
        f"[dim]Root Cause:[/dim] {_escape(diagnosis.root_cause)}",
        f"[dim]Confidence:[/dim] [bold {conf_color}]{_escape(diagnosis.confidence.upper())}[/bold {conf_color}]",
        divider,
        "[bold]EXPLANATION[/bold]",
        _escape(diagnosis.explanation),
        divider,
        "[bold]SUGGESTED FIX[/bold]",
        _escape(diagnosis.suggested_fix),
    ]

    if diagnosis.fix_command:
        lines += [
            divider,
            "[bold]COMMAND[/bold]",
            f"[bold cyan]{_escape(diagnosis.fix_command)}[/bold cyan]",
        ]

    console.print(Panel(
        "\n".join(lines),
        title=f"[bold]AWS DIAGNOSIS:[/bold] [cyan]{_escape(diagnosis.resource_name)}[/cyan]",
        border_style="cyan",
        padding=(1, 2),
    ))


# ---------------------------------------------------------------------------
# Command: agent aws scan
# ---------------------------------------------------------------------------

_DEFAULT_REGION = "ap-south-1"


@aws_app.command("scan")
def aws_scan(
    region: str = typer.Option(
        _DEFAULT_REGION,
        "--region", "-r",
        help="AWS region to scan.",
        show_default=True,
    ),
    services: str = typer.Option(
        "ec2,rds,alb",
        "--services", "-s",
        help="Comma-separated list of services to check: ec2, rds, alb.",
        show_default=True,
    ),
) -> None:
    """Scan the AWS account for unhealthy EC2, RDS, and ALB resources."""
    try:
        from agent.skills.aws import AwsSkill

        skill     = AwsSkill()
        svc_list  = [s.strip().lower() for s in services.split(",")]
        console.print()

        with console.status(
            f"[bold cyan]Scanning {region} ({', '.join(svc_list)})...[/bold cyan]",
            spinner="dots",
        ):
            diagnoses = skill.scan_account(region, svc_list)

        console.print()

        if not diagnoses:
            console.print(Panel(
                "[bold green]All resources healthy![/bold green]  No problems detected.",
                border_style="green",
            ))
            console.print()
            return

        table = Table(
            title=f"[bold red]{len(diagnoses)} problem(s) found — {region}[/bold red]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        table.add_column("Name",       max_width=30, no_wrap=True)
        table.add_column("Type",       width=10)
        table.add_column("Problem",    width=22)
        table.add_column("Status",     max_width=28, no_wrap=True)
        table.add_column("Confidence", justify="center", width=11)

        for d in diagnoses:
            rtype_color = _AWS_RESOURCE_COLOR.get(d.resource_type.value, "white")
            prob_color  = _AWS_PROBLEM_COLOR.get(d.problem_type.value, "white")
            table.add_row(
                d.resource_name,
                f"[{rtype_color}]{d.resource_type.value}[/{rtype_color}]",
                f"[{prob_color}]{d.problem_type.value}[/{prob_color}]",
                d.root_cause[:26] + "…" if len(d.root_cause) > 27 else d.root_cause,
                _confidence_markup(d.confidence),
            )

        console.print(table)
        console.print()
        console.print(
            f"  [dim]Run:[/dim] [bold]agent aws heal --region {region}[/bold]  "
            "[dim]to interactively fix all problems.[/dim]"
        )
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent aws diagnose RESOURCE_ID
# ---------------------------------------------------------------------------

@aws_app.command("diagnose")
def aws_diagnose(
    resource_id: str = typer.Argument(..., help="AWS resource ID (instance ID, DB identifier, or target group ARN)."),
    resource_type: str = typer.Option(
        ...,
        "--type", "-t",
        help="Resource type: ec2 | rds | alb",
    ),
    region: str = typer.Option(
        _DEFAULT_REGION,
        "--region", "-r",
        help="AWS region.",
        show_default=True,
    ),
    auto_fix: bool = typer.Option(
        False,
        "--auto-fix",
        help="Apply the fix automatically without prompting.",
    ),
) -> None:
    """Diagnose a specific AWS resource and optionally apply the fix."""
    try:
        from agent.core.models import AwsResource, AwsResourceType
        from agent.skills.aws import AwsSkill

        type_map = {"ec2": AwsResourceType.EC2, "rds": AwsResourceType.RDS, "alb": AwsResourceType.ALB}
        rtype = type_map.get(resource_type.lower())
        if rtype is None:
            _print_error(f"Unknown resource type: {resource_type!r}. Use: ec2, rds, alb")
            raise typer.Exit(1)

        skill    = AwsSkill()
        resource = AwsResource(
            id=resource_id,
            name=resource_id,
            resource_type=rtype,
            status="unknown",
            region=region,
        )
        console.print()

        with console.status("[bold cyan]Analyzing with Claude...", spinner="dots"):
            diagnosis = skill.diagnose_resource(resource)

        console.print()
        _print_aws_diagnosis(diagnosis)
        console.print()

        if not diagnosis.fix_command:
            console.print("[dim]No automated fix command available for this issue.[/dim]")
            console.print()
            return

        apply = auto_fix or typer.confirm("Apply this fix?", default=False)

        if not apply:
            console.print("[dim]Fix skipped.[/dim]")
            console.print()
            return

        with console.status("[bold yellow]Applying fix...", spinner="dots"):
            success = skill.apply_fix(diagnosis)

        if success:
            console.print("[bold green]Fix applied successfully.[/bold green]")
        else:
            console.print("[bold red]Fix command failed.[/bold red]  Check output above.")
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent aws heal
# ---------------------------------------------------------------------------

@aws_app.command("heal")
def aws_heal(
    region: str = typer.Option(
        _DEFAULT_REGION,
        "--region", "-r",
        help="AWS region to scan and heal.",
        show_default=True,
    ),
    services: str = typer.Option(
        "ec2,rds,alb",
        "--services", "-s",
        help="Comma-separated services to check.",
        show_default=True,
    ),
) -> None:
    """Scan the AWS account, diagnose every problem, and interactively apply fixes."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.aws import AwsSkill

        skill    = AwsSkill()
        svc_list = [s.strip().lower() for s in services.split(",")]
        console.print()
        console.print(Rule(f"[bold cyan]AWS Account Heal — {region}[/bold cyan]"))
        console.print()

        with console.status(
            f"[bold cyan]Scanning {region} ({', '.join(svc_list)})...[/bold cyan]",
            spinner="dots",
        ):
            diagnoses = skill.scan_account(region, svc_list)

        if not diagnoses:
            console.print(Panel(
                "[bold green]All resources healthy![/bold green]  Nothing to fix.",
                border_style="green",
            ))
            console.print()
            return

        console.print(f"  Found [bold red]{len(diagnoses)}[/bold red] problem(s). Diagnosing each one...\n")

        fixed = skipped = failed = 0

        for i, diagnosis in enumerate(diagnoses, 1):
            console.print(Rule(
                f"[dim]{i}/{len(diagnoses)}[/dim]  "
                f"[cyan]{diagnosis.resource_name}[/cyan]  "
                f"[dim]{diagnosis.resource_type.value}[/dim]"
            ))
            console.print()
            _print_aws_diagnosis(diagnosis)
            console.print()

            if not diagnosis.fix_command:
                console.print("[dim]No automated fix available — manual investigation needed.[/dim]\n")
                skipped += 1
                continue

            apply = typer.confirm(
                f"  Apply fix for [cyan]{diagnosis.resource_name}[/cyan]?",
                default=False,
            )

            if not apply:
                console.print("  [dim]Skipped.[/dim]\n")
                skipped += 1
                continue

            with console.status("[bold yellow]Applying fix...", spinner="dots"):
                success = skill.apply_fix(diagnosis)

            if success:
                console.print("  [bold green]Fix applied.[/bold green]\n")
                fixed += 1
            else:
                console.print("  [bold red]Fix command failed.[/bold red]  Check logs above.\n")
                failed += 1

        console.print(Rule(style="dim"))
        console.print(
            f"\n  [bold]Summary:[/bold]  "
            f"[green]{fixed} fixed[/green]  "
            f"[dim]{skipped} skipped[/dim]  "
            f"[red]{failed} failed[/red]\n"
        )

        if fixed > 0:
            console.print(f"  Run [bold]agent aws scan --region {region}[/bold] to confirm everything is healthy.\n")

        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent aws history
# ---------------------------------------------------------------------------

@aws_app.command("history")
def aws_history(
    limit: int = typer.Option(
        10,
        "--limit", "-n",
        help="Number of past diagnoses to show.",
        show_default=True,
    ),
) -> None:
    """Show past AWS diagnoses stored in memory."""
    try:
        from agent.memory.store import get_by_source

        diagnoses = get_by_source("aws-diagnose")[:limit]
        fixes     = {
            m.metadata.get("resource_id"): m
            for m in get_by_source("aws-fix")
        }

        console.print()

        if not diagnoses:
            console.print("[dim]No past AWS diagnoses found in memory.[/dim]")
            console.print()
            return

        table = Table(
            title=f"AWS Diagnosis History  [dim](latest {len(diagnoses)})[/dim]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        table.add_column("Resource",    max_width=28, no_wrap=True)
        table.add_column("Type",        width=10)
        table.add_column("Problem",     width=22)
        table.add_column("Confidence",  justify="center", width=11)
        table.add_column("Fix Applied", justify="center", width=11)
        table.add_column("Timestamp",   width=17)

        for mem in diagnoses:
            meta      = mem.metadata
            rid       = meta.get("resource_name", meta.get("resource_id", "?"))
            rtype     = meta.get("resource_type", "?")
            problem   = meta.get("problem_type", "?")
            conf      = meta.get("confidence", "?")
            fix_mem   = fixes.get(meta.get("resource_id"))
            fix_ok    = fix_mem.metadata.get("success") if fix_mem else None
            fix_label = (
                "[green]yes[/green]"  if fix_ok is True  else
                "[red]failed[/red]"   if fix_ok is False  else
                "[dim]—[/dim]"
            )
            rtype_color = _AWS_RESOURCE_COLOR.get(rtype, "white")
            ts = mem.created_at.strftime("%Y-%m-%d %H:%M")
            table.add_row(
                rid,
                f"[{rtype_color}]{rtype}[/{rtype_color}]",
                problem,
                _confidence_markup(conf),
                fix_label,
                ts,
            )

        console.print(table)
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent aws list  — full inventory (EC2, EIPs, LBs, SGs)
# ---------------------------------------------------------------------------

@aws_app.command("list")
def aws_list(
    region: str = typer.Option(
        _DEFAULT_REGION,
        "--region", "-r",
        help="AWS region to inventory.",
        show_default=True,
    ),
    fix: bool = typer.Option(
        False,
        "--fix",
        help="Interactively apply fixes for flagged issues.",
    ),
) -> None:
    """Show full AWS inventory: EC2, Elastic IPs, Load Balancers, Security Groups."""
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from agent.integrations.aws import (
            get_all_ec2,
            get_all_load_balancers,
            get_elastic_ips,
            get_security_groups,
            run_aws_fix,
        )

        console.print()
        console.print(Rule(f"[bold cyan]AWS Inventory — {region}[/bold cyan]"))
        console.print()

        # Parallel fetch
        results: dict[str, list] = {}
        with console.status("[bold cyan]Fetching all AWS resources...[/bold cyan]", spinner="dots"):
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    pool.submit(get_all_ec2, region):              "ec2",
                    pool.submit(get_elastic_ips, region):          "eip",
                    pool.submit(get_all_load_balancers, region):   "lb",
                    pool.submit(get_security_groups, region):      "sg",
                }
                for fut in as_completed(futures):
                    key = futures[fut]
                    try:
                        results[key] = fut.result()
                    except Exception as exc:
                        console.print(f"[yellow]Warning: {key} fetch failed: {exc}[/yellow]")
                        results[key] = []

        ec2_list = results.get("ec2", [])
        eip_list = results.get("eip", [])
        lb_list  = results.get("lb",  [])
        sg_list  = results.get("sg",  [])

        issues: list[dict] = []

        # ── EC2 ──────────────────────────────────────────────────────────
        if ec2_list:
            tbl = Table(
                title=f"[bold white]EC2 Instances ({len(ec2_list)})[/bold white]",
                show_lines=False,
                header_style="bold dim",
                border_style="dim",
            )
            tbl.add_column("Name",          max_width=28, no_wrap=True)
            tbl.add_column("Instance ID",   width=20)
            tbl.add_column("Type",          width=12)
            tbl.add_column("State",         width=12)
            tbl.add_column("Public IP",     width=16)
            tbl.add_column("AZ",            width=14)

            for inst in sorted(ec2_list, key=lambda x: x["name"]):
                state       = inst["state"]
                state_color = (
                    "green"  if state == "running"
                    else "red"    if state in ("stopped", "terminated")
                    else "yellow"
                )
                flag = "" if inst["is_healthy"] else " [red]⚠[/red]"
                tbl.add_row(
                    f"{_escape(inst['name'])}{flag}",
                    f"[dim]{inst['id']}[/dim]",
                    inst["instance_type"],
                    f"[{state_color}]{state}[/{state_color}]",
                    inst.get("public_ip") or "[dim]—[/dim]",
                    inst.get("availability_zone") or "[dim]—[/dim]",
                )
                if not inst["is_healthy"]:
                    fix_cmd = (
                        f"aws ec2 start-instances --instance-ids {inst['id']} --region {region}"
                        if state == "stopped"
                        else None
                    )
                    issues.append({
                        "label":   f"EC2 {inst['name']} ({inst['id']}) — {state}",
                        "fix_cmd": fix_cmd,
                        "resource_id": inst["id"],
                    })

            console.print(tbl)
            console.print()

        # ── Elastic IPs ───────────────────────────────────────────────────
        if eip_list:
            tbl = Table(
                title=f"[bold white]Elastic IPs ({len(eip_list)})[/bold white]",
                show_lines=False,
                header_style="bold dim",
                border_style="dim",
            )
            tbl.add_column("IP Address",     width=18)
            tbl.add_column("Allocation ID",  width=26)
            tbl.add_column("Status",         width=14)
            tbl.add_column("Attached To",    max_width=26)

            for eip in eip_list:
                status       = "ATTACHED" if eip["is_attached"] else "UNATTACHED"
                status_color = "green" if eip["is_attached"] else "red"
                flag         = "" if eip["is_attached"] else " [red]⚠[/red]"
                attached_to  = eip.get("instance_id") or eip.get("network_interface_id") or "[dim]—[/dim]"
                tbl.add_row(
                    f"{_escape(eip['public_ip'])}{flag}",
                    f"[dim]{eip['allocation_id']}[/dim]",
                    f"[{status_color}]{status}[/{status_color}]",
                    attached_to,
                )
                if not eip["is_attached"]:
                    issues.append({
                        "label": f"Elastic IP {eip['public_ip']} — unattached (costs $0.005/hr)",
                        "fix_cmd": (
                            f"aws ec2 release-address --allocation-id {eip['allocation_id']} --region {region}"
                        ),
                        "resource_id": eip["allocation_id"],
                    })

            console.print(tbl)
            console.print()

        # ── Load Balancers ────────────────────────────────────────────────
        if lb_list:
            tbl = Table(
                title=f"[bold white]Load Balancers ({len(lb_list)})[/bold white]",
                show_lines=False,
                header_style="bold dim",
                border_style="dim",
            )
            tbl.add_column("Name",        max_width=30, no_wrap=True)
            tbl.add_column("Type",        width=8)
            tbl.add_column("State",       width=10)
            tbl.add_column("Targets",     width=12)
            tbl.add_column("DNS",         max_width=50, no_wrap=True)

            for lb in sorted(lb_list, key=lambda x: x["name"]):
                state_color  = "green" if lb["state"] == "active" else "red"
                target_str   = (
                    f"[green]{lb['healthy_targets']}/{lb['total_targets']}[/green]"
                    if lb["is_healthy"]
                    else f"[red]{lb['healthy_targets']}/{lb['total_targets']}[/red]"
                )
                flag = "" if lb["is_healthy"] else " [red]⚠[/red]"
                tbl.add_row(
                    f"{_escape(lb['name'])}{flag}",
                    lb["type"].upper(),
                    f"[{state_color}]{lb['state']}[/{state_color}]",
                    target_str if lb["total_targets"] > 0 else "[dim]no targets[/dim]",
                    f"[dim]{lb['dns_name'][:48]}[/dim]" if lb["dns_name"] else "[dim]—[/dim]",
                )
                if not lb["is_healthy"]:
                    issues.append({
                        "label":   f"LB {lb['name']} — {lb['healthy_targets']}/{lb['total_targets']} targets healthy",
                        "fix_cmd": None,
                        "resource_id": lb["arn"],
                    })

            console.print(tbl)
            console.print()

        # ── Security Groups ───────────────────────────────────────────────
        if sg_list:
            # Show only SGs with open-to-world rules first, then rest
            open_sgs  = [sg for sg in sg_list if sg["open_rules"]]
            clean_sgs = [sg for sg in sg_list if not sg["open_rules"]]

            tbl = Table(
                title=f"[bold white]Security Groups ({len(sg_list)})[/bold white]",
                show_lines=False,
                header_style="bold dim",
                border_style="dim",
            )
            tbl.add_column("Name",       max_width=28, no_wrap=True)
            tbl.add_column("ID",         width=14)
            tbl.add_column("VPC",        width=14)
            tbl.add_column("Open Ports (0.0.0.0/0)", max_width=42)

            for sg in open_sgs + clean_sgs:
                if sg["open_rules"]:
                    open_str = ", ".join(
                        f"[yellow]{r['proto']}/{r['port']}[/yellow]"
                        for r in sg["open_rules"]
                    )
                    flag = " [red]⚠[/red]"
                    issues.append({
                        "label":   f"SG {sg['name']} ({sg['id']}) — open to world: {', '.join(r['port'] for r in sg['open_rules'])}",
                        "fix_cmd": None,
                        "resource_id": sg["id"],
                    })
                else:
                    open_str = "[green]none[/green]"
                    flag     = ""

                tbl.add_row(
                    f"{_escape(sg['name'])}{flag}",
                    f"[dim]{sg['id']}[/dim]",
                    f"[dim]{sg['vpc_id'] or '—'}[/dim]",
                    open_str,
                )

            console.print(tbl)
            console.print()

        # ── Summary & issues ──────────────────────────────────────────────
        total = len(ec2_list) + len(eip_list) + len(lb_list) + len(sg_list)
        console.print(
            f"  [bold]Total:[/bold] {len(ec2_list)} EC2  {len(eip_list)} EIPs  "
            f"{len(lb_list)} LBs  {len(sg_list)} SGs  |  "
            + (
                f"[red]{len(issues)} issues found[/red]"
                if issues else "[green]all healthy[/green]"
            )
        )
        console.print()

        if not issues:
            return

        console.print(Rule("[bold yellow]Issues Found[/bold yellow]"))
        console.print()
        for i, issue in enumerate(issues, 1):
            console.print(f"  [bold]{i}.[/bold] {issue['label']}")
            if issue["fix_cmd"]:
                console.print(f"     [dim]Fix:[/dim] [bold cyan]{issue['fix_cmd']}[/bold cyan]")
            else:
                console.print("     [dim]Fix requires manual investigation (see AWS console)[/dim]")
        console.print()

        if not fix:
            console.print(
                "  [dim]Run[/dim] [bold]agent aws list --fix --region {region}[/bold] "
                "[dim]to interactively apply fixes.[/dim]"
            )
            console.print()
            return

        # Interactive fix loop
        console.print(Rule("[bold cyan]Interactive Fix[/bold cyan]"))
        console.print()
        for issue in issues:
            if not issue["fix_cmd"]:
                console.print(f"  [dim]SKIP[/dim] {issue['label']} [dim]— no automated fix[/dim]")
                continue

            console.print(f"  [bold yellow]Issue:[/bold yellow] {issue['label']}")
            console.print(f"  [bold]Command:[/bold] [bold cyan]{issue['fix_cmd']}[/bold cyan]")
            console.print()
            apply = typer.confirm("  Apply this fix? [y/N]", default=False)
            if not apply:
                console.print("  [dim]Skipped.[/dim]\n")
                continue

            with console.status("[bold yellow]Applying...[/bold yellow]", spinner="dots"):
                result = run_aws_fix(issue["fix_cmd"])

            if result.success:
                console.print("  [bold green]✓ Applied.[/bold green]\n")
            else:
                console.print(f"  [bold red]✗ Failed:[/bold red] {result.error[:120]}\n")

        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Helpers: ingress
# ---------------------------------------------------------------------------

_INGRESS_PROBLEM_COLOR = {
    "ExternalIPPending":      "bold red",
    "ControllerNotRunning":   "bold red",
    "ControllerNotInstalled": "bold red",
    "NoAddress":              "red",
    "BackendDown":            "red",
    "ServiceNotFound":        "red",
    "TLSSecretMissing":       "yellow",
    "CertManagerError":       "yellow",
    "WrongIngressClass":      "yellow",
    "NoIngressClass":         "yellow",
    "ConfigMapError":         "dark_orange",
    "Healthy":                "green",
    "Unknown":                "dim",
}


def _print_ingress_diagnosis(diagnosis) -> None:
    conf_color  = _CONFIDENCE_COLOR.get(diagnosis.confidence.lower(), "white")
    prob_color  = _INGRESS_PROBLEM_COLOR.get(diagnosis.problem_type.value, "white")
    divider     = "[dim]" + "-" * 58 + "[/dim]"

    lines = [
        f"[dim]Problem    :[/dim] [{prob_color}]{_escape(diagnosis.problem_type.value)}[/{prob_color}]",
        f"[dim]Root Cause :[/dim] {_escape(diagnosis.root_cause)}",
        f"[dim]Confidence :[/dim] [bold {conf_color}]{_escape(diagnosis.confidence.upper())}[/bold {conf_color}]",
        divider,
        "[bold]EXPLANATION[/bold]",
        _escape(diagnosis.explanation),
        divider,
        "[bold]SUGGESTED FIX[/bold]",
        _escape(diagnosis.suggested_fix),
    ]
    if diagnosis.fix_command:
        lines += [
            divider,
            "[bold]COMMAND[/bold]",
            f"[bold cyan]{_escape(diagnosis.fix_command)}[/bold cyan]",
        ]

    console.print(Panel(
        "\n".join(lines),
        title=f"[bold]INGRESS DIAGNOSIS:[/bold] [cyan]{_escape(diagnosis.name)}[/cyan]  "
              f"[dim]{_escape(diagnosis.namespace)}[/dim]",
        border_style="cyan",
        padding=(1, 2),
    ))


# ---------------------------------------------------------------------------
# Command: agent ingress scan
# ---------------------------------------------------------------------------

@ingress_app.command("scan")
def ingress_scan() -> None:
    """
    Full nginx ingress health audit — controllers, external IPs, backends, TLS.

    Runs 7 collectors in parallel, then uses Claude to analyze everything
    and produce a prioritised list of issues with exact fix commands.
    """
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.ingress import IngressSkill

        console.print()
        console.print(Rule("[bold cyan]Nginx Ingress — Full Scan[/bold cyan]"))
        console.print()

        with console.status(
            "[bold cyan]Collecting ingress data in parallel...[/bold cyan]",
            spinner="dots",
        ):
            report = IngressSkill().scan()

        elapsed = f"{report.collection_ms / 1000:.1f}s"
        ctrl_color = "green" if report.controller_ok else "red"
        ctrl_label = "OK" if report.controller_ok else "PROBLEM"

        ip_label = (
            f"[green]{_escape(report.external_ip)}[/green]"
            if report.external_ip
            else "[bold red]<pending>[/bold red]"
        )

        console.print(
            f"  Controller: [{ctrl_color}]{ctrl_label}[/{ctrl_color}]  "
            f"External IP: {ip_label}  "
            f"[dim]{elapsed}[/dim]"
        )
        console.print()

        # Analysis panel
        if report.analysis:
            console.print(Panel(
                _escape(report.analysis),
                title="[bold]AI ANALYSIS[/bold]",
                border_style="cyan",
                padding=(0, 1),
            ))
            console.print()

        # Issue counts
        critical = [i for i in report.issues if i.severity == "critical"]
        warnings = [i for i in report.issues if i.severity == "warning"]
        infos    = [i for i in report.issues if i.severity == "info"]

        console.print(
            f"  Issues:  "
            f"[bold red]{len(critical)} critical[/bold red]   "
            f"[yellow]{len(warnings)} warnings[/yellow]   "
            f"[cyan]{len(infos)} info[/cyan]"
        )
        console.print()

        if not report.issues:
            console.print(Panel(
                "[bold green]Ingress is healthy![/bold green]  No issues found.",
                border_style="green",
            ))
            console.print()
            return

        # Render each issue
        for i, issue in enumerate(report.issues, 1):
            sev_color  = _SEVERITY_COLOR.get(issue.severity, "white")
            sev_icon   = _SEVERITY_ICON.get(issue.severity, "•")
            prob_color = _INGRESS_PROBLEM_COLOR.get(issue.problem_type.value, "white")
            divider    = "[dim]" + "-" * 56 + "[/dim]"

            lines = [
                f"[dim]Type     :[/dim] [{prob_color}]{_escape(issue.problem_type.value)}[/{prob_color}]"
                f"   [dim]Severity:[/dim] [{sev_color}]{issue.severity.upper()}[/{sev_color}]",
                f"[dim]Resource :[/dim] [cyan]{_escape(issue.resource)}[/cyan]"
                + (f"  [dim]{_escape(issue.namespace)}[/dim]" if issue.namespace else ""),
                f"[dim]Problem  :[/dim] {_escape(issue.description)}",
                divider,
                f"[dim]Fix      :[/dim] {_escape(issue.fix)}",
            ]
            if issue.fix_command:
                lines += [
                    divider,
                    f"[dim]Command  :[/dim] [bold cyan]{_escape(issue.fix_command)}[/bold cyan]",
                ]

            console.print(Panel(
                "\n".join(lines),
                title=f"[{sev_color}]{sev_icon} {i}/{len(report.issues)}[/{sev_color}]",
                border_style=sev_color,
                padding=(1, 2),
            ))
            console.print()

        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent ingress diagnose NAME
# ---------------------------------------------------------------------------

@ingress_app.command("diagnose")
def ingress_diagnose(
    name: str = typer.Argument(..., help="Name of the Ingress resource."),
    namespace: str = typer.Option(
        "default", "--namespace", "-n",
        help="Namespace of the Ingress.",
        show_default=True,
    ),
    auto_fix: bool = typer.Option(
        False, "--auto-fix",
        help="Apply the suggested fix without prompting.",
    ),
) -> None:
    """Deep AI diagnosis of a specific Ingress resource — full logs, events, YAML."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.ingress import IngressSkill

        skill = IngressSkill()
        console.print()

        with console.status(
            f"[bold cyan]Diagnosing ingress/{name} in {namespace}...[/bold cyan]",
            spinner="dots",
        ):
            diagnosis = skill.diagnose(name, namespace)

        console.print()
        _print_ingress_diagnosis(diagnosis)
        console.print()

        if not diagnosis.fix_command:
            console.print("[dim]No automated fix command available.[/dim]")
            console.print()
            _cost_footer(get_session_total())
            return

        apply = auto_fix or typer.confirm("Apply this fix?", default=False)
        if not apply:
            console.print("[dim]Fix skipped.[/dim]")
            console.print()
            _cost_footer(get_session_total())
            return

        with console.status("[bold yellow]Applying fix...", spinner="dots"):
            ok = skill.apply_fix(diagnosis.fix_command, name, namespace)

        if ok:
            console.print("[bold green]Fix applied.[/bold green]")
        else:
            console.print("[bold red]Fix command failed.[/bold red]  Check output above.")
        console.print()
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent ingress fix-ip
# ---------------------------------------------------------------------------

@ingress_app.command("fix-ip")
def ingress_fix_ip(
    auto_fix: bool = typer.Option(
        False, "--auto-fix",
        help="Apply the primary fix command without prompting.",
    ),
) -> None:
    """
    Targeted fix for nginx ingress external IP stuck at <pending>.

    Detects cloud vs bare-metal environment and gives the exact fix:
      - Cloud (EKS/GKE/AKS): IAM/subnet/LB quota guidance + command
      - Bare-metal/local:     NodePort patch or MetalLB install command
    """
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.ingress import IngressSkill

        console.print()
        console.print(Rule("[bold cyan]Nginx Ingress — Fix External IP[/bold cyan]"))
        console.print()

        with console.status(
            "[bold cyan]Analyzing LoadBalancer service...[/bold cyan]",
            spinner="dots",
        ):
            result = IngressSkill().fix_external_ip()

        console.print()

        if not result.get("ok"):
            _print_error(result.get("error", "Unknown error"))
            raise typer.Exit(1)

        if result.get("already_assigned"):
            console.print(Panel(
                f"[bold green]External IP already assigned:[/bold green]  "
                f"[cyan]{_escape(result['external_ip'])}[/cyan]",
                border_style="green",
            ))
            console.print()
            return

        env_label = "[cyan]Cloud[/cyan]" if result["is_cloud"] else "[yellow]Bare-metal / Local[/yellow]"
        divider   = "[dim]" + "-" * 58 + "[/dim]"

        lines = [
            f"[dim]Service    :[/dim] [cyan]{_escape(result['service'])}[/cyan]  "
            f"[dim]{_escape(result['namespace'])}[/dim]",
            f"[dim]Environment:[/dim] {env_label}",
            f"[dim]Root Cause :[/dim] {_escape(result['root_cause'])}",
            f"[dim]Confidence :[/dim] [{_CONFIDENCE_COLOR.get(result['confidence'],'white')}]"
            f"{result['confidence'].upper()}[/{_CONFIDENCE_COLOR.get(result['confidence'],'white')}]",
            divider,
            "[bold]EXPLANATION[/bold]",
            _escape(result["explanation"]),
            divider,
            "[bold]FIX[/bold]",
            _escape(result["fix"]),
        ]

        if result.get("fix_command"):
            lines += [
                divider,
                "[bold]PRIMARY COMMAND[/bold]",
                f"[bold cyan]{_escape(result['fix_command'])}[/bold cyan]",
            ]

        if result.get("workaround_command"):
            lines += [
                divider,
                "[bold]WORKAROUND (NodePort fallback)[/bold]",
                f"[bold yellow]{_escape(result['workaround_command'])}[/bold yellow]",
            ]

        console.print(Panel(
            "\n".join(lines),
            title="[bold]EXTERNAL IP DIAGNOSIS[/bold]",
            border_style="cyan",
            padding=(1, 2),
        ))
        console.print()

        if result.get("fix_command"):
            apply = auto_fix or typer.confirm(
                "Apply primary fix?", default=False
            )
            if apply:
                from agent.skills.ingress import IngressSkill
                with console.status("[bold yellow]Applying...", spinner="dots"):
                    ok = IngressSkill().apply_fix(
                        result["fix_command"],
                        result["service"],
                        result["namespace"],
                    )
                if ok:
                    console.print("[bold green]✓ Applied.[/bold green]  "
                                  "Watch the service with:  "
                                  f"kubectl get svc {result['service']} "
                                  f"-n {result['namespace']} -w")
                else:
                    console.print("[bold red]✗ Failed.[/bold red]  Check output above.")
                console.print()

            elif result.get("workaround_command") and typer.confirm(
                "Apply NodePort workaround instead?", default=False
            ):
                from agent.skills.ingress import IngressSkill
                with console.status("[bold yellow]Applying workaround...", spinner="dots"):
                    ok = IngressSkill().apply_fix(
                        result["workaround_command"],
                        result["service"],
                        result["namespace"],
                    )
                console.print(
                    "[bold green]✓ Switched to NodePort.[/bold green]"
                    if ok else "[bold red]✗ Failed.[/bold red]"
                )
                console.print()

        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent ingress heal
# ---------------------------------------------------------------------------

@ingress_app.command("heal")
def ingress_heal() -> None:
    """Scan all ingress issues and interactively apply every available fix."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.ingress import IngressSkill

        console.print()
        console.print(Rule("[bold cyan]Nginx Ingress — Heal[/bold cyan]"))
        console.print()

        with console.status(
            "[bold cyan]Scanning for issues...[/bold cyan]",
            spinner="dots",
        ):
            report = IngressSkill().scan()

        if not report.issues:
            console.print(Panel(
                "[bold green]All ingress resources healthy![/bold green]  Nothing to fix.",
                border_style="green",
            ))
            console.print()
            _cost_footer(get_session_total())
            return

        fixable = [i for i in report.issues if i.fix_command]
        no_fix  = [i for i in report.issues if not i.fix_command]

        console.print(
            f"  Found [bold red]{len(report.issues)}[/bold red] issue(s)  "
            f"([green]{len(fixable)}[/green] have fix commands)\n"
        )

        fixed = skipped = failed = 0
        skill = IngressSkill()

        for i, issue in enumerate(report.issues, 1):
            sev_color  = _SEVERITY_COLOR.get(issue.severity, "white")
            sev_icon   = _SEVERITY_ICON.get(issue.severity, "•")
            prob_color = _INGRESS_PROBLEM_COLOR.get(issue.problem_type.value, "white")

            console.print(Rule(
                f"[dim]{i}/{len(report.issues)}[/dim]  "
                f"[{prob_color}]{_escape(issue.problem_type.value)}[/{prob_color}]  "
                f"[cyan]{_escape(issue.resource)}[/cyan]"
            ))
            console.print(f"  [{sev_color}]{issue.severity.upper()}[/{sev_color}]  "
                          f"{_escape(issue.description)}")
            console.print(f"  [dim]Fix:[/dim] {_escape(issue.fix)}")

            if not issue.fix_command:
                console.print("  [dim]No automated fix command — manual action needed.[/dim]\n")
                skipped += 1
                continue

            console.print(f"  [dim]Command:[/dim] [cyan]{_escape(issue.fix_command)}[/cyan]\n")
            apply = typer.confirm(f"  Apply fix for [{prob_color}]{issue.problem_type.value}[/{prob_color}]?",
                                  default=False)

            if not apply:
                console.print("  [dim]Skipped.[/dim]\n")
                skipped += 1
                continue

            with console.status("[bold yellow]Applying...", spinner="dots"):
                ok = skill.apply_fix(issue.fix_command, issue.resource, issue.namespace)

            if ok:
                console.print("  [bold green]✓ Applied.[/bold green]\n")
                fixed += 1
            else:
                console.print("  [bold red]✗ Failed.[/bold red]\n")
                failed += 1

        console.print(Rule(style="dim"))
        console.print(
            f"\n  [bold]Result:[/bold]  "
            f"[green]{fixed} fixed[/green]  "
            f"[dim]{skipped} skipped[/dim]  "
            f"[red]{failed} failed[/red]\n"
        )
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Helpers: tls
# ---------------------------------------------------------------------------

_TLS_PROBLEM_COLOR = {
    "CertExpired":             "bold red",
    "CertExpiringSoon":        "red",
    "CertManagerNotInstalled": "bold red",
    "CertManagerNotRunning":   "bold red",
    "CertificateNotReady":     "red",
    "IssuerNotReady":          "red",
    "AcmeChallengeFailing":    "red",
    "SecretMissing":           "bold red",
    "SecretInvalid":           "red",
    "HostnameMismatch":        "yellow",
    "RateLimitHit":            "yellow",
    "SelfSigned":              "yellow",
    "WrongIssuerRef":          "yellow",
    "Healthy":                 "green",
    "Unknown":                 "dim",
}


def _print_tls_diagnosis(diagnosis) -> None:
    conf_color = _CONFIDENCE_COLOR.get(diagnosis.confidence.lower(), "white")
    prob_color = _TLS_PROBLEM_COLOR.get(diagnosis.problem_type.value, "white")
    divider    = "[dim]" + "-" * 58 + "[/dim]"

    lines = [
        f"[dim]Problem    :[/dim] [{prob_color}]{_escape(diagnosis.problem_type.value)}[/{prob_color}]",
        f"[dim]Root Cause :[/dim] {_escape(diagnosis.root_cause)}",
        f"[dim]Confidence :[/dim] [bold {conf_color}]{_escape(diagnosis.confidence.upper())}[/bold {conf_color}]",
        divider,
        "[bold]EXPLANATION[/bold]",
        _escape(diagnosis.explanation),
        divider,
        "[bold]SUGGESTED FIX[/bold]",
        _escape(diagnosis.suggested_fix),
    ]
    if diagnosis.fix_command:
        lines += [
            divider,
            "[bold]COMMAND[/bold]",
            f"[bold cyan]{_escape(diagnosis.fix_command)}[/bold cyan]",
        ]
    console.print(Panel(
        "\n".join(lines),
        title=f"[bold]TLS DIAGNOSIS:[/bold] [cyan]{_escape(diagnosis.name)}[/cyan]  "
              f"[dim]{_escape(diagnosis.namespace)}[/dim]",
        border_style="cyan",
        padding=(1, 2),
    ))


# ---------------------------------------------------------------------------
# Command: agent tls scan
# ---------------------------------------------------------------------------

@tls_app.command("scan")
def tls_scan() -> None:
    """
    Auto-discover every domain in the cluster and audit TLS certificates.

    No hardcoded domains — reads all Ingress resources across all namespaces.
    Shows per-domain status cards + summary table.
    """
    try:
        import asyncio
        import json as _json
        from concurrent.futures import ThreadPoolExecutor, wait as _fut_wait
        from rich.progress import Progress, SpinnerColumn, TextColumn

        from agent.core import llm
        from agent.integrations.kubectl import (
            check_ingress_controller,
            get_all_ingresses,
            get_cert_manager_certificates,
            get_tls_secret,
        )

        console.print()

        with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                      console=console, transient=True) as prog:
            t = prog.add_task("Discovering domains...", total=None)
            ingresses = get_all_ingresses()

            prog.update(t, description="Checking TLS certificates...")
            secret_cache: dict = {}
            for _ing in ingresses:
                if _ing.tls_secret and _ing.tls_enabled:
                    _k = f"{_ing.namespace}/{_ing.tls_secret}"
                    if _k not in secret_cache:
                        secret_cache[_k] = get_tls_secret(_ing.tls_secret, _ing.namespace)

            prog.update(t, description="Fetching cert-manager status...")
            cm_certs    = get_cert_manager_certificates()
            cm_by_dom   = {c.domain: c for c in cm_certs}

            prog.update(t, description="Analyzing with Claude...")
            _prompt = "Domains: " + ", ".join(i.domain for i in ingresses) + \
                      "\nGive a 1-sentence cluster TLS summary. JSON: {\"summary\": \"...\"}"
            try:
                _r = asyncio.run(llm.chat(
                    messages=[{"role": "user", "content": _prompt}],
                    system="Kubernetes TLS expert. Return ONLY valid JSON.",
                    json_mode=True,
                    max_tokens=120,
                ))
                _ai_summary = _json.loads(_r.content.strip()).get("summary", "")
            except Exception:
                _ai_summary = ""

        # ── header panel ──────────────────────────────────────────────────────
        console.print(Panel(
            f"[bold]TLS CERTIFICATE SCAN[/bold]\n"
            f"Found [bold cyan]{len(ingresses)}[/bold cyan] domain(s) in cluster"
            + (f"\n\n[dim]{_escape(_ai_summary)}[/dim]" if _ai_summary else ""),
            border_style="cyan",
            padding=(0, 2),
        ))
        console.print()

        if not ingresses:
            console.print("[dim]No Ingress resources found in any namespace.[/dim]")
            return

        summary_rows: list[tuple[str, str, str]] = []

        for ing in ingresses:
            _k   = f"{ing.namespace}/{ing.tls_secret}" if ing.tls_secret else ""
            si   = secret_cache.get(_k) if _k else None
            cm   = cm_by_dom.get(ing.domain)
            div  = "[dim]" + "─" * 48 + "[/dim]"

            # Status
            if not ing.tls_enabled:
                s_icon  = "[dark_orange]⚠[/dark_orange]"
                s_label = "[dark_orange]NO TLS[/dark_orange]"
                s_key   = "No TLS"
                exp_str = "-"
                border  = "dark_orange"
            elif si is None:
                s_icon  = "[bold red]✗[/bold red]"
                s_label = "[bold red]SECRET MISSING[/bold red]"
                s_key   = "Secret Missing"
                exp_str = "-"
                border  = "red"
            elif si.is_expired:
                s_icon  = "[bold red]✗[/bold red]"
                s_label = "[bold red]EXPIRED[/bold red]"
                s_key   = "Expired"
                days    = abs(si.days_until_expiry)
                exp_str = f"{days} days ago  ({si.expiry_date})"
                border  = "red"
            elif si.is_expiring_soon:
                s_icon  = "[yellow]⚠[/yellow]"
                s_label = f"[yellow]EXPIRING SOON ({si.days_until_expiry}d)[/yellow]"
                s_key   = "Expiring Soon"
                exp_str = f"{si.days_until_expiry} days  ({si.expiry_date})"
                border  = "yellow"
            else:
                s_icon  = "[green]✓[/green]"
                s_label = "[green]HEALTHY[/green]"
                s_key   = "Healthy"
                exp_str = f"{si.days_until_expiry} days  ({si.expiry_date})"
                border  = "green"

            issuer = (si.issuer if si and si.issuer else
                      cm.issuer if cm and cm.issuer else "—")
            secret = ing.tls_secret if ing.tls_secret else "—"

            lines = [
                f"[bold cyan]🌐  {ing.domain}[/bold cyan]"
                f"    [dim]NAMESPACE: {ing.namespace}[/dim]",
                div,
                f"[dim]Status  :[/dim]  {s_icon} {s_label}",
                f"[dim]Issuer  :[/dim]  {issuer}",
            ]

            if ing.tls_enabled and si:
                lbl = "Expired " if si.is_expired else "Expires "
                lines.append(f"[dim]{lbl}:[/dim]  {exp_str}")
            elif not ing.tls_enabled:
                lines.append(
                    f"[dim]Fix     :[/dim]  [yellow]agent domain encrypt {ing.domain}"
                    f" --email you@example.com[/yellow]"
                )

            lines.append(f"[dim]Secret  :[/dim]  {secret}")

            if s_key != "Healthy" and ing.tls_enabled:
                lines.append(div)
                lines.append(
                    f"[dim]Fix     :[/dim]  [yellow]agent tls fix {ing.domain}[/yellow]"
                )

            console.print(Panel("\n".join(lines), border_style=border, padding=(0, 1)))
            console.print()

            summary_rows.append((ing.domain, f"{s_icon} {s_key}", exp_str))

        # ── summary table ─────────────────────────────────────────────────────
        tbl = Table(
            title="[bold]SUMMARY[/bold]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        tbl.add_column("Domain",  min_width=26)
        tbl.add_column("Status",  width=22)
        tbl.add_column("Expires", width=22)

        for _dom, _st, _ex in summary_rows:
            tbl.add_row(_dom, _st, _ex)

        console.print(tbl)
        console.print()
        console.print(
            "  [dim]Deep dive:[/dim]  [bold]agent tls check <domain>[/bold]   "
            "[dim]Fix:[/dim]  [bold]agent tls fix <domain>[/bold]"
        )
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent tls diagnose NAME
# ---------------------------------------------------------------------------

@tls_app.command("diagnose")
def tls_diagnose(
    name: str = typer.Argument(..., help="Name of the Certificate, Secret, or Issuer."),
    namespace: str = typer.Option(
        "default", "--namespace", "-n", show_default=True,
    ),
    kind: str = typer.Option(
        "certificate", "--kind", "-k",
        help="Resource kind: certificate | secret | issuer | clusterissuer",
        show_default=True,
    ),
    auto_fix: bool = typer.Option(False, "--auto-fix"),
) -> None:
    """Deep AI diagnosis of a specific Certificate, TLS Secret, or Issuer."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.tls import TLSSkill

        skill = TLSSkill()
        console.print()

        with console.status(
            f"[bold cyan]Diagnosing {kind}/{name} in {namespace}...[/bold cyan]",
            spinner="dots",
        ):
            diagnosis = skill.diagnose(name, namespace, kind)

        console.print()
        _print_tls_diagnosis(diagnosis)
        console.print()

        if not diagnosis.fix_command:
            console.print("[dim]No automated fix command available.[/dim]")
            console.print()
            _cost_footer(get_session_total())
            return

        apply = auto_fix or typer.confirm("Apply this fix?", default=False)
        if not apply:
            console.print("[dim]Fix skipped.[/dim]")
            console.print()
            _cost_footer(get_session_total())
            return

        with console.status("[bold yellow]Applying fix...", spinner="dots"):
            ok = skill.apply_fix(diagnosis.fix_command, name, namespace)

        console.print("[bold green]Fix applied.[/bold green]" if ok
                      else "[bold red]Fix command failed.[/bold red]")
        console.print()
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent tls renew NAME
# ---------------------------------------------------------------------------

@tls_app.command("renew")
def tls_renew(
    name: str = typer.Argument(..., help="Name of the cert-manager Certificate to renew."),
    namespace: str = typer.Option("default", "--namespace", "-n", show_default=True),
) -> None:
    """Trigger immediate renewal of a cert-manager Certificate resource."""
    try:
        from agent.skills.tls import TLSSkill

        console.print()
        with console.status(
            f"[bold cyan]Triggering renewal of {name} in {namespace}...[/bold cyan]",
            spinner="dots",
        ):
            result = TLSSkill().renew(name, namespace)

        console.print()
        if result.get("ok"):
            console.print(Panel(
                f"[bold green]Renewal triggered[/bold green] via [cyan]{result['method']}[/cyan]\n"
                + (f"[dim]{_escape(result.get('output','')[:200])}[/dim]" if result.get("output") else ""),
                border_style="green",
            ))
            console.print(
                f"  Watch: [bold]kubectl get certificate {name} -n {namespace} -w[/bold]"
            )
        else:
            console.print(Panel(
                f"[bold red]Renewal failed:[/bold red] {_escape(result.get('error',''))}\n\n"
                + _escape(result.get("hint", "")),
                border_style="red",
            ))
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent tls heal
# ---------------------------------------------------------------------------

@tls_app.command("heal")
def tls_heal() -> None:
    """Scan all TLS issues and interactively apply every available fix."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.tls import TLSSkill

        console.print()
        console.print(Rule("[bold cyan]TLS / Certificate — Heal[/bold cyan]"))
        console.print()

        with console.status("[bold cyan]Scanning for TLS issues...[/bold cyan]", spinner="dots"):
            report = TLSSkill().scan()

        if not report.issues:
            console.print(Panel(
                "[bold green]All TLS resources healthy![/bold green]  Nothing to fix.",
                border_style="green",
            ))
            console.print()
            _cost_footer(get_session_total())
            return

        fixable = [i for i in report.issues if i.fix_command]
        console.print(
            f"  Found [bold red]{len(report.issues)}[/bold red] issue(s)  "
            f"([green]{len(fixable)}[/green] have fix commands)\n"
        )

        fixed = skipped = failed = 0
        skill = TLSSkill()

        for i, issue in enumerate(report.issues, 1):
            sev_color  = _SEVERITY_COLOR.get(issue.severity, "white")
            prob_color = _TLS_PROBLEM_COLOR.get(issue.problem_type.value, "white")

            console.print(Rule(
                f"[dim]{i}/{len(report.issues)}[/dim]  "
                f"[{prob_color}]{_escape(issue.problem_type.value)}[/{prob_color}]  "
                f"[cyan]{_escape(issue.resource)}[/cyan]"
            ))
            console.print(f"  [{sev_color}]{issue.severity.upper()}[/{sev_color}]  "
                          f"{_escape(issue.description)}")
            console.print(f"  [dim]Fix:[/dim] {_escape(issue.fix)}\n")

            # ── Special case: cert-manager not installed ──────────────────
            if issue.problem_type.value == "CertManagerNotInstalled":
                import shutil as _shutil
                method = "helm" if _shutil.which("helm") else "kubectl apply (manifest)"
                console.print(
                    f"  cert-manager is not installed. Will install via [cyan]{method}[/cyan].\n"
                    "  Steps that will run:\n"
                    + ("  1. helm repo add jetstack\n  2. helm repo update\n"
                       "  3. helm install cert-manager\n  4. wait for pods ready\n"
                       if _shutil.which("helm") else
                       "  1. kubectl apply -f cert-manager.yaml (official manifest)\n"
                       "  2. wait for pods ready\n")
                )
                if not typer.confirm("  Proceed with cert-manager installation?", default=False):
                    console.print("  [dim]Skipped.[/dim]\n")
                    skipped += 1
                    continue

                with console.status("[bold yellow]Installing cert-manager...[/bold yellow]",
                                    spinner="dots"):
                    result = skill.install_cert_manager()

                console.print()
                for step in result["steps"]:
                    icon = "[green]✓[/green]" if step["ok"] else "[red]✗[/red]"
                    console.print(f"  {icon}  {_escape(step['label'])}")
                    if not step["ok"] and step.get("output"):
                        console.print(f"     [dim]{_escape(step['output'][:200])}[/dim]")
                console.print()

                if result["ok"]:
                    console.print("  [bold green]cert-manager installed successfully.[/bold green]\n")
                    fixed += 1
                else:
                    console.print("  [bold red]Installation failed.[/bold red]  "
                                  "Check output above.\n")
                    failed += 1
                continue

            # ── Normal fix path ───────────────────────────────────────────
            if not issue.fix_command:
                console.print("  [dim]No automated fix — manual action needed.[/dim]\n")
                skipped += 1
                continue

            console.print(f"  [dim]Command:[/dim] [cyan]{_escape(issue.fix_command)}[/cyan]\n")
            apply = typer.confirm(
                f"  Apply fix for [{prob_color}]{issue.problem_type.value}[/{prob_color}]?",
                default=False,
            )

            if not apply:
                console.print("  [dim]Skipped.[/dim]\n")
                skipped += 1
                continue

            with console.status("[bold yellow]Applying...", spinner="dots"):
                ok = skill.apply_fix(issue.fix_command, issue.resource, issue.namespace)

            console.print("  [bold green]✓ Applied.[/bold green]\n" if ok
                          else "  [bold red]✗ Failed.[/bold red]\n")
            if ok:
                fixed += 1
            else:
                failed += 1

        console.print(Rule(style="dim"))
        console.print(
            f"\n  [bold]Result:[/bold]  "
            f"[green]{fixed} fixed[/green]  "
            f"[dim]{skipped} skipped[/dim]  "
            f"[red]{failed} failed[/red]\n"
        )
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent tls monitor
# ---------------------------------------------------------------------------

@tls_app.command("monitor")
def tls_monitor(
    fix: bool = typer.Option(
        False, "--fix",
        help="After showing issues, ask permission and apply each fix.",
    ),
) -> None:
    """
    Auto-discover every domain in the cluster and audit TLS certificate health.

    Reads all Ingress resources across every namespace — no hardcoded domains.
    Shows a table of every domain with certificate status, then lists problems
    with Claude's diagnosis.  With --fix, asks permission before applying each fix.
    """
    try:
        from agent.skills.tls_monitor import TLSMonitorSkill

        console.print()
        result = TLSMonitorSkill().scan()

        if not fix:
            return

        diagnoses = result.get("diagnoses", [])
        fixable   = [d for d in diagnoses
                     if d.fix_type != "none" or d.fix_command]

        if not fixable:
            console.print("[dim]No automated fixes available.[/dim]\n")
            return

        console.print(Rule("[bold yellow]Fix Mode[/bold yellow]"))
        console.print()

        from agent.skills.tls_monitor import TLSMonitorSkill as _Skill
        skill = _Skill()
        fixed = skipped = failed = 0

        for d in fixable:
            from agent.skills.tls_monitor import _SEVERITY_MAP
            _, color = _SEVERITY_MAP.get(d.problem_type, ("info", "dim"))
            console.print(
                f"  [{color}]{d.problem_type}[/{color}]  "
                f"[bold cyan]{d.domain}[/bold cyan]  "
                f"[dim]{d.ingress.namespace}[/dim]"
            )
            console.print(f"  [dim]Fix:[/dim] {d.suggested_fix}")
            if d.fix_command:
                console.print(f"  [dim]Cmd:[/dim] [cyan]{d.fix_command}[/cyan]")
            console.print()

            if not typer.confirm(f"  Apply fix for {d.domain}?", default=False):
                console.print("  [dim]Skipped.[/dim]\n")
                skipped += 1
                continue

            r = skill.fix(d, confirmed=True)
            if r["ok"]:
                fixed += 1
            else:
                failed += 1

        console.print(Rule(style="dim"))
        console.print(
            f"\n  [bold]Result:[/bold]  "
            f"[green]{fixed} fixed[/green]  "
            f"[dim]{skipped} skipped[/dim]  "
            f"[red]{failed} failed[/red]\n"
        )

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Commands: agent tls check / fix / history
# ---------------------------------------------------------------------------

def _domain_tls_deep_check(domain: str, namespace_hint: str | None) -> dict:
    """Collect full TLS data for one domain and run Claude diagnosis."""
    import asyncio
    import json as _json

    from agent.core import llm
    from agent.integrations.kubectl import (
        get_all_ingresses,
        get_cert_manager_certificates,
        get_cert_manager_issuers,
        get_tls_secret,
        run_tls_fix,
    )

    all_ings = get_all_ingresses()
    candidates = [i for i in all_ings if i.domain == domain]
    if namespace_hint:
        candidates = [i for i in candidates if i.namespace == namespace_hint] or candidates
    if not candidates:
        return {"found": False, "error": f"No Ingress found for domain '{domain}'"}
    ing = candidates[0]

    si  = get_tls_secret(ing.tls_secret, ing.namespace) if ing.tls_secret and ing.tls_enabled else None
    cm_certs = get_cert_manager_certificates()
    cm  = next((c for c in cm_certs if c.domain == domain), None)
    issuers = get_cert_manager_issuers()

    prompt = f"""=== TLS DEEP CHECK ===
Domain:     {domain}
Namespace:  {ing.namespace}
Ingress:    {ing.name}
Backend:    {ing.backend_service or 'unknown'}
Address:    {ing.address or '<pending>'}
TLS Enabled: {ing.tls_enabled}
TLS Secret: {ing.tls_secret or 'none'}
"""
    if si:
        prompt += f"""
Certificate:
  type:       {si.cert_type}
  issuer:     {si.issuer}
  cert_domain: {si.domain}
  expiry:     {si.expiry_date}
  days_left:  {si.days_until_expiry}
  expired:    {si.is_expired}
  expiring_soon: {si.is_expiring_soon}
"""
    else:
        prompt += "\nTLS Secret: NOT FOUND IN CLUSTER\n"

    if cm:
        prompt += f"""
cert-manager Certificate:
  ready:   {cm.ready}
  status:  {cm.status}
  message: {cm.message}
  expiry:  {cm.expiry}
  issuer:  {cm.issuer}
"""

    prompt += f"\nAvailable ClusterIssuers: {issuers or ['none']}\n"
    prompt += """
Return JSON:
{
  "problem_type": "CertExpired|CertExpiringSoon|SecretMissing|CertificateNotReady|AcmeChallengeFailing|NoTLSConfigured|Healthy|Unknown",
  "root_cause": "...",
  "ai_analysis": "...",
  "suggested_fix": "...",
  "fix_command": "kubectl ... or null",
  "fix_type": "restart_cert|delete_secret|apply_manifest|none",
  "confidence": "high|medium|low"
}"""

    try:
        raw  = asyncio.run(llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system="Kubernetes TLS expert. Return ONLY valid JSON, no markdown.",
            json_mode=True,
            max_tokens=600,
        ))
        diag = _json.loads(raw.content.strip())
    except Exception:
        diag = {
            "problem_type": "Unknown",
            "root_cause":   "LLM unavailable",
            "ai_analysis":  "",
            "suggested_fix": "Check cluster manually",
            "fix_command":  None,
            "fix_type":     "none",
            "confidence":   "low",
        }

    return {
        "found":       True,
        "domain":      domain,
        "ingress":     ing,
        "secret_info": si,
        "cm_cert":     cm,
        "diagnosis":   diag,
    }


def _render_tls_check_panel(data: dict) -> None:
    """Render the full TLS CHECK output panel."""
    ing  = data["ingress"]
    si   = data["secret_info"]
    cm   = data["cm_cert"]
    diag = data["diagnosis"]
    domain = data["domain"]

    prob    = diag.get("problem_type", "Unknown")
    pcolor  = _TLS_PROBLEM_COLOR.get(prob, "white")
    divider = "[bold cyan]" + "═" * 50 + "[/bold cyan]"

    # Status line
    if prob == "Healthy":
        status_line = "[bold green]✓ HEALTHY[/bold green]"
    elif prob in ("CertExpired",):
        status_line = "[bold red]✗ CERT EXPIRED[/bold red]"
    elif prob == "CertExpiringSoon":
        status_line = f"[yellow]⚠ EXPIRING SOON ({si.days_until_expiry}d)[/yellow]" if si else "[yellow]⚠ EXPIRING SOON[/yellow]"
    elif prob == "SecretMissing":
        status_line = "[bold red]✗ SECRET MISSING[/bold red]"
    elif prob == "NoTLSConfigured":
        status_line = "[dark_orange]⚠ NO TLS CONFIGURED[/dark_orange]"
    else:
        status_line = f"[{pcolor}]{_escape(prob)}[/{pcolor}]"

    issuer_str = (si.issuer if si and si.issuer else
                  cm.issuer if cm and cm.issuer else "—")
    secret_str = f"{ing.tls_secret}  [dim](namespace: {ing.namespace})[/dim]" if ing.tls_secret else "—"

    lines: list[str] = [
        f"[dim]Status      :[/dim]  {status_line}",
        f"[dim]Issuer      :[/dim]  {issuer_str}",
    ]

    if si:
        if si.is_expired:
            exp_lbl = f"[bold red]{si.expiry_date}  ({abs(si.days_until_expiry)} days ago)[/bold red]"
        elif si.is_expiring_soon:
            exp_lbl = f"[yellow]{si.expiry_date}  ({si.days_until_expiry} days)[/yellow]"
        else:
            exp_lbl = f"[green]{si.expiry_date}  ({si.days_until_expiry} days)[/green]"
        lines.append(f"[dim]Expires     :[/dim]  {exp_lbl}")

    lines += [
        f"[dim]Secret      :[/dim]  {secret_str}",
        f"[dim]Ingress     :[/dim]  {ing.name}",
    ]

    if cm:
        cm_ready = "[green]Ready[/green]" if cm.ready else "[red]NOT Ready[/red]"
        lines += [
            f"[dim]cert-manager:[/dim]  {cm_ready}  {_escape(cm.message[:60]) if cm.message else ''}",
        ]

    lines.append(divider)
    if diag.get("root_cause"):
        lines += ["[bold]ROOT CAUSE[/bold]", _escape(diag["root_cause"]), divider]
    if diag.get("ai_analysis"):
        lines += ["[bold]AI ANALYSIS[/bold]", _escape(diag["ai_analysis"]), divider]

    lines += [
        "[bold]SUGGESTED FIX[/bold]",
        _escape(diag.get("suggested_fix", "—")),
    ]

    if diag.get("fix_command"):
        lines += [
            divider,
            "[bold]COMMAND[/bold]",
            f"[bold cyan]{_escape(diag['fix_command'])}[/bold cyan]",
        ]

    conf   = diag.get("confidence", "low")
    ccolor = _CONFIDENCE_COLOR.get(conf, "white")
    console.print(Panel(
        "\n".join(lines),
        title=f"[bold]TLS CHECK:[/bold]  [bold cyan]{_escape(domain)}[/bold cyan]"
              f"  [dim]confidence=[/dim][{ccolor}]{conf}[/{ccolor}]",
        border_style=pcolor,
        padding=(1, 2),
    ))


@tls_app.command("check")
def tls_check(
    domain: str = typer.Argument(..., help="Domain to check, e.g. www.infragpt.online"),
    namespace: Optional[str] = typer.Option(
        None, "--namespace", "-n",
        help="Namespace hint (auto-detected if omitted).",
    ),
) -> None:
    """
    Deep TLS check for one specific domain.

    Auto-detects the domain's Ingress, TLS secret, and cert-manager
    Certificate, then runs Claude for a full root-cause diagnosis.
    Asks before applying any fix — default is always N.
    """
    try:
        console.print()
        console.print(Rule(f"[bold cyan]TLS CHECK — {_escape(domain)}[/bold cyan]"))
        console.print()

        with console.status(
            f"[bold cyan]Analysing TLS for {_escape(domain)}...[/bold cyan]",
            spinner="dots",
        ):
            data = _domain_tls_deep_check(domain, namespace)

        console.print()

        if not data["found"]:
            _print_error(data["error"])
            raise typer.Exit(1)

        _render_tls_check_panel(data)
        console.print()

        diag = data["diagnosis"]
        if not diag.get("fix_command"):
            console.print("[dim]No automated fix command available.[/dim]")
            console.print()
            return

        console.print(
            f"  [bold]Command that will run:[/bold]  "
            f"[bold cyan]{_escape(diag['fix_command'])}[/bold cyan]"
        )
        console.print()

        apply = typer.confirm("Apply this fix? [y/N]", default=False)
        if not apply:
            console.print("[dim]Fix skipped.[/dim]")
            console.print()
            return

        _apply_tls_domain_fix(data, console)

        # Save to memory
        try:
            from agent.memory.retrieval import remember
            remember(
                content=f"TLS check: {domain}  problem={diag.get('problem_type','?')}  "
                        f"fix_applied={diag.get('fix_command','none')}",
                source="tls-check",
                metadata={"domain": domain, "problem": diag.get("problem_type", "?"),
                          "fixed": True, "namespace": data["ingress"].namespace},
            )
        except Exception:
            pass

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@tls_app.command("fix")
def tls_fix(
    domain: str = typer.Argument(..., help="Domain to fix, e.g. www.infragpt.online"),
    namespace: Optional[str] = typer.Option(
        None, "--namespace", "-n",
        help="Namespace hint (auto-detected if omitted).",
    ),
) -> None:
    """
    Diagnose and fix TLS for one domain — always asks before applying.

    Same full diagnosis as 'tls check' but goes directly to the fix prompt.
    The fix command is shown before asking. Default answer is always N.
    """
    try:
        console.print()
        console.print(Rule(f"[bold cyan]TLS FIX — {_escape(domain)}[/bold cyan]"))
        console.print()

        with console.status(
            f"[bold cyan]Diagnosing TLS for {_escape(domain)}...[/bold cyan]",
            spinner="dots",
        ):
            data = _domain_tls_deep_check(domain, namespace)

        console.print()

        if not data["found"]:
            _print_error(data["error"])
            raise typer.Exit(1)

        _render_tls_check_panel(data)
        console.print()

        diag = data["diagnosis"]

        if diag.get("problem_type") == "Healthy":
            console.print(Panel(
                f"[bold green]✓  {domain}  is already healthy — no fix needed.[/bold green]",
                border_style="green",
            ))
            console.print()
            return

        if not diag.get("fix_command"):
            console.print(Panel(
                "[yellow]No automated fix command available for this issue.\n\n"
                "[dim]Manual investigation needed.[/dim]",
                border_style="yellow",
            ))
            console.print()
            return

        console.print(
            f"  [bold]Command that will run:[/bold]  "
            f"[bold cyan]{_escape(diag['fix_command'])}[/bold cyan]\n"
        )
        apply = typer.confirm("Apply this fix? [y/N]", default=False)
        if not apply:
            console.print("[dim]Fix skipped.[/dim]")
            console.print()
            return

        _apply_tls_domain_fix(data, console)

        # Save to memory
        try:
            from agent.memory.retrieval import remember
            remember(
                content=f"TLS fix applied: {domain}  problem={diag.get('problem_type','?')}  "
                        f"cmd={diag.get('fix_command','')}",
                source="tls-fix",
                metadata={"domain": domain, "problem": diag.get("problem_type", "?"),
                          "fixed": True, "namespace": data["ingress"].namespace},
            )
        except Exception:
            pass

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


def _apply_tls_domain_fix(data: dict, con) -> None:
    """Apply the diagnosed fix, wait, and verify."""
    import time as _time
    diag    = data["diagnosis"]
    ing     = data["ingress"]
    fix_cmd = diag.get("fix_command", "")
    fix_type = diag.get("fix_type", "none")

    from agent.integrations.kubectl import apply_fix, run_tls_fix

    if fix_type in ("restart_cert", "delete_secret", "annotate_ingress", "apply_manifest"):
        params = {"namespace": ing.namespace, "ingress": ing.name, "name": ing.tls_secret or ""}
        if fix_type == "delete_secret":
            params["name"] = ing.tls_secret or ""
        with con.status("[bold yellow]Applying fix...[/bold yellow]", spinner="dots"):
            result = run_tls_fix(fix_type, params)
    else:
        with con.status("[bold yellow]Applying fix...[/bold yellow]", spinner="dots"):
            result = apply_fix(fix_cmd)

    if not result.success:
        con.print(f"  [bold red]✗ Fix command failed:[/bold red] {_escape(result.error[:120])}")
        return

    con.print("  [bold green]✓ Fix applied.[/bold green]")
    con.print("  [dim]Waiting 30 s for cert-manager to react...[/dim]")

    for remaining in range(30, 0, -5):
        con.print(f"  [dim]{remaining}s...[/dim]", end="\r")
        _time.sleep(5)
    con.print(" " * 20, end="\r")

    con.print("  [cyan]Checking certificate status...[/cyan]")
    with con.status("[bold cyan]Verifying...[/bold cyan]", spinner="dots"):
        from agent.integrations.kubectl import get_cert_manager_certificates, get_tls_secret
        cm_certs  = get_cert_manager_certificates()
        cm_after  = next((c for c in cm_certs if c.domain == data["domain"]), None)
        si_after  = get_tls_secret(ing.tls_secret, ing.namespace) if ing.tls_secret else None

    if cm_after and cm_after.ready:
        con.print(
            f"\n  [bold green]✓  Certificate is Ready![/bold green]  "
            f"expires {cm_after.expiry}"
        )
    elif si_after and not si_after.is_expired:
        con.print(
            f"\n  [bold green]✓  TLS secret is valid[/bold green]  "
            f"expires {si_after.expiry_date}"
        )
    else:
        con.print(
            "\n  [yellow]Certificate not ready yet — ACME can take 1-3 min.[/yellow]\n"
            f"  Watch: [bold]kubectl get certificate -n {ing.namespace} -w[/bold]\n"
            "  Debug: [bold]kubectl get challenges -A[/bold]"
        )
    con.print()


@tls_app.command("history")
def tls_history(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of past incidents.", show_default=True),
) -> None:
    """Show past TLS incidents from memory (tls check / tls fix history)."""
    try:
        from agent.memory.store import get_by_source

        checks = get_by_source("tls-check")[:limit]
        fixes  = get_by_source("tls-fix")[:limit]

        all_events = sorted(checks + fixes,
                            key=lambda m: m.created_at, reverse=True)[:limit]

        console.print()

        if not all_events:
            console.print("[dim]No past TLS incidents in memory.[/dim]")
            console.print("  [dim]Run[/dim] agent tls check <domain> [dim]to start building history.[/dim]")
            console.print()
            return

        tbl = Table(
            title=f"TLS Incident History  [dim](latest {len(all_events)})[/dim]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        tbl.add_column("Domain",     min_width=26)
        tbl.add_column("Problem",    width=22)
        tbl.add_column("Namespace",  width=16)
        tbl.add_column("Action",     width=10, justify="center")
        tbl.add_column("Timestamp",  width=17)

        for mem in all_events:
            meta     = mem.metadata
            dom      = meta.get("domain", "?")
            prob     = meta.get("problem", "?")
            ns       = meta.get("namespace", "?")
            action   = "[green]fixed[/green]" if mem.source == "tls-fix" else "[cyan]checked[/cyan]"
            ts       = mem.created_at.strftime("%Y-%m-%d %H:%M")
            pcolor   = _TLS_PROBLEM_COLOR.get(prob, "white")
            tbl.add_row(dom, f"[{pcolor}]{prob}[/{pcolor}]", ns, action, ts)

        console.print(tbl)
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Commands: agent domain
# ---------------------------------------------------------------------------

@domain_app.command("scan")
def domain_scan(
    domain: str = typer.Argument(..., help="Domain to check, e.g. app.example.com"),
) -> None:
    """Check TLS/HTTPS status for a domain — certs, ingress, secrets."""
    try:
        from agent.skills.domain import DomainSkill
        console.print()
        DomainSkill().scan(domain)
        console.print()
    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@domain_app.command("encrypt")
def domain_encrypt(
    domain: str = typer.Argument(..., help="Domain to encrypt, e.g. app.example.com"),
    email: str = typer.Option(
        ..., "--email", "-e",
        help="Email for Let's Encrypt account (receives expiry notices).",
    ),
    namespace: str = typer.Option(
        "default", "--namespace", "-n",
        help="Kubernetes namespace where the Ingress / Certificate lives.",
        show_default=True,
    ),
    ingress: Optional[str] = typer.Option(
        None, "--ingress", "-i",
        help="Ingress resource name to patch. Auto-detected if omitted.",
    ),
    staging: bool = typer.Option(
        False, "--staging",
        help="Use Let's Encrypt staging (rate-limit safe for testing).",
    ),
) -> None:
    """
    Provision a free Let's Encrypt HTTPS certificate for a domain.

    Steps this command performs:
      1. Verify cert-manager is running (install hint if not)
      2. Create ClusterIssuer letsencrypt-prod (or staging)
      3. Find the Ingress for your domain, patch it with TLS + annotation
         (or create a standalone Certificate resource if no Ingress found)
      4. Wait up to 2 min for the ACME HTTP-01 challenge to complete

    After success, the cert appears in  agent tls scan  automatically.
    """
    try:
        from agent.skills.domain import DomainSkill

        console.print()

        if staging:
            console.print(
                "  [yellow]Staging mode — certificate will NOT be trusted by browsers.[/yellow]\n"
                "  Use this to test the flow without hitting rate limits.\n"
            )

        result = DomainSkill().encrypt(
            domain    = domain,
            email     = email,
            namespace = namespace,
            ingress   = ingress,
            staging   = staging,
        )

        console.print()
        if not result["ok"]:
            err = result.get("error", "")
            if err:
                _print_error(err)
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@domain_app.command("status")
def domain_status(
    domain: str = typer.Argument(..., help="Domain to check."),
    namespace: str = typer.Option("default", "--namespace", "-n", show_default=True),
) -> None:
    """
    Quick cert status — shows Ready/NotReady and expiry date.

    Useful after running encrypt to see if the cert issued successfully.
    """
    try:
        from rich.table import Table

        console.print()
        r = run_kubectl_cmd(["get", "certificates", "-n", namespace, "-o", "json"])
        if not r:
            _print_error("kubectl not available")
            raise typer.Exit(1)

        from agent.integrations.kubectl import run_kubectl
        cert_r = run_kubectl(["get", "certificates", "-n", namespace, "-o", "json"])
        if not cert_r.success:
            _print_error(cert_r.error)
            raise typer.Exit(1)

        import json as _json
        certs = _json.loads(cert_r.output).get("items", [])
        matching = [
            c for c in certs
            if domain in c.get("spec", {}).get("dnsNames", [])
        ]

        if not matching:
            console.print(
                f"  [yellow]No Certificate resource found for {domain} in {namespace}.[/yellow]\n"
                f"  Run:  [bold]agent domain encrypt {domain} --email you@example.com -n {namespace}[/bold]"
            )
            console.print()
            return

        tbl = Table(show_header=True, header_style="bold cyan")
        tbl.add_column("Certificate", width=28)
        tbl.add_column("Ready",       justify="center", width=8)
        tbl.add_column("Expiry",      width=12)
        tbl.add_column("Secret",      width=24)
        tbl.add_column("Issuer",      width=20)

        for cert in matching:
            meta   = cert["metadata"]
            spec   = cert.get("spec", {})
            status = cert.get("status", {})
            cond_map = {c["type"]: c["status"] for c in status.get("conditions", [])}
            ready    = cond_map.get("Ready") == "True"
            expiry   = (status.get("notAfter") or "")[:10] or "pending"
            color    = "green" if ready else "yellow"
            tbl.add_row(
                meta["name"],
                f"[{color}]{'YES' if ready else 'NO'}[/{color}]",
                expiry,
                spec.get("secretName", "?"),
                spec.get("issuerRef", {}).get("name", "?"),
            )

        console.print(tbl)
        console.print()
        console.print(
            "  [dim]Full cert audit:[/dim]  [bold]agent tls scan[/bold]\n"
            "  [dim]Watch live:   [/dim]  "
            f"[bold]kubectl get certificate -n {namespace} -w[/bold]"
        )
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


def run_kubectl_cmd(args: list[str]):
    from agent.integrations.kubectl import run_kubectl
    return run_kubectl(args)


# ---------------------------------------------------------------------------
# Command: agent domain live
# ---------------------------------------------------------------------------

_LAYER_LABEL = {
    "lb":          "LoadBalancer",
    "dns":         "DNS Resolution",
    "tls_section": "TLS Section",
    "tls_cert":    "TLS Certificate",
    "tls_secret":  "TLS Secret",
    "http":        "HTTP",
    "https":       "HTTPS",
    "backend":     "Backend Pods",
}


@domain_app.command("live")
def domain_live(
    domain: Optional[str] = typer.Argument(
        None,
        help="Domain to check (e.g. www.infragpt.online). "
             "Omit to check every domain in the cluster.",
    ),
    namespace: Optional[str] = typer.Option(
        None, "--namespace", "-n",
        help="Namespace hint (auto-detected from Ingress if omitted).",
    ),
    fix: bool = typer.Option(
        False, "--fix",
        help="Ask permission and apply the suggested fix.",
    ),
) -> None:
    """
    End-to-end domain health check: DNS → LB → TLS → HTTP → backend pods.

    Finds every cluster domain automatically — no hardcoded names.
    Detects the most common root cause: Ingress has cert-manager annotation
    but no spec.tls section (cert issued in wrong namespace → HTTPS broken).

    --fix  asks before applying each fix. Default is always N.
    """
    try:
        from agent.skills.domain import DomainSkill

        console.print()
        console.print(Rule(
            "[bold cyan]Domain Live Check[/bold cyan]"
            + (f"  [dim]{_escape(domain)}[/dim]" if domain else "  all domains")
        ))
        console.print()

        skill = DomainSkill()

        with console.status("[bold cyan]Checking all layers...[/bold cyan]",
                            spinner="dots"):
            results = skill.live(domain, namespace)

        for data in results:
            if not data.get("found", True):
                console.print(
                    f"  [red]✗[/red]  {_escape(data['domain'])}  "
                    f"[dim]{_escape(data.get('error', 'not found'))}[/dim]"
                )
                continue

            _render_domain_live_panel(data, console)

            if not fix:
                continue

            _apply_domain_live_fix(data, skill, console)

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Helpers: domain live
# ---------------------------------------------------------------------------

def _render_domain_live_panel(data: dict, con) -> None:
    """Render one domain's 8-layer health panel."""
    dom    = data["domain"]
    chks   = data["checks"]
    diag   = data["diagnosis"]
    ing    = data["ingress"]

    status  = diag.get("overall_status", "unknown")
    s_color = {"live": "green", "degraded": "yellow",
               "down": "bold red"}.get(status, "white")
    conf    = diag.get("confidence", "high")
    ccolor  = _CONFIDENCE_COLOR.get(conf, "white")
    divider = "[dim]" + "─" * 52 + "[/dim]"

    layer_lines: list[str] = []
    for key, label in _LAYER_LABEL.items():
        chk = chks.get(key, {})
        if not isinstance(chk, dict):
            continue
        ok = chk.get("ok")

        if ok is True:
            icon   = "[green]✓[/green]"
            detail = ""
            if key == "lb":
                detail = f"  [dim]{chk.get('address', '?')}[/dim]"
            elif key == "dns":
                detail = f"  [dim]{', '.join((chk.get('resolved_ips') or [])[:2])}[/dim]"
                if chk.get("warning"):
                    icon   = "[yellow]⚠[/yellow]"
                    detail += f"  [yellow]{_escape(chk['warning'][:60])}[/yellow]"
            elif key in ("tls_cert", "tls_secret"):
                detail = (f"  [dim]expires {chk.get('expiry', '?')}  "
                          f"{chk.get('days_left', '?')} days[/dim]")
            elif key in ("http", "https"):
                detail = f"  [dim]HTTP {chk.get('status_code', '?')}[/dim]"
        elif ok is False:
            icon   = "[bold red]✗[/bold red]"
            err    = chk.get("error") or chk.get("hint") or ""
            detail = f"  [red]{_escape(str(err)[:72])}[/red]"
        else:
            icon   = "[dim]—[/dim]"
            detail = f"  [dim]{_escape(str(chk.get('note', 'n/a')))}[/dim]"

        n = list(_LAYER_LABEL).index(key) + 1
        layer_lines.append(
            f"  {icon}  [dim]{n}[/dim]  [bold]{label:<18}[/bold]{detail}"
        )

    body = "\n".join(layer_lines) + f"\n{divider}"

    if diag.get("root_cause"):
        body += f"\n[bold]Root cause:[/bold]  {_escape(diag['root_cause'])}"
    if diag.get("ai_analysis"):
        body += f"\n[bold]Analysis  :[/bold]  {_escape(diag['ai_analysis'])}"

    if diag.get("fix_command"):
        body += (
            f"\n{divider}"
            f"\n[bold]Fix       :[/bold]  {_escape(diag.get('suggested_fix',''))}"
            f"\n[bold]Command   :[/bold]  [bold cyan]{_escape(diag['fix_command'])}[/bold cyan]"
        )
    elif diag.get("suggested_fix") and status != "live":
        body += f"\n{divider}\n[bold]Fix       :[/bold]  {_escape(diag['suggested_fix'])}"

    next_steps = diag.get("next_steps", [])
    if next_steps:
        body += f"\n{divider}\n[bold]Next steps:[/bold]\n"
        body += "\n".join(f"  [dim]{i+1}.[/dim]  {_escape(s)}"
                          for i, s in enumerate(next_steps[:3]))

    con.print(Panel(
        body,
        title=(
            f"[{s_color}]{status.upper()}[/{s_color}]  "
            f"[bold cyan]{_escape(dom)}[/bold cyan]  "
            f"[dim]{ing.namespace}[/dim]  "
            f"[dim]confidence=[/dim][{ccolor}]{conf}[/{ccolor}]"
        ),
        border_style=s_color.replace("bold ", ""),
        padding=(0, 1),
    ))
    con.print()


def _apply_domain_live_fix(data: dict, skill, con) -> None:
    """Ask permission then apply the diagnosed fix for one domain."""
    diag     = data["diagnosis"]
    ing      = data["ingress"]
    dom      = data["domain"]
    fix_type = diag.get("fix_type", "none")
    fix_cmd  = diag.get("fix_command")

    if diag.get("overall_status") == "live":
        con.print("  [green]✓ Already live — no fix needed.[/green]\n")
        return

    if not fix_cmd:
        con.print("  [dim]No automated fix available for this issue.[/dim]\n")
        return

    con.print(
        f"  [bold]Command:[/bold]  "
        f"[bold cyan]{_escape(fix_cmd)}[/bold cyan]\n"
    )
    if not typer.confirm("  Apply this fix? [y/N]", default=False):
        con.print("  [dim]Skipped.[/dim]\n")
        return

    if fix_type == "patch_ingress_tls":
        secret = (
            (data["checks"].get("tls_section") or {}).get("secret_name")
            or f"{dom.replace('.', '-')}-tls"
        )
        with con.status("[bold yellow]Patching Ingress spec.tls...[/bold yellow]",
                        spinner="dots"):
            r = skill.live_fix_ingress_tls(ing.name, ing.namespace, dom, secret)

        if not r["ok"]:
            con.print(f"  [bold red]✗ Patch failed:[/bold red] {_escape(r['error'][:100])}")
            return

        con.print("  [bold green]✓ Ingress patched.[/bold green]")
        con.print(
            "  [cyan]Waiting for cert-manager to issue certificate...[/cyan]  "
            "[dim](up to 90 s)[/dim]"
        )
        with con.status("[bold cyan]Watching certificate...[/bold cyan]",
                        spinner="dots"):
            cert_r = skill.live_wait_cert(dom, ing.namespace, timeout=90)

        if cert_r["ok"]:
            con.print(
                f"\n  [bold green]✓ Certificate Ready![/bold green]  "
                f"expires {cert_r['expiry']}\n"
                f"  [green]https://{dom}[/green] is now live.\n"
                "  Confirm:  [bold]agent domain live[/bold]\n"
            )
        else:
            con.print(
                "\n  [yellow]Certificate not ready yet — ACME can take 1-3 min.[/yellow]\n"
                f"  Watch: [bold]kubectl get certificate -n {ing.namespace} -w[/bold]\n"
                "  Debug: [bold]kubectl get challenges -A[/bold]\n"
            )
    else:
        from agent.integrations.kubectl import apply_fix as _apply_fix
        with con.status("[bold yellow]Applying...[/bold yellow]", spinner="dots"):
            r2 = _apply_fix(fix_cmd)
        if r2.success:
            con.print("  [bold green]✓ Applied.[/bold green]\n")
        else:
            con.print(f"  [bold red]✗ Failed:[/bold red] {_escape(r2.error[:120])}\n")


# ---------------------------------------------------------------------------
# Helpers: dns
# ---------------------------------------------------------------------------

_DNS_PROBLEM_COLOR = {
    "CoreDNSNotRunning":    "bold red",
    "CoreDNSNotInstalled":  "bold red",
    "CoreDNSConfigInvalid": "yellow",
    "DNSResolutionFail":    "bold red",
    "ExternalDNSFail":      "yellow",
    "NdotsMisconfigured":   "yellow",
    "KubeDNSSvcMissing":    "bold red",
    "NoEndpoints":          "red",
    "Healthy":              "green",
    "Unknown":              "dim",
}


# ---------------------------------------------------------------------------
# Command: agent dns scan
# ---------------------------------------------------------------------------

@dns_app.command("scan")
def dns_scan() -> None:
    """
    Full DNS health audit — CoreDNS pods, config, resolution tests, external-dns, ndots.

    Runs 6 collectors in parallel, one Claude call for analysis.
    """
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.dns import DNSSkill

        console.print()
        console.print(Rule("[bold cyan]DNS — Full Scan[/bold cyan]"))
        console.print()

        report = DNSSkill().scan()
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent dns diagnose NAME
# ---------------------------------------------------------------------------

@dns_app.command("diagnose")
def dns_diagnose(
    name: str = typer.Argument("coredns", help="Component to diagnose (e.g. coredns, kube-dns)."),
    namespace: str = typer.Option(
        "kube-system", "--namespace", "-n", show_default=True,
    ),
    auto_fix: bool = typer.Option(False, "--auto-fix"),
) -> None:
    """Deep AI diagnosis of a DNS component."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.dns import DNSSkill

        skill = DNSSkill()
        console.print()

        with console.status(
            f"[bold cyan]Diagnosing DNS component '{name}' in {namespace}...[/bold cyan]",
            spinner="dots",
        ):
            diagnosis = skill.diagnose(name, namespace)

        console.print()

        if not diagnosis.fix_command:
            console.print("[dim]No automated fix command available.[/dim]")
            console.print()
            _cost_footer(get_session_total())
            return

        apply = auto_fix or typer.confirm("Apply this fix?", default=False)
        if not apply:
            console.print("[dim]Fix skipped.[/dim]")
            console.print()
            _cost_footer(get_session_total())
            return

        with console.status("[bold yellow]Applying fix...", spinner="dots"):
            ok = skill.apply_fix(diagnosis.fix_command, name, namespace)

        console.print("[bold green]Fix applied.[/bold green]" if ok
                      else "[bold red]Fix command failed.[/bold red]")
        console.print()
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent dns restart
# ---------------------------------------------------------------------------

@dns_app.command("restart")
def dns_restart() -> None:
    """Restart CoreDNS pods via rollout restart."""
    try:
        from agent.skills.dns import DNSSkill
        console.print()
        if typer.confirm("Restart CoreDNS deployment?", default=False):
            DNSSkill().restart_coredns()
        else:
            console.print("[dim]Aborted.[/dim]")
        console.print()
    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent dns heal
# ---------------------------------------------------------------------------

@dns_app.command("heal")
def dns_heal() -> None:
    """Scan all DNS issues and interactively apply every available fix."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.dns import DNSSkill

        console.print()
        console.print(Rule("[bold cyan]DNS — Heal[/bold cyan]"))
        console.print()

        skill = DNSSkill()

        with console.status("[bold cyan]Scanning for DNS issues...[/bold cyan]", spinner="dots"):
            skill.scan()

        # scan() already renders — no report object to iterate here
        # The skill.scan() internally prints the table; user applies fixes manually
        # or runs diagnose+fix-ip for targeted fixes
        console.print(
            "\n  [dim]Targeted fix:[/dim]  [bold]agent dns diagnose coredns -n kube-system[/bold]\n"
            "  [dim]Restart CoreDNS:[/dim]  [bold]agent dns restart[/bold]"
        )
        console.print()
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Helpers: network
# ---------------------------------------------------------------------------

_NETWORK_PROBLEM_COLOR = {
    "CniNotRunning":          "bold red",
    "CniNotDetected":         "yellow",
    "KubeProxyDown":          "bold red",
    "ServiceNoEndpoints":     "yellow",
    "PodConnectivityFail":    "bold red",
    "NetworkPolicyBlocking":  "yellow",
    "NodeNetworkUnavailable": "bold red",
    "NodePressure":           "yellow",
    "Healthy":                "green",
    "Unknown":                "dim",
}


# ---------------------------------------------------------------------------
# Command: agent network scan
# ---------------------------------------------------------------------------

@network_app.command("scan")
def network_scan() -> None:
    """
    Full network health audit — CNI, kube-proxy, NetworkPolicy, services, nodes.

    Runs 7 collectors in parallel, one Claude call for analysis.
    """
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.network import NetworkSkill

        console.print()
        console.print(Rule("[bold cyan]Network — Full Scan[/bold cyan]"))
        console.print()

        NetworkSkill().scan()
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent network diagnose NAME
# ---------------------------------------------------------------------------

@network_app.command("diagnose")
def network_diagnose(
    name: str = typer.Argument("cni", help="Component: cni, kube-proxy, service/<name>, etc."),
    namespace: str = typer.Option(
        "kube-system", "--namespace", "-n", show_default=True,
    ),
    auto_fix: bool = typer.Option(False, "--auto-fix"),
) -> None:
    """Deep AI diagnosis of a network component."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.network import NetworkSkill

        skill = NetworkSkill()
        console.print()

        with console.status(
            f"[bold cyan]Diagnosing network component '{name}' in {namespace}...[/bold cyan]",
            spinner="dots",
        ):
            diagnosis = skill.diagnose(name, namespace)

        console.print()

        if not diagnosis.fix_command:
            console.print("[dim]No automated fix command available.[/dim]")
            console.print()
            _cost_footer(get_session_total())
            return

        apply = auto_fix or typer.confirm("Apply this fix?", default=False)
        if not apply:
            console.print("[dim]Fix skipped.[/dim]")
            console.print()
            _cost_footer(get_session_total())
            return

        with console.status("[bold yellow]Applying fix...", spinner="dots"):
            ok = skill.apply_fix(diagnosis.fix_command, name, namespace)

        console.print("[bold green]Fix applied.[/bold green]" if ok
                      else "[bold red]Fix command failed.[/bold red]")
        console.print()
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent network restart-cni
# ---------------------------------------------------------------------------

@network_app.command("restart-cni")
def network_restart_cni(
    cni: str = typer.Option(
        "calico", "--cni", "-c",
        help="CNI to restart: calico | flannel | cilium | weave",
        show_default=True,
    ),
) -> None:
    """Restart a CNI DaemonSet via rollout restart."""
    try:
        from agent.skills.network import NetworkSkill
        console.print()
        if typer.confirm(f"Restart {cni} DaemonSet?", default=False):
            NetworkSkill().restart_cni(cni)
        else:
            console.print("[dim]Aborted.[/dim]")
        console.print()
    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent network heal
# ---------------------------------------------------------------------------

@network_app.command("heal")
def network_heal() -> None:
    """Scan all network issues and show targeted fix commands."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.network import NetworkSkill

        console.print()
        console.print(Rule("[bold cyan]Network — Heal[/bold cyan]"))
        console.print()

        NetworkSkill().scan()

        console.print(
            "\n  [dim]Targeted fix:[/dim]  [bold]agent network diagnose cni -n kube-system[/bold]\n"
            "  [dim]Restart CNI:[/dim]     [bold]agent network restart-cni --cni calico[/bold]"
        )
        console.print()
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent info
# ---------------------------------------------------------------------------

_AREA_ICON = {
    "system":           "💻",
    "tools":            "🔧",
    "k8s_context":      "⎈",
    "ingress_context":  "🌍",
    "tls_context":      "🔒",
    "dns_context":      "🔍",
    "network_context":  "🕸",
    "aws_context":      "☁",
    "gmail_context":    "✉",
    "env":              "🔑",
    "git":              "📁",
    "local_network":    "🌐",
    "docker":           "🐳",
    "python_packages":  "🐍",
}


@app.command()
def info(
    analyze: bool = typer.Option(
        False,
        "--analyze", "-a",
        help="Ask Claude to analyze the environment and summarize capabilities.",
    ),
    areas: Optional[str] = typer.Option(
        None,
        "--areas",
        help="Comma-separated areas to collect. Default: all. "
             "Options: system,tools,k8s_context,ingress_context,tls_context,"
             "dns_context,network_context,aws_context,gmail_context,"
             "env,git,local_network,docker,python_packages",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose", "-v",
        help="Show raw data for each area, not just the summary.",
    ),
) -> None:
    """
    Collect a full environment snapshot — OS, tools, K8s, AWS, Gmail, Git, Docker.

    Runs all collectors in parallel and renders a rich summary panel.
    Use --analyze to get a Claude-powered assessment of your setup.
    """
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.info_gather import InfoGatherSkill

        area_list = [a.strip() for a in areas.split(",")] if areas else None

        console.print()
        console.print(Rule("[bold cyan]Agentic OS — Environment Snapshot[/bold cyan]"))
        console.print()

        with console.status(
            "[bold cyan]Collecting environment data in parallel...[/bold cyan]",
            spinner="dots",
        ):
            result = InfoGatherSkill().run({
                "areas":   area_list,
                "analyze": analyze,
            })

        elapsed   = f"{result['collection_ms'] / 1000:.2f}s"
        ok_count  = len(result["areas_ok"])
        fail_count = len(result["areas_failed"])

        console.print(
            f"  Collected [bold green]{ok_count}[/bold green] area(s) in [dim]{elapsed}[/dim]"
            + (f"  [bold red]{fail_count} failed[/bold red]" if fail_count else "")
        )
        console.print()

        # ── Per-area panels ───────────────────────────────────────────────
        results = result["results"]
        area_order = [
            "system", "local_network", "tools", "python_packages",
            "k8s_context", "ingress_context", "tls_context",
            "dns_context", "network_context",
            "docker", "aws_context", "gmail_context",
            "git", "env",
        ]
        ordered = [a for a in area_order if a in results] + \
                  [a for a in results if a not in area_order]

        for area in ordered:
            res  = results[area]
            icon = _AREA_ICON.get(area, "•")
            ok   = res.get("ok", False)
            summary = res.get("summary", "(no data)")
            color = "cyan" if ok else "red"
            label = f"{icon}  [bold]{area.replace('_', ' ').title()}[/bold]"

            if verbose and ok and res.get("data"):
                import json as _json
                try:
                    raw = _json.dumps(res["data"], indent=2)
                    body = summary + "\n\n[dim]" + _escape(raw[:800]) + "[/dim]"
                except Exception:
                    body = summary
            else:
                body = summary

            console.print(Panel(
                body,
                title=label,
                border_style=color,
                padding=(0, 1),
            ))

        console.print()

        # ── Claude analysis panel ─────────────────────────────────────────
        if result.get("analysis"):
            console.print(Panel(
                _escape(result["analysis"]),
                title="[bold]AI ANALYSIS[/bold]",
                border_style="magenta",
                padding=(0, 1),
            ))
            console.print()
            _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

@app.command("setup")
def cmd_setup() -> None:
    """Interactive wizard to configure AI providers, AWS, Gmail, and integrations."""
    from agent.skills.setup import SetupWizard
    SetupWizard().run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()
