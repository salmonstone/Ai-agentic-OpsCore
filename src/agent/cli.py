"""
AI Agentic OS — command-line interface.

Entry point: agent <command> [options]

Run `agent commands` for the full command list with descriptions,
or `agent commands --search TEXT` to filter it.
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

# Load .env into the real process environment. config.py's pydantic Settings
# reads .env on its own, but several integrations (AWS profile/region,
# webhook secrets) read os.environ directly — without this, those values
# are silently ignored even after `agent setup` writes them to .env.
from dotenv import load_dotenv
load_dotenv()

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

# ── First-run bootstrap ────────────────────────────────────────────────────
# Create data/ directory structure so SQLite and vault never fail on first use.
_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_ENV_FILE  = Path(__file__).parent.parent.parent / ".env"

for _d in (_DATA_DIR, _DATA_DIR / "vault"):
    _d.mkdir(parents=True, exist_ok=True)

# Show a helpful banner if .env doesn't exist yet (first run)
if not _ENV_FILE.exists():
    _c = Console()
    _c.print()
    _c.print(Panel(
        "[bold yellow]First run detected — no .env file found.[/bold yellow]\n\n"
        "Run the setup wizard to configure your API keys:\n\n"
        "  [bold cyan]agent setup[/bold cyan]\n\n"
        "Or copy the template and edit it manually:\n\n"
        "  [bold cyan]cp .env.example .env[/bold cyan]",
        title="[bold]Welcome to AI Agentic OS[/bold]",
        border_style="yellow",
    ))
    _c.print()
# ──────────────────────────────────────────────────────────────────────────

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

# Commands that work without a live cluster (kubeconfig / local DB only).
_K8S_NO_CLUSTER_CMDS = {"contexts", "switch", "add-cluster", "history"}


@k8s_app.callback(invoke_without_command=True)
def _k8s_cluster_guard(ctx: typer.Context) -> None:
    """Before any k8s subcommand, verify the cluster is reachable."""
    if ctx.invoked_subcommand is None:
        return   # no subcommand → Typer prints help, nothing to guard
    if ctx.invoked_subcommand in _K8S_NO_CLUSTER_CMDS:
        return   # these don't touch the cluster
    try:
        from agent.integrations.kubectl import is_cluster_available
        if not is_cluster_available():
            _print_no_cluster_panel()
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception:
        return  # reachability check itself failed — let the subcommand surface its own error

    try:
        from agent.integrations.kubectl import check_cluster_auth, build_eks_access_fix_hint, get_current_context
        ok, _detail = check_cluster_auth()
        if not ok:
            console.print()
            console.print(Panel(
                "[bold red]Connected, but not authorized.[/bold red]\n\n"
                "The cluster's API server is reachable and your AWS/cloud credentials "
                "are valid — but the cluster's own RBAC doesn't recognize this identity, "
                "so every request is rejected before it even checks permissions.\n\n"
                f"[bold]Fix:[/bold]\n\n{_escape(build_eks_access_fix_hint(get_current_context()))}",
                title="[bold]Cluster Access Denied[/bold]",
                border_style="red",
                padding=(1, 2),
            ))
            console.print()
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception:
        return  # auth probe itself failed — let the subcommand surface its own error


def _print_no_cluster_panel() -> None:
    console.print()
    console.print(Panel(
        "[bold red]No Kubernetes cluster connected.[/bold red]\n\n"
        "Your kubeconfig has no reachable cluster, or the cluster has been deleted.\n\n"
        "[bold]Connect a cluster:[/bold]\n\n"
        "  [bold cyan]AWS EKS[/bold cyan]\n"
        "    aws eks update-kubeconfig --name <cluster-name> --region <region>\n\n"
        "  [bold cyan]Local (minikube)[/bold cyan]\n"
        "    minikube start\n\n"
        "  [bold cyan]Local (kind)[/bold cyan]\n"
        "    kind create cluster\n\n"
        "  [bold cyan]Or use AtlasOS to add an EKS cluster[/bold cyan]\n"
        "    agent k8s add-cluster --name <name> --region <region>\n\n"
        "[dim]Once connected, re-run your command. "
        "Cluster status is re-checked every 30 seconds.[/dim]",
        title="[bold]Cluster Not Found[/bold]",
        border_style="red",
        padding=(1, 2),
    ))
    console.print()


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

resources_app = typer.Typer(
    help="Resource monitor — CPU/memory usage, limit alerts, and guided fixes.",
    no_args_is_help=True,
)
app.add_typer(resources_app, name="resources")

cost_app = typer.Typer(
    help="Cost analysis — estimate pod/node costs, detect waste, and query AWS billing.",
    no_args_is_help=True,
)
app.add_typer(cost_app, name="cost")

monitor_app = typer.Typer(
    help="Monitoring daemon — continuous pod/resource/security checks with Slack alerting.",
    no_args_is_help=True,
)
app.add_typer(monitor_app, name="monitor")

deploy_app = typer.Typer(
    help="GitHub webhook deployment management — mappings, simulation, and webhook control.",
    no_args_is_help=True,
)
app.add_typer(deploy_app, name="deploy")

daemon_app = typer.Typer(
    help="24/7 autonomous healing daemon — monitors pods, resources, and costs.",
    no_args_is_help=True,
)
app.add_typer(daemon_app, name="daemon")

incident_app = typer.Typer(
    help="Incident tracking and management.",
    no_args_is_help=True,
)
app.add_typer(incident_app, name="incident")

slo_app = typer.Typer(
    help="SLO and error budget tracking.",
    no_args_is_help=True,
)
app.add_typer(slo_app, name="slo")

db_app = typer.Typer(
    help="Database health monitoring — RDS and Aurora.",
    no_args_is_help=True,
)
app.add_typer(db_app, name="db")

scale_app = typer.Typer(
    help="Auto-scaling policies — scheduled and metric-based.",
    no_args_is_help=True,
)
app.add_typer(scale_app, name="scale")

runbook_app = typer.Typer(
    help="Runbook automation — YAML-defined playbooks.",
    no_args_is_help=True,
)
app.add_typer(runbook_app, name="runbook")

page_app = typer.Typer(
    help="On-call paging via PagerDuty or OpsGenie.",
    no_args_is_help=True,
)
app.add_typer(page_app, name="page")

dashboard_app = typer.Typer(
    help="Web dashboard — real-time status of daemon, incidents, SLOs, costs.",
    no_args_is_help=True,
)
app.add_typer(dashboard_app, name="dashboard")

events_app = typer.Typer(
    help="Durable event queue — inspect, retry, and manually enqueue events.",
    no_args_is_help=True,
)
app.add_typer(events_app, name="events")

terraform_app = typer.Typer(
    help="Terraform (IaC) review — offline security, cost, and blast-radius scan.",
    no_args_is_help=True,
)
app.add_typer(terraform_app, name="terraform")

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
# Command: agent commands
# ---------------------------------------------------------------------------

def _walk_commands(click_cmd, prefix: str = ""):
    """Yield (full_name, short_help, is_group) for every command in the tree.

    Duck-types on ``.commands`` rather than ``isinstance(sub, click.Group)`` —
    Typer vendors its own Click shim, so its groups are not click.Group
    subclasses.
    """
    for name in sorted(getattr(click_cmd, "commands", {})):
        sub = click_cmd.commands[name]
        if getattr(sub, "hidden", False):
            continue
        full = f"{prefix}{name}"
        is_group = bool(getattr(sub, "commands", None))
        try:
            help_text = sub.get_short_help_str(limit=200)
        except Exception:
            help_text = ((sub.help or "").strip().splitlines() or [""])[0]
        yield full, help_text, is_group
        if is_group:
            yield from _walk_commands(sub, prefix=f"{full} ")


@app.command()
def commands(
    search: Optional[str] = typer.Option(
        None,
        "--search", "-s",
        help="Only show commands whose name or description matches this text.",
    ),
    plain: bool = typer.Option(
        False,
        "--plain", "-p",
        help="One command per line, no table — easy to grep or pipe.",
    ),
) -> None:
    """List every available command and what it does."""
    import typer.main

    root = typer.main.get_command(app)
    entries = list(_walk_commands(root))

    if search:
        needle = search.lower()
        # Keep a group if it matches, or if any of its children match.
        keep = {
            name for name, help_text, _ in entries
            if needle in name.lower() or needle in help_text.lower()
        }
        for name in list(keep):
            parts = name.split(" ")
            for i in range(1, len(parts)):
                keep.add(" ".join(parts[:i]))
        entries = [e for e in entries if e[0] in keep]

    if not entries:
        console.print()
        console.print(f"  [yellow]No commands match[/yellow] [bold]{_escape(search or '')}[/bold]")
        console.print()
        raise typer.Exit(1)

    if plain:
        width = max(len(name) for name, _, _ in entries)
        for name, help_text, is_group in entries:
            if is_group:
                continue
            print(f"agent {name.ljust(width)}  {help_text}")
        return

    leaf_count = sum(1 for _, _, is_group in entries if not is_group)
    group_count = len(entries) - leaf_count

    console.print()
    console.print(Rule("[bold]Available Commands[/bold]"))
    console.print()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Description", style="dim")

    for name, help_text, is_group in entries:
        depth = name.count(" ")
        if is_group:
            table.add_row("", "")
            table.add_row(
                f"[bold cyan]{_escape(name)}[/bold cyan]",
                f"[not dim]{_escape(help_text)}[/not dim]",
            )
        else:
            indent = "  " * depth
            table.add_row(f"{indent}[cyan]{_escape(name)}[/cyan]", _escape(help_text))

    console.print(table)
    console.print()
    console.print(Rule(style="dim"))
    console.print(
        f"  [dim]{leaf_count} commands across {group_count} groups."
        f" Run[/dim] agent <command> --help [dim]for options.[/dim]",
        highlight=False,
    )
    console.print()


# ---------------------------------------------------------------------------
# Command: agent about  —  living architecture documentation
# ---------------------------------------------------------------------------

_ABOUT_BANNER = r"""
         █████╗ ████████╗██╗      █████╗ ███████╗
        ██╔══██╗╚══██╔══╝██║     ██╔══██╗██╔════╝
        ███████║   ██║   ██║     ███████║███████╗
        ██╔══██║   ██║   ██║     ██╔══██║╚════██║
        ██║  ██║   ██║   ███████╗██║  ██║███████║
        ╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝╚══════╝
"""

_ABOUT_DIAGRAM = r"""
┌─────────────────────────────────────────────────────┐
│                  USER INTERFACES                     │
│   CLI (Typer + Rich)     Dashboard (React + FastAPI) │
│   agent <command>        http://localhost:3000       │
└──────────────────────┬──────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────┐
│              SKILL EXECUTION LAYER                    │
│   cli.py routes each command → a skill class          │
│   src/agent/skills/*.py                              │
│   Each skill: gather data → retrieve memory →         │
│               ONE Claude call → save → render         │
└───────────┬──────────────────────────────────────────┘
            │
     ┌──────┴───────┐
     ▼              ▼
┌──────────┐  ┌──────────────────────────────────────┐
│  CLAUDE  │  │        DATA COLLECTION (Python)       │
│ core/    │  │  kubectl  boto3  Gmail  Slack  httpx  │
│ llm.py   │◄─┤  Skill assembles ONE prompt from      │
│ (single  │  │  live data + past memory, then calls  │
│  call)   │──►  Claude once. No tool-use callback.    │
└──────────┘  └──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│                  MEMORY LAYER                        │
│   SQLite (store.py, source of truth)                 │
│   ChromaDB (embeddings.py, vectors) → RAG via         │
│   retrieval.py   ──►  Obsidian vault sync            │
└─────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│              OBSERVABILITY LAYER                     │
│   structlog logs   token+cost (costs.py)   audit DB  │
│   eval suites      deploy reports                    │
└─────────────────────────────────────────────────────┘
"""

_ABOUT_TECH_STACK = [
    ("Language",        "Python 3.11 — AI ecosystem alignment"),
    ("LLM",             "Claude — strong reasoning for agentic ops"),
    ("No framework",    "Raw Anthropic SDK — full control, debuggable"),
    ("Memory primary",  "SQLite — embedded, zero-ops, reliable"),
    ("Memory vectors",  "ChromaDB — local, no infra needed"),
    ("CLI",             "Typer — type-hint driven, clean"),
    ("Terminal UI",     "Rich — tables, panels, colors"),
    ("Data validation", "Pydantic v2 — type safety everywhere"),
    ("Dashboard API",   "FastAPI — async, same Python codebase"),
    ("Dashboard UI",    "React (Vite) — inline-styled, no framework"),
    ("Live updates",    "WebSocket — real-time, bidirectional"),
    ("HTTP client",     "httpx async — non-blocking API calls"),
    ("Logging",         "structlog — JSON, queryable"),
    ("Package manager", "uv — fast, modern, reproducible"),
]

_ABOUT_PROD_FEATURES = [
    "Alert deduplication (cooldown + severity escalation)",
    "Auto-rollback on deploy failure",
    "False-positive filtering (security scan)",
    "Human-in-the-loop approval gates",
    "Production namespace name confirmation",
    "Cost tracking on every Claude call",
    "Full audit log of all actions (SQLite)",
    "Persistent memory with RAG retrieval",
    "Webhook HMAC signature verification",
    "Graceful error handling with retries",
    "Secrets in env vars only (never in code)",
]

# Which sections --section can target
_ABOUT_SECTIONS = [
    "status", "scale", "skills", "architecture",
    "dataflow", "analysis", "commands", "tech-stack", "features",
]


def _about_yn(value: bool, yes: str = "✓", no: str = "–") -> str:
    return f"[green]{yes}[/green]" if value else f"[dim]{no}[/dim]"


def _about_header() -> None:
    body = (
        f"[bold cyan]{_ABOUT_BANNER.strip(chr(10))}[/bold cyan]\n\n"
        "[bold]AI-Native Infrastructure Operating System[/bold]\n"
        "[dim]Observe · Diagnose · Heal · Optimize[/dim]"
    )
    console.print()
    console.print(Panel(body, border_style="cyan", padding=(1, 2)))


def _about_status(state) -> None:
    console.print()
    console.print(Rule("[bold]Current State[/bold]"))
    console.print()
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("k", style="dim", width=16)
    t.add_column("v")

    cluster = (
        f"[green]{_escape(state.cluster_name)}[/green] [dim](connected)[/dim]"
        if state.kubectl_connected else "[dim]not connected[/dim]"
    )
    t.add_row("Cluster", cluster)
    t.add_row("AWS Profile",
              f"[green]{_escape(state.aws_profile)}[/green]" if state.aws_configured else "[dim]not set[/dim]")
    t.add_row("Memory", f"[cyan]{state.memory_records:,}[/cyan] records · "
                        f"[cyan]{state.embeddings_count:,}[/cyan] embeddings")
    t.add_row("Daemon",
              "[green]● RUNNING[/green]" if state.daemon_running else "[dim]○ stopped[/dim]")
    t.add_row("Webhook",
              f"[green]● listening[/green] [dim](port {state.webhook_port})[/dim]"
              if state.webhook_running else f"[dim]○ not running (port {state.webhook_port})[/dim]")
    t.add_row("Anthropic", _about_yn(state.anthropic_configured, "✓ key set", "– missing"))
    t.add_row("Slack", _about_yn(state.slack_configured, "✓ configured", "– not set"))
    t.add_row("Gmail", _about_yn(state.gmail_configured, "✓ connected", "– not set"))
    t.add_row("Deploys", f"[cyan]{state.total_deploys}[/cyan] recorded · "
                         f"[cyan]{state.pending_deploys}[/cyan] pending")
    t.add_row("Last Activity", _escape(state.last_activity))
    console.print(t)


def _about_scale(structure, skills_summary) -> None:
    total_cases = sum(s["cases"] for s in structure.eval_suites)
    console.print()
    console.print(Rule("[bold]Project Scale[/bold]"))
    console.print()
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("k", style="dim", width=22)
    t.add_column("v", justify="right")
    t.add_row("Total Python files", f"[bold cyan]{structure.total_files}[/bold cyan]")
    t.add_row("Total lines of code", f"[bold cyan]{structure.total_lines:,}[/bold cyan]")
    t.add_row("Skills", f"[cyan]{len(skills_summary)}[/cyan]")
    t.add_row("Integrations", f"[cyan]{len(structure.integrations)}[/cyan]")
    t.add_row("Eval suites", f"[cyan]{len(structure.eval_suites)}[/cyan]")
    t.add_row("Eval test cases", f"[cyan]{total_cases}[/cyan]")
    t.add_row("Webhook mappings", f"[cyan]{len(structure.webhook_mappings)}[/cyan]")
    t.add_row("Config variables", f"[cyan]{len(structure.config_vars)}[/cyan]")
    t.add_row("Dependencies", f"[cyan]{len(structure.dependencies)}[/cyan]")
    console.print(t)
    console.print()
    console.print("  [dim italic]All numbers from a live filesystem scan — nothing hardcoded.[/dim italic]")


def _about_skills(skills_summary) -> None:
    console.print()
    console.print(Rule("[bold]Installed Skills[/bold]"))
    console.print()
    t = Table(show_header=True, header_style="bold cyan", border_style="dim")
    t.add_column("Skill", style="cyan", no_wrap=True)
    t.add_column("What it does")
    t.add_column("Eval Suite", justify="center")
    for s in skills_summary:
        desc = s["description"]
        if len(desc) > 60:
            desc = desc[:57] + "…"
        eval_cell = (
            f"[green]✓ {s['eval_cases']} cases[/green]"
            if s["eval_suite"] and s["eval_cases"]
            else ("[green]✓[/green]" if s["eval_suite"] else "[dim]—[/dim]")
        )
        t.add_row(s["label"], _escape(desc), eval_cell)
    console.print(t)
    console.print()
    console.print(f"  [dim italic]{len(skills_summary)} skills discovered by scanning "
                  "src/agent/skills/.[/dim italic]")


def _about_diagram() -> None:
    console.print()
    console.print(Rule("[bold]Architecture[/bold]"))
    console.print(f"[dim]{_ABOUT_DIAGRAM}[/dim]")


def _about_dataflow() -> None:
    console.print()
    console.print(Rule("[bold]Data Flow — what happens when you run[/bold] agent k8s scan"))
    console.print()
    steps = [
        ("1", "cli.py receives the command and routes it to K8sSkill.scan_cluster()."),
        ("2", "The skill gathers live data in Python: integrations/kubectl.py runs "
              "`kubectl get pods -A` and parses JSON into PodInfo objects."),
        ("3", "memory/retrieval.py runs a ChromaDB semantic search for relevant past "
              "incidents and returns them as RAG context."),
        ("4", "The skill assembles ONE prompt (live pods + past memory) and makes a "
              "single Claude call via core/llm.py (timed, cost-tracked, retried)."),
        ("5", "Claude returns a diagnosis. The skill parses it — no tool-use callback; "
              "all data was gathered before the call."),
        ("6", "Result saved to memory (store.py + embeddings.py), rendered as a Rich "
              "table. If Slack is on, a deduplicated alert is sent; cost is logged (costs.py)."),
    ]
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("n", style="bold cyan", width=3)
    t.add_column("desc")
    for n, d in steps:
        t.add_row(f"Step {n}", d)
    console.print(t)


def _about_commands() -> None:
    from agent.dashboard.discovery import get_command_groups
    console.print()
    console.print(Rule("[bold]All Available Commands[/bold]"))
    console.print("  [dim italic]Generated from live CLI introspection — never hardcoded.[/dim italic]")
    console.print()
    groups = get_command_groups()
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("cmd", style="cyan", no_wrap=True)
    t.add_column("help", style="dim")
    for group, cmds in groups.items():
        t.add_row("", "")
        t.add_row(f"[bold cyan]{_escape(group)}[/bold cyan]", "")
        for c in cmds:
            help_text = (c.get("help_text") or "").strip().splitlines()
            help_text = help_text[0] if help_text else ""
            t.add_row(f"  agent {_escape(c['full_command'])}", _escape(help_text))
    console.print(t)


def _about_techstack() -> None:
    console.print()
    console.print(Rule("[bold]Technology Choices[/bold]"))
    console.print()
    t = Table(show_header=True, header_style="bold cyan", border_style="dim")
    t.add_column("Component", style="cyan", no_wrap=True)
    t.add_column("Choice + Reason")
    for comp, reason in _ABOUT_TECH_STACK:
        t.add_row(comp, reason)
    console.print(t)


def _about_features(structure) -> None:
    total_cases = sum(s["cases"] for s in structure.eval_suites)
    console.print()
    console.print(Rule("[bold]What Makes This Production-Grade[/bold]"))
    console.print()
    console.print(
        f"  [green]✓[/green] Eval suites per skill "
        f"([bold]{total_cases}[/bold] test cases across "
        f"[bold]{len(structure.eval_suites)}[/bold] suites)"
    )
    for feat in _ABOUT_PROD_FEATURES:
        console.print(f"  [green]✓[/green] {feat}")


def _about_claude(skill, structure, state) -> None:
    from rich.markdown import Markdown
    console.print()
    console.print(Rule("[bold]Claude Architecture Analysis[/bold]"))
    console.print()
    with console.status("[cyan]Analyzing project architecture…[/cyan]", spinner="dots"):
        explanation = skill.generate_architecture_explanation(structure, state)
    console.print(Markdown(explanation))
    console.print()
    from agent.observability.costs import get_session_total
    totals = get_session_total()
    if totals["calls"]:
        _cost_footer(totals)


@app.command()
def about(
    section: Optional[str] = typer.Option(
        None, "--section", "-s",
        help="Show only one section: " + ", ".join(_ABOUT_SECTIONS) + ".",
    ),
    no_claude: bool = typer.Option(
        False, "--no-claude",
        help="Skip the Claude analysis section — instant, no API call.",
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit the full project scan as JSON (no Claude, no Rich).",
    ),
) -> None:
    """Explain the entire AtlasOS architecture from a live scan of the project."""
    from agent.skills.about import AboutSkill

    skill = AboutSkill()

    if section and section not in _ABOUT_SECTIONS:
        _print_error(
            f"Unknown section {section!r}. Choose one of: {', '.join(_ABOUT_SECTIONS)}."
        )
        raise typer.Exit(1)

    structure = skill.scan_project_structure()
    state = skill.scan_runtime_state()
    skills_summary = [
        {
            "label": s.label, "name": s.name, "description": s.description,
            "eval_suite": s.eval_suite, "eval_cases": s.eval_cases,
            "integrations_used": s.integrations_used, "methods": s.methods,
        }
        for s in skill.get_skills_summary(structure)
    ]

    # ── JSON mode: dump and exit ────────────────────────────────────────────
    if as_json:
        payload = {
            "version": structure.version,
            "structure": structure.to_dict(),
            "runtime": state.to_dict(),
            "skills": skills_summary,
        }
        print(json.dumps(payload, indent=2, default=str))
        return

    # ── Single-section mode ─────────────────────────────────────────────────
    if section:
        if section == "status":
            _about_status(state)
        elif section == "scale":
            _about_scale(structure, skills_summary)
        elif section == "skills":
            _about_skills(skills_summary)
        elif section == "architecture":
            _about_diagram()
        elif section == "dataflow":
            _about_dataflow()
        elif section == "commands":
            _about_commands()
        elif section == "tech-stack":
            _about_techstack()
        elif section == "features":
            _about_features(structure)
        elif section == "analysis":
            _about_claude(skill, structure, state)
        console.print()
        return

    # ── Full report ─────────────────────────────────────────────────────────
    _about_header()
    _about_status(state)
    _about_scale(structure, skills_summary)
    _about_skills(skills_summary)
    _about_diagram()
    _about_dataflow()
    if not no_claude:
        _about_claude(skill, structure, state)
    _about_commands()
    _about_techstack()
    _about_features(structure)
    console.print()
    console.print(Rule(style="dim"))
    console.print(
        "  [dim]Living documentation — regenerated from the live project each run."
        "  Use[/dim] agent about --section <name> [dim]for one section,[/dim]"
        " --no-claude [dim]to skip AI, or[/dim] --json [dim]for raw data.[/dim]",
        highlight=False,
    )
    console.print()


# ---------------------------------------------------------------------------
# Command: agent terraform scan
# ---------------------------------------------------------------------------

_TF_SEV_COLOR = {
    "critical": "bold red", "high": "red", "medium": "yellow",
    "low": "cyan", "info": "dim",
}


@terraform_app.command("scan")
def terraform_scan(
    path: str = typer.Argument(".", help="Directory (or file) containing .tf files."),
    plan: Optional[str] = typer.Option(
        None, "--plan",
        help="A `terraform show -json <planfile>` JSON for real blast-radius.",
    ),
    no_claude: bool = typer.Option(
        False, "--no-claude", help="Skip the Claude review — instant, no API call.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the full report as JSON (no Claude, no Rich).",
    ),
    fix: bool = typer.Option(
        False, "--fix",
        help="PATCH the .tf files with safe additive fixes (adds a .bak backup). "
             "Never runs `terraform apply`.",
    ),
) -> None:
    """
    Review Terraform config offline — what's dangerous, insecure, or expensive.

    A judgment layer on top of `terraform plan`: it reads your .tf files and
    ranks the security, cost, and blast-radius risks in plain English. No
    cluster, no AWS credentials, no `terraform` binary required.
    """
    from agent.skills.terraform import TerraformScanSkill

    root = Path(path)
    if not root.exists():
        _print_error(f"Path not found: {path}")
        raise typer.Exit(1)

    skill = TerraformScanSkill()
    report = skill.scan(path, plan_file=plan, with_ai=not (as_json or no_claude))

    if not report.resource_count and not report.modules and not report.parse_errors:
        _print_error(
            f"No Terraform resources or modules found under '{path}'. "
            "Point it at a folder that contains .tf files."
        )
        raise typer.Exit(1)

    # ── JSON mode ───────────────────────────────────────────────────────────
    if as_json:
        print(json.dumps(report.model_dump(), indent=2, default=str))
        return

    # ── Header ──────────────────────────────────────────────────────────────
    score = report.security_score
    score_color = "green" if score >= 85 else "yellow" if score >= 60 else "red"
    console.print()
    console.print(Rule(f"[bold]Terraform Review — {_escape(report.root)}[/bold]"))
    console.print(
        f"  Resources: [cyan]{report.resource_count}[/cyan] in "
        f"{report.file_count} file(s)   "
        f"Modules: [cyan]{len(report.modules)}[/cyan]   "
        f"Providers: [cyan]{_escape(', '.join(report.providers) or 'none')}[/cyan]   "
        f"State: {'[green]remote[/green]' if report.remote_state else '[yellow]local[/yellow]'}",
        highlight=False,
    )
    console.print(
        f"  Security score: [{score_color}]{score}/100[/{score_color}]   "
        f"Findings: [red]{report.critical_count} critical[/red], "
        f"[yellow]{report.high_count} high[/yellow]   "
        f"Est. cost: [green]${report.estimated_monthly_cost:,.2f}/mo[/green]",
        highlight=False,
    )

    # ── Plan blast radius (only if a plan JSON was supplied) ─────────────────
    if report.plan_actions:
        acts = "  ".join(f"{k}: {v}" for k, v in sorted(report.plan_actions.items()))
        console.print(f"  Plan actions — {_escape(acts)}", highlight=False)
        if report.replace_addresses:
            console.print(
                f"  [bold red]Destroy+recreate:[/bold red] "
                f"{_escape(', '.join(report.replace_addresses))}",
                highlight=False,
            )

    # ── Modules ───────────────────────────────────────────────────────────────
    if report.modules:
        console.print()
        mtable = Table(title="Modules", title_style="bold", box=None, padding=(0, 1))
        mtable.add_column("Module")
        mtable.add_column("Source")
        mtable.add_column("Version", no_wrap=True)
        for mod in report.modules:
            if mod.local:
                ver = "[dim]local[/dim]"
            elif mod.version:
                ver = f"[green]{_escape(mod.version)}[/green]"
            else:
                ver = "[yellow]unpinned[/yellow]"
            mtable.add_row(f"module.{_escape(mod.name)}", _escape(mod.source), ver)
        console.print(mtable)
        if not report.resource_count:
            console.print(
                "  [dim]Module-only config — the real resources live inside these "
                "modules (run `terraform init` to fetch them).[/dim]",
                highlight=False,
            )

    # ── Findings ──────────────────────────────────────────────────────────────
    console.print()
    if report.findings:
        table = Table(title="Findings (ranked)", title_style="bold", box=None,
                      padding=(0, 1), show_lines=False)
        table.add_column("Severity", no_wrap=True)
        table.add_column("Category", style="dim", no_wrap=True)
        table.add_column("Finding")
        table.add_column("Fix", no_wrap=True)
        for f in report.findings:
            color = _TF_SEV_COLOR.get(f.severity, "white")
            fixcol = "[green]auto[/green]" if f.auto_fixable else ""
            table.add_row(
                f"[{color}]{f.severity.upper()}[/{color}]",
                f.category,
                _escape(f.title),
                fixcol,
            )
        console.print(table)
    else:
        console.print("  [green]No security or blast-radius findings.[/green]")

    # ── Cost drivers ──────────────────────────────────────────────────────────
    if report.cost_items:
        console.print()
        ctable = Table(title="Estimated monthly cost", title_style="bold", box=None,
                       padding=(0, 1))
        ctable.add_column("Resource")
        ctable.add_column("Detail", style="dim")
        ctable.add_column("$/mo", justify="right", style="green")
        ctable.add_column("Assumptions", style="dim")
        for c in report.cost_items[:12]:
            ctable.add_row(_escape(c.resource), _escape(c.detail),
                           f"{c.monthly_cost:,.2f}", _escape(c.assumptions))
        console.print(ctable)
        console.print(
            f"  [dim]Estimate only — a rough monthly figure, not a quote.[/dim]",
            highlight=False,
        )

    # ── Claude review ─────────────────────────────────────────────────────────
    if report.claude_summary:
        console.print()
        console.print(Panel(
            _escape(report.claude_summary),
            title="[bold]Claude review[/bold]",
            border_style="cyan",
        ))

    if report.parse_errors:
        console.print()
        console.print(f"  [yellow]Parse warnings:[/yellow] {len(report.parse_errors)}")
        for e in report.parse_errors[:5]:
            console.print(f"    [dim]{_escape(str(e))}[/dim]")

    # ── --fix: patch the .tf source only ──────────────────────────────────────
    if fix:
        console.print()
        console.print(Rule("[bold]Applying safe .tf patches[/bold]", style="dim"))
        fixable = [f for f in report.findings if f.auto_fixable]
        if not fixable:
            console.print("  [dim]No auto-fixable findings to patch.[/dim]")
        else:
            console.print(
                f"  This will edit {len({f.file for f in fixable})} file(s) "
                f"(a .bak backup is written first). "
                f"[bold]It will NOT run `terraform apply`.[/bold]"
            )
            for f in fixable:
                console.print(
                    f"    [green]+[/green] {_escape(f.resource)}: "
                    f"[cyan]{_escape(f.fix_attribute or '')}[/cyan]  "
                    f"[dim]({_escape(f.file)})[/dim]"
                )
            if typer.confirm("  Apply these patches?", default=False):
                results = skill.apply_fixes(report)
                applied = [r for r in results if r["status"] == "applied"]
                skipped = [r for r in results if r["status"] != "applied"]
                console.print(f"  [green]Patched {len(applied)} finding(s).[/green]")
                for r in skipped:
                    console.print(
                        f"    [yellow]skipped[/yellow] {r.get('finding')}: "
                        f"{_escape(str(r.get('reason', '')))}"
                    )
                console.print(
                    "  [dim]Review the diff, then run `terraform plan` to see the "
                    "effect before applying.[/dim]"
                )
            else:
                console.print("  [dim]Aborted — no files changed.[/dim]")

    console.print()
    from agent.observability.costs import get_session_total
    totals = get_session_total()
    if totals["calls"]:
        _cost_footer(totals)


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
# Command: agent eval resources
# ---------------------------------------------------------------------------

@eval_app.command("resources")
def eval_resources() -> None:
    """Run the Resource Monitor eval suite (mocked metrics, no live cluster needed)."""
    try:
        from evals.resources.runner import run_evals  # type: ignore[import]

        console.print()
        console.print(Rule("[bold]Resource Monitor Eval Suite[/bold]"))
        console.print()

        with console.status("[bold green]Running 8 eval cases...", spinner="dots"):
            data = run_evals()

        results = data["results"]
        totals  = data["totals"]

        table = Table(
            title=f"Resource Eval Results  [dim]({totals['cases']} cases)[/dim]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        table.add_column("ID",           width=10)
        table.add_column("Scenario",     max_width=32, no_wrap=True)
        table.add_column("Expected",     width=14)
        table.add_column("Alert?",       justify="center", width=8)
        table.add_column("Severity",     justify="center", width=10)
        table.add_column("Fix valid?",   justify="center", width=10)
        table.add_column("Score",        justify="right",  width=8)
        table.add_column("Pass",         justify="center", width=6)

        for r in results:
            alert_ok = "[green]OK[/green]"  if r["alert_pass"]    else "[red]FAIL[/red]"
            sev_ok   = "[green]OK[/green]"  if r["severity_pass"] else "[red]FAIL[/red]"
            fix_ok   = "[green]OK[/green]"  if r["fix_pass"]      else "[yellow]N/A[/yellow]"
            status   = (
                "[bold green]PASS[/bold green]" if r["passed"]
                else "[bold red]FAIL[/bold red]"
            )
            table.add_row(
                r["id"],
                r["scenario"][:30],
                r["expected_alert_type"] or "[dim]none[/dim]",
                alert_ok, sev_ok, fix_ok,
                f"{r['score']}/100",
                status,
            )

        console.print(table)
        console.print()

        n      = totals["cases"]
        passed = totals["passed"]
        pct    = round(passed / n * 100) if n else 0
        color  = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"
        avg_score = round(totals["total_score"] / n) if n else 0

        console.print(f"  Pass rate   : [{color}]{passed}/{n} ({pct}%)[/{color}]")
        console.print(f"  Avg score   : [cyan]{avg_score}/100[/cyan]")
        console.print(f"  Alert hits  : {totals['alert_hits']}/{n}")
        console.print(f"  Severity ok : {totals['severity_hits']}/{n}")
        console.print(f"  Fix valid   : {totals['fix_hits']}/{n}")
        console.print()

        if pct < 80:
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent eval security
# ---------------------------------------------------------------------------

@eval_app.command("security")
def eval_security() -> None:
    """Run Security Audit eval suite (no live cluster needed)."""
    try:
        from evals.security.runner import run_evals  # type: ignore[import]

        console.print()
        console.print(Rule("[bold]Security Audit Eval Suite[/bold]"))
        console.print()

        with console.status("[bold green]Running 8 eval cases...", spinner="dots"):
            data = run_evals()

        results = data["results"]
        totals  = data["totals"]

        tbl = Table(
            title=f"Security Eval Results  [dim]({totals['cases']} cases)[/dim]",
            show_lines=True, header_style="bold cyan", border_style="dim",
        )
        tbl.add_column("ID",       width=10)
        tbl.add_column("Scenario", max_width=36, no_wrap=True)
        tbl.add_column("Score",    width=10, justify="center")
        tbl.add_column("Range",    width=12, justify="center")
        tbl.add_column("Prod?",    width=8,  justify="center")
        tbl.add_column("Result",   width=8,  justify="center")

        for r in results:
            lo, hi = r["expected_range"]
            prod   = "[green]OK[/green]" if r["prod_pass"] else "[red]FAIL[/red]"
            status = "[bold green]PASS[/bold green]" if r["passed"] else "[bold red]FAIL[/bold red]"
            tbl.add_row(
                r["id"], r["scenario"][:34],
                f"{r['computed_score']}/100",
                f"[{lo}–{hi}]",
                prod, status,
            )

        console.print(tbl)
        console.print()

        n     = totals["cases"]
        pct   = round(totals["passed"] / n * 100) if n else 0
        col   = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"
        avg   = round(totals["total_score"] / n) if n else 0
        console.print(f"  Pass rate: [{col}]{totals['passed']}/{n} ({pct}%)[/{col}]")
        console.print(f"  Avg score: [cyan]{avg}/100[/cyan]")
        console.print()

        if pct < 80:
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent eval multi-cluster
# ---------------------------------------------------------------------------

@eval_app.command("multi-cluster")
def eval_multi_cluster() -> None:
    """Run Multi-Cluster eval suite (no live cluster needed)."""
    try:
        from evals.multi_cluster.runner import run_evals  # type: ignore[import]

        console.print()
        console.print(Rule("[bold]Multi-Cluster Eval Suite[/bold]"))
        console.print()

        with console.status("[bold green]Running 10 eval cases...", spinner="dots"):
            data = run_evals()

        results = data["results"]
        totals  = data["totals"]

        tbl = Table(
            title=f"Multi-Cluster Eval Results  [dim]({totals['cases']} cases)[/dim]",
            show_lines=True, header_style="bold cyan", border_style="dim",
        )
        tbl.add_column("ID",       width=10)
        tbl.add_column("Scenario", max_width=40, no_wrap=True)
        tbl.add_column("Detail",   min_width=30)
        tbl.add_column("Score",    width=8, justify="center")
        tbl.add_column("Result",   width=8, justify="center")

        for r in results:
            status = "[bold green]PASS[/bold green]" if r["passed"] else "[bold red]FAIL[/bold red]"
            tbl.add_row(
                r["id"], r["scenario"][:38],
                f"[dim]{_escape(r['detail'])}[/dim]",
                f"{r['score']}/100", status,
            )

        console.print(tbl)
        console.print()

        n   = totals["cases"]
        pct = round(totals["passed"] / n * 100) if n else 0
        col = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"
        avg = round(totals["total_score"] / n) if n else 0
        console.print(f"  Pass rate: [{col}]{totals['passed']}/{n} ({pct}%)[/{col}]")
        console.print(f"  Avg score: [cyan]{avg}/100[/cyan]")
        console.print()

        if pct < 80:
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@eval_app.command("cost")
def eval_cost() -> None:
    """Run Cost Analysis eval suite (no live cluster needed)."""
    try:
        from evals.cost.runner import run_evals  # type: ignore[import]

        console.print()
        console.print(Rule("[bold]Cost Analysis Eval Suite[/bold]"))
        console.print()

        with console.status("[bold green]Running cost eval cases...", spinner="dots"):
            data = run_evals()

        results = data["results"]
        totals  = data["totals"]

        tbl = Table(
            title=f"Cost Eval Results  [dim]({totals['cases']} cases)[/dim]",
            show_lines=True, header_style="bold cyan", border_style="dim",
        )
        tbl.add_column("ID",       width=10)
        tbl.add_column("Scenario", max_width=40, no_wrap=True)
        tbl.add_column("Detail",   min_width=30)
        tbl.add_column("Score",    width=8, justify="center")
        tbl.add_column("Result",   width=8, justify="center")

        for r in results:
            status = "[bold green]PASS[/bold green]" if r["passed"] else "[bold red]FAIL[/bold red]"
            tbl.add_row(
                r["id"], r["scenario"][:38],
                f"[dim]{_escape(r['detail'])}[/dim]",
                f"{r['score']}/100", status,
            )

        console.print(tbl)
        console.print()

        n   = totals["cases"]
        pct = round(totals["passed"] / n * 100) if n else 0
        col = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"
        avg = round(totals["total_score"] / n) if n else 0
        console.print(f"  Pass rate: [{col}]{totals['passed']}/{n} ({pct}%)[/{col}]")
        console.print(f"  Avg score: [cyan]{avg}/100[/cyan]")
        console.print()

        if pct < 80:
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent eval logs
# ---------------------------------------------------------------------------

@eval_app.command("logs")
def eval_logs() -> None:
    """Run Log Analysis eval suite (no live cluster needed)."""
    try:
        from evals.logs.runner import run_evals  # type: ignore[import]

        console.print()
        console.print(Rule("[bold]Log Analysis Eval Suite[/bold]"))
        console.print()

        with console.status("[bold green]Running log analysis eval cases...", spinner="dots"):
            data = run_evals()

        results = data["results"]
        totals  = data["totals"]

        tbl = Table(
            title=f"Log Analysis Eval Results  [dim]({totals['cases']} cases)[/dim]",
            show_lines=True, header_style="bold cyan", border_style="dim",
        )
        tbl.add_column("ID",       width=10)
        tbl.add_column("Scenario", max_width=40, no_wrap=True)
        tbl.add_column("Detail",   min_width=30)
        tbl.add_column("Score",    width=8, justify="center")
        tbl.add_column("Result",   width=8, justify="center")

        for r in results:
            status = "[bold green]PASS[/bold green]" if r["passed"] else "[bold red]FAIL[/bold red]"
            tbl.add_row(
                r["id"], r["scenario"][:38],
                f"[dim]{_escape(r['detail'])}[/dim]",
                f"{r['score']}/100", status,
            )

        console.print(tbl)
        console.print()

        n   = totals["cases"]
        pct = round(totals["passed"] / n * 100) if n else 0
        col = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"
        avg = round(totals["total_score"] / n) if n else 0
        console.print(f"  Pass rate: [{col}]{totals['passed']}/{n} ({pct}%)[/{col}]")
        console.print(f"  Avg score: [cyan]{avg}/100[/cyan]")
        console.print()

        if pct < 80:
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@eval_app.command("slack")
def eval_slack() -> None:
    """Run Slack deduplication and formatting evals (no real Slack messages sent)."""
    try:
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "evals/slack/runner.py"],
            capture_output=False,
        )
        if result.returncode != 0:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@eval_app.command("deployment")
def eval_deployment() -> None:
    """Run Deployment Management eval suite (no live cluster changes made)."""
    try:
        from evals.deployment.runner import run_evals  # type: ignore[import]

        console.print()
        console.print(Rule("[bold]Deployment Management Eval Suite[/bold]"))
        console.print()

        with console.status("[bold green]Running 8 eval cases...", spinner="dots"):
            data = run_evals()

        results = data["results"]
        totals  = data["totals"]

        tbl = Table(
            title=f"Deployment Eval Results  [dim]({totals['cases']} cases)[/dim]",
            show_lines=True, header_style="bold cyan", border_style="dim",
        )
        tbl.add_column("ID",       width=10)
        tbl.add_column("Scenario", max_width=38, no_wrap=True)
        tbl.add_column("Detail",   min_width=30)
        tbl.add_column("Score",    width=8, justify="center")
        tbl.add_column("Result",   width=8, justify="center")

        for r in results:
            status = "[bold green]PASS[/bold green]" if r["passed"] else "[bold red]FAIL[/bold red]"
            tbl.add_row(
                r["id"], r["scenario"][:36],
                f"[dim]{_escape(r['detail'])}[/dim]",
                f"{r['score']}/100", status,
            )

        console.print(tbl)
        console.print()

        n   = totals["cases"]
        pct = round(totals["passed"] / n * 100) if n else 0
        col = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"
        avg = round(totals["total_score"] / n) if n else 0
        console.print(f"  Pass rate: [{col}]{totals['passed']}/{n} ({pct}%)[/{col}]")
        console.print(f"  Avg score: [cyan]{avg}/100[/cyan]")
        console.print()

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
    fix:   bool = typer.Option(False, "--fix",   help="Apply fix commands for each issue found."),
    slack: bool = typer.Option(False, "--slack",  help="Send new pod failures to Slack."),
) -> None:
    """Scan the cluster for all 10 production issue types with fix commands."""
    try:
        from agent.skills.k8s import K8sSkill
        from agent.skills._fix_runner import apply_shell_fix

        skill = K8sSkill()
        console.print()

        with console.status("[bold cyan]Scanning cluster (10 checks)...", spinner="dots"):
            issues = skill.full_cluster_scan(namespace)

        console.print()

        if not issues:
            console.print(Panel(
                "[bold green]All clear![/bold green]  No issues detected across all 10 checks.",
                border_style="green",
            ))
            console.print()
            return

        # Slack: alert on new pod/node issues
        if slack:
            from agent.integrations.slack import send_alert_generic, is_configured as slack_ok
            from agent.integrations.alert_dedup import AlertDeduplicator, make_fingerprint
            dedup = AlertDeduplicator()
            if slack_ok():
                sent = 0
                for iss in issues:
                    if iss.get("severity") not in ("critical", "warning"):
                        continue
                    fp = make_fingerprint(
                        iss.get("resource", "unknown"),
                        iss.get("namespace", ""),
                        iss.get("problem_type", iss.get("category", "K8S")),
                    )
                    sev = iss.get("severity", "warning")
                    if dedup.should_send(fp, sev):
                        ok = send_alert_generic(
                            title=f"Pod issue: {iss.get('resource', 'unknown')}",
                            message=iss.get("root_cause", iss.get("description", "")),
                            severity=sev,
                            fields={
                                "Namespace": iss.get("namespace", "—"),
                                "Type":      iss.get("problem_type", iss.get("category", "—")),
                            },
                            fix_command=iss.get("fix_command"),
                        )
                        if ok:
                            dedup.record_sent(fp, sev)
                            sent += 1
                if sent:
                    console.print(f"  [green]Slack:[/green] {sent} alert(s) sent")
                else:
                    console.print("  [dim]Slack: no new alerts (all deduplicated)[/dim]")
            else:
                console.print("  [yellow]Slack: webhook not configured (set SLACK_WEBHOOK_URL in .env)[/yellow]")

        _CAT_LABEL = {
            "pod":        ("Unhealthy Pods",           "bold red"),
            "deployment": ("Failed / Scaled-Down",     "bold red"),
            "node":       ("Node Issues",              "bold red"),
            "probe":      ("Probe Failures",           "bold yellow"),
            "quota":      ("Resource Quota Exceeded",  "bold red"),
            "tls":        ("TLS / Certificate Issues", "bold yellow"),
            "helm":       ("Helm Release Issues",      "bold red"),
        }
        _SEV_COLOR = {"critical": "bold red", "warning": "yellow", "info": "dim"}

        critical_count = sum(1 for i in issues if i["severity"] == "critical")
        warning_count  = sum(1 for i in issues if i["severity"] == "warning")
        console.print(Panel(
            f"[bold red]{critical_count} critical[/bold red]  "
            f"[yellow]{warning_count} warning[/yellow]  "
            f"across {len(issues)} issue(s)",
            title="[bold]Cluster Scan Results[/bold]",
            border_style="red" if critical_count else "yellow",
        ))
        console.print()

        # Group by category and render one table per category
        from collections import defaultdict
        by_cat: dict[str, list[dict]] = defaultdict(list)
        for iss in issues:
            by_cat[iss["category"]].append(iss)

        pod_issues_for_diagnose = []
        for cat in ("pod", "deployment", "node", "probe", "quota", "tls", "helm"):
            cat_issues = by_cat.get(cat, [])
            if not cat_issues:
                continue
            label, header_color = _CAT_LABEL.get(cat, (cat.upper(), "bold"))

            tbl = Table(
                title=f"[{header_color}]{label} ({len(cat_issues)})[/{header_color}]",
                show_lines=True,
                header_style="bold cyan",
                border_style="dim",
            )
            tbl.add_column("Resource",    max_width=32, no_wrap=True)
            tbl.add_column("Namespace",   width=16, no_wrap=True)
            tbl.add_column("Severity",    width=10, justify="center")
            tbl.add_column("Description", min_width=36)
            tbl.add_column("Fix Command", min_width=40)

            for iss in cat_issues:
                sev_color = _SEV_COLOR.get(iss["severity"], "white")
                tbl.add_row(
                    f"[bold]{_escape(iss['resource'])}[/bold]",
                    _escape(iss["namespace"] or "—"),
                    f"[{sev_color}]{iss['severity'].upper()}[/{sev_color}]",
                    _escape(iss["description"]),
                    f"[cyan]{_escape(iss['fix_command'] or '—')}[/cyan]",
                )
                if cat == "pod" and iss.get("fix_command") and "describe" in iss["fix_command"]:
                    pod_issues_for_diagnose.append(iss)

            console.print(tbl)
            console.print()

            # Apply fixes if --fix flag set
            if fix:
                fixable = [i for i in cat_issues if i.get("fix_command") and "describe" not in i["fix_command"]]
                if fixable:
                    console.print(f"  [bold cyan]Applying {len(fixable)} fix(es) for {label}...[/bold cyan]")
                    for iss in fixable:
                        console.print(f"  [dim]→[/dim] {_escape(iss['fix_command'])}")
                        result = apply_shell_fix(iss["fix_command"])
                        icon = "[green]✓[/green]" if result == "ok" else "[red]✗[/red]"
                        console.print(f"  {icon} {iss['resource']}")
                    console.print()

        if pod_issues_for_diagnose and not fix:
            first = pod_issues_for_diagnose[0]
            console.print(
                f"  [dim]Deep-dive:[/dim] [bold]agent k8s diagnose "
                f"{first['resource']} -n {first['namespace']}[/bold]"
            )
            console.print()

        if not fix and any(
            i.get("fix_command") and "describe" not in i.get("fix_command", "")
            for i in issues
        ):
            console.print(
                "  [dim]Auto-fix all:[/dim] [bold]agent k8s scan --fix[/bold]"
            )
            console.print()

        console.print("[dim]Tip: run [bold]agent k8s logs <pod>[/bold] to see why a pod is failing[/dim]")

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Command: agent k8s logs POD
# ---------------------------------------------------------------------------

@k8s_app.command("logs")
def k8s_logs(
    pod: str = typer.Argument(..., help="Pod name (partial match supported)"),
    namespace: str = typer.Option("", "--namespace", "-n", help="Namespace (auto-detects if omitted)"),
    lines: int = typer.Option(200, "--lines", "-l", help="Log lines to fetch per container", show_default=True),
    raw: bool = typer.Option(False, "--raw", help="Show raw logs without AI analysis"),
    container: str = typer.Option("", "--container", "-c", help="Specific container name"),
) -> None:
    """Fetch pod logs and AI-identify the root cause of failures."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.log_analysis import LogAnalysisSkill
        from agent.integrations.kubectl import get_pods, get_pod_logs_smart

        console.print()

        # --- Namespace auto-detection ---
        resolved_ns  = namespace
        resolved_pod = pod
        if not resolved_ns:
            with console.status("[dim]Searching all namespaces for pod...[/dim]", spinner="dots"):
                all_pods = get_pods("all")
            matches = [p for p in all_pods if pod in p.name]
            if not matches:
                # Fuzzy suggestions
                suggestions = [p.name for p in all_pods if pod.split("-")[0] in p.name][:5]
                console.print(f"[red]No pod matching '{_escape(pod)}' found.[/red]")
                if suggestions:
                    console.print(f"[dim]Did you mean: {', '.join(_escape(s) for s in suggestions)}[/dim]")
                raise typer.Exit(1)
            if len(matches) == 1:
                resolved_pod = matches[0].name
                resolved_ns  = matches[0].namespace
                console.print(f"[dim]Found: {_escape(resolved_ns)}/{_escape(resolved_pod)}[/dim]")
            else:
                console.print(f"[yellow]Multiple matches for '{_escape(pod)}':[/yellow]")
                for i, p in enumerate(matches[:10], 1):
                    console.print(f"  {i}. {_escape(p.namespace)}/{_escape(p.name)}  [{_escape(p.status)}]")
                console.print("[dim]Use -n <namespace> to specify.[/dim]")
                raise typer.Exit(1)

        console.print(Rule(f"[bold cyan]Log Analysis: {_escape(resolved_ns)}/{_escape(resolved_pod)}[/bold cyan]"))
        console.print()

        # --- Raw mode ---
        if raw:
            with console.status("[dim]Fetching logs...[/dim]", spinner="dots"):
                pod_logs = get_pod_logs_smart(
                    resolved_pod, resolved_ns, lines,
                    container or None,
                )
            if pod_logs.fetch_errors:
                for e in pod_logs.fetch_errors:
                    console.print(f"[yellow]{_escape(e)}[/yellow]")
            for cname, log_text in pod_logs.current_logs.items():
                console.print(Rule(f"[dim]{_escape(cname)} (current)[/dim]"))
                console.print(log_text)
            for cname, log_text in pod_logs.previous_logs.items():
                console.print(Rule(f"[yellow]{_escape(cname)} (previous/crash)[/yellow]"))
                console.print(log_text)
            _cost_footer(get_session_total())
            return

        # --- Full AI analysis ---
        skill = LogAnalysisSkill()

        # Progress updates
        with console.status("[bold green]Fetching logs and container states...", spinner="dots"):
            from agent.integrations.kubectl import get_container_states
            pod_logs   = get_pod_logs_smart(resolved_pod, resolved_ns, lines, container or None)
            con_states = get_container_states(resolved_pod, resolved_ns)

        # Show fetch summary
        for s in con_states:
            exit_str = f", exit={s.exit_code}" if s.exit_code is not None else ""
            reason   = f" ({_escape(s.reason)})" if s.reason else ""
            state_col = "red" if s.state in ("waiting", "terminated") else "green"
            console.print(
                f"  [dim]Container:[/dim] [bold]{_escape(s.name)}[/bold]  "
                f"[{state_col}]{_escape(s.state)}{reason}[/{state_col}]  "
                f"[dim]restarts={s.restart_count}{exit_str}[/dim]"
            )

        cur_total  = sum(len(v.splitlines()) for v in pod_logs.current_logs.values())
        prev_total = sum(len(v.splitlines()) for v in pod_logs.previous_logs.values())
        console.print(f"  [dim]Current logs: {cur_total} lines  |  "
                      f"Previous (crash) logs: {prev_total} lines[/dim]")

        if pod_logs.fetch_errors:
            for e in pod_logs.fetch_errors:
                console.print(f"  [yellow]{_escape(e)}[/yellow]")

        # Handle pending / no logs
        if not pod_logs.current_logs and not pod_logs.previous_logs:
            console.print()
            if any("pending" in e.lower() for e in pod_logs.fetch_errors):
                console.print(Panel(
                    "[yellow]Pod is Pending — no logs yet.[/yellow]\n\n"
                    f"Run:  [bold]kubectl describe pod {_escape(resolved_pod)} -n {_escape(resolved_ns)}[/bold]\n"
                    "to see scheduling events and why it hasn't started.",
                    title="[yellow]Pod Not Started[/yellow]", border_style="yellow",
                ))
            else:
                console.print("[dim]No logs found.[/dim]")
            _cost_footer(get_session_total())
            return

        console.print()
        with console.status("[bold green]Analysing with Claude...", spinner="dots"):
            from agent.integrations.kubectl import extract_error_patterns
            all_text = "\n".join(list(pod_logs.current_logs.values()) +
                                  list(pod_logs.previous_logs.values()))
            patterns = extract_error_patterns(all_text)
            analysis = skill._call_claude(
                resolved_pod, resolved_ns, pod_logs, con_states, patterns, ""
            )

        # --- Error type color ---
        _ERR_COLOR = {
            "OOM":        "bold red",
            "NETWORK":    "orange1",
            "CONFIG":     "yellow",
            "PERMISSION": "magenta",
            "CRASH":      "bold red",
            "UNKNOWN":    "dim",
        }
        _CONF_COLOR = {"high": "bold green", "medium": "yellow", "low": "dim"}
        err_col  = _ERR_COLOR.get(analysis.error_type, "white")
        conf_col = _CONF_COLOR.get(analysis.confidence, "white")

        # --- Main analysis panel ---
        # Header: container info
        header_lines = []
        for s in con_states:
            exit_str = f"  Exit Code: {s.exit_code}" if s.exit_code is not None else ""
            reason   = f"  Reason: {_escape(s.reason)}" if s.reason else ""
            header_lines.append(
                f"  Container:  [bold]{_escape(s.name)}[/bold]  "
                f"state={_escape(s.state)}  restarts={s.restart_count}{exit_str}{reason}"
            )
        header_lines.append(f"  Error Type: [{err_col}]{analysis.error_type}[/{err_col}]")
        header_lines.append(f"  Confidence: [{conf_col}]{analysis.confidence.upper()}[/{conf_col}]")
        if pod_logs.has_previous:
            header_lines.append("  [dim]^ Crash logs available (used for analysis)[/dim]")

        console.print(Panel(
            "\n".join(header_lines),
            title=f"[bold cyan]LOG ANALYSIS: {_escape(resolved_ns)}/{_escape(resolved_pod)}[/bold cyan]",
            border_style="cyan", padding=(0, 1),
        ))
        console.print()

        # Root cause
        console.print(Panel(
            f"[bold]{_escape(analysis.root_cause)}[/bold]",
            title="[bold red]ROOT CAUSE[/bold red]",
            border_style="red", padding=(0, 1),
        ))
        console.print()

        # Smoking gun lines
        if analysis.key_log_lines:
            lines_text = "\n".join(f"  [bold yellow]>[/bold yellow] {_escape(ln)}"
                                    for ln in analysis.key_log_lines)
            console.print(Panel(
                lines_text,
                title="[bold yellow]SMOKING GUN (key log lines)[/bold yellow]",
                border_style="yellow", padding=(0, 1),
            ))
            console.print()

        # Explanation
        if analysis.explanation:
            console.print(Panel(
                _escape(analysis.explanation),
                title="[bold]EXPLANATION[/bold]",
                border_style="dim", padding=(0, 1),
            ))
            console.print()

        # Fix
        if analysis.suggested_fix:
            fix_content = _escape(analysis.suggested_fix)
            if analysis.fix_command:
                fix_content += f"\n\n  [bold cyan]{_escape(analysis.fix_command)}[/bold cyan]"
            console.print(Panel(
                fix_content,
                title="[bold green]SUGGESTED FIX[/bold green]",
                border_style="green", padding=(0, 1),
            ))
            console.print()

        # Apply prompt
        if analysis.fix_command:
            apply = typer.confirm("Apply this fix?", default=False)
            if apply:
                from agent.integrations.kubectl import run_kubectl
                import shlex
                cmd_parts = shlex.split(analysis.fix_command)
                if cmd_parts and cmd_parts[0] == "kubectl":
                    cmd_parts = cmd_parts[1:]
                fix_result = run_kubectl(cmd_parts)
                if fix_result.success:
                    console.print("[bold green]Fix applied successfully.[/bold green]")
                else:
                    console.print(f"[red]Fix failed: {_escape(fix_result.error[:200])}[/red]")
            console.print()

        _cost_footer(get_session_total())

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
        from datetime import timezone, timedelta
        dt   = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(timezone.utc)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return "just now"


_IST = None  # lazy init


def _fmt_revision_time(iso_str: str) -> str:
    """Two-line cell: relative age on top, IST date+time below."""
    if not iso_str:
        return "[dim]—[/dim]"
    try:
        from datetime import timezone, timedelta
        global _IST
        if _IST is None:
            _IST = timezone(timedelta(hours=5, minutes=30))
        dt_utc = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(timezone.utc)
        dt_ist = dt_utc.astimezone(_IST)
        secs   = int((datetime.now(timezone.utc) - dt_utc).total_seconds())
        if secs < 60:
            rel = f"{secs}s ago"
        elif secs < 3600:
            rel = f"{secs // 60}m ago"
        elif secs < 86400:
            rel = f"{secs // 3600}h {(secs % 3600) // 60}m ago"
        else:
            days = secs // 86400
            rel  = f"{days}d ago"
        # e.g. "12 Jun, 03:45 PM IST"
        date_str = dt_ist.strftime("%d %b, %I:%M %p") + " IST"
        return f"{rel}\n[dim]{date_str}[/dim]"
    except Exception:
        return "[dim]—[/dim]"


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
    "deployments": "bold red",
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
# Security Audit commands
# ---------------------------------------------------------------------------

@k8s_app.command("security")
def k8s_security(
    namespace:       str  = typer.Option("all", "--namespace", "-n", show_default=True),
    severity:        str  = typer.Option("all", "--severity", "-s",
                                         help="Filter: critical / high / all",
                                         show_default=True),
    finding:         str  = typer.Option("", "--finding",
                                         help="Deep-dive into one finding ID, e.g. C1"),
    fix:             bool = typer.Option(False, "--fix",
                                         help="Apply fix commands with y/N prompts."),
    show_all:        bool = typer.Option(False, "--show-all",
                                         help="Show system components and expected findings too."),
    slack:           bool = typer.Option(False, "--slack",
                                         help="Send new critical findings to Slack."),
) -> None:
    """
    Audit the cluster for real security issues.

    Default view shows only ACTIONABLE findings — system components
    (aws-node, ebs-csi, kube-proxy, ingress-nginx) are filtered out.
    Use --show-all to see everything labelled [REAL] or [SYSTEM].
    """
    try:
        from rich.rule import Rule
        from agent.skills.security import SecurityAuditSkill

        skill = SecurityAuditSkill()
        console.print()

        with console.status("[bold cyan]Running security audit (6 checks)...", spinner="dots"):
            report = skill.run_audit(namespace)

        # Slack: alert on new critical/high findings
        if slack:
            from agent.integrations.slack import send_alert_generic, is_configured as slack_ok
            from agent.integrations.alert_dedup import AlertDeduplicator, make_fingerprint
            dedup = AlertDeduplicator()
            if slack_ok():
                sent = 0
                for f in report.findings:
                    if not f.actionable or f.severity not in ("critical", "high"):
                        continue
                    fp = make_fingerprint(f.resource, f.namespace or "", f.check_type)
                    sev = "critical" if f.severity == "critical" else "warning"
                    if dedup.should_send(fp, sev):
                        ok = send_alert_generic(
                            title=f"Security: {f.title}",
                            message=f.description,
                            severity=sev,
                            fields={
                                "Resource":  f.resource,
                                "Namespace": f.namespace or "—",
                                "Check":     f.check_type,
                            },
                            fix_command=f.fix_command,
                        )
                        if ok:
                            dedup.record_sent(fp, sev)
                            sent += 1
                # Summary alert
                total_crit = sum(1 for f in report.findings if f.severity == "critical" and f.actionable)
                if total_crit:
                    send_alert_generic(
                        title=f"Security scan complete — {total_crit} critical finding(s)",
                        message=f"Score: {report.security_score}/100. New alerts sent: {sent}.",
                        severity="warning",
                    )
                if sent:
                    console.print(f"  [green]Slack:[/green] {sent} new finding(s) sent")
                else:
                    console.print("  [dim]Slack: no new findings (all deduplicated)[/dim]")
            else:
                console.print("  [yellow]Slack: webhook not configured (set SLACK_WEBHOOK_URL in .env)[/yellow]")

        # ── Score header ──────────────────────────────────────────────
        score = report.security_score
        if score >= 86:
            score_color, score_label = "bold green",  "PRODUCTION READY"
        elif score >= 71:
            score_color, score_label = "bold yellow", "MEDIUM RISK — Fix before production"
        elif score >= 51:
            score_color, score_label = "bold orange1","HIGH RISK — Needs immediate attention"
        else:
            score_color, score_label = "bold red",    "CRITICAL — DO NOT GO TO PRODUCTION"

        prod_icon = (
            "[bold green]READY[/bold green]"
            if report.production_ready else
            "[bold red]NOT READY[/bold red]"
        )
        actionable = (
            report.critical + report.high + report.medium + report.low
        )

        console.print(Panel(
            f"  [bold]Cluster:[/bold]  {_escape(report.cluster_name)}\n"
            f"  [bold]Score:[/bold]    [{score_color}]{score}/100  {score_label}[/{score_color}]\n"
            f"  [bold]Status:[/bold]   Production {prod_icon}\n\n"
            f"  [bold cyan]ACTIONABLE FINDINGS (fix these)[/bold cyan]\n"
            f"  [bold red]{report.critical} critical[/bold red]  "
            f"[bold orange1]{report.high} high[/bold orange1]  "
            f"[yellow]{report.medium} medium[/yellow]  "
            f"[dim]{report.low} low[/dim]"
            + (f"  [dim]({actionable} total)[/dim]" if actionable else "  [green]none[/green]")
            + f"\n\n  [dim]SYSTEM COMPONENTS (expected — ignored in score)[/dim]\n"
            f"  [dim]AWS/K8s managed: {report.system_components}   "
            f"({report.total_findings} total scanned)[/dim]\n\n"
            f"  [dim]Run:[/dim]  agent k8s security --show-all       "
            f"[dim](see everything)[/dim]\n"
            f"  [dim]      agent k8s security --fix          "
            f"[dim](apply fixes)[/dim]",
            title="[bold]SECURITY AUDIT REPORT[/bold]",
            border_style="red" if score < 71 else ("yellow" if score < 86 else "green"),
            padding=(0, 2),
        ))
        console.print()

        # ── Deep-dive into single finding ─────────────────────────────
        if finding:
            target = next((f for f in report.findings if f.id == finding.upper()), None)
            if not target:
                console.print(f"[red]Finding '{finding}' not found.[/red]")
                raise typer.Exit(1)

            _SEV_COLOR_MAP = {
                "critical": "bold red", "high": "bold orange1",
                "medium": "yellow", "low": "dim",
                "info": "cyan", "system_component": "dim",
            }
            sev_col = _SEV_COLOR_MAP.get(target.severity, "white")
            label   = (
                "[dim][SYSTEM — EXPECTED][/dim]"
                if target.category == "system_component" else
                "[bold green][REAL ISSUE][/bold green]"
            )
            console.print(Panel(
                f"[{sev_col}]{target.severity.upper()}[/{sev_col}]  "
                f"[bold]{_escape(target.title)}[/bold]  {label}\n\n"
                f"[bold]Category:[/bold]  {target.category}\n"
                f"[bold]Resource:[/bold]  {_escape(target.namespace)}/{_escape(target.affected_resource)}\n\n"
                f"[bold]Description:[/bold]\n  {_escape(target.description)}\n\n"
                f"[bold]Evidence:[/bold]\n  [dim]{_escape(target.evidence)}[/dim]\n\n"
                f"[bold]Recommendation:[/bold]\n  {_escape(target.recommendation)}"
                + (f"\n\n[bold]CVE:[/bold] {_escape(target.cve_reference)}"
                   if target.cve_reference else ""),
                title=f"[bold]Finding {target.id}[/bold]",
                border_style=(
                    "dim" if target.category == "system_component"
                    else "red" if target.severity == "critical"
                    else "yellow"
                ),
                padding=(0, 2),
            ))
            if skill.is_fixable(target):
                if target.fix_command:
                    fix_display = f"[bold green]{_escape(target.fix_command)}[/bold green]"
                    panel_title = "[bold green]Fix Command[/bold green]"
                elif target.category == "network":
                    fix_display = "[bold green]Apply default-deny NetworkPolicy via kubectl apply[/bold green]"
                    panel_title = "[bold green]Auto-Fix Available[/bold green]"
                else:
                    fix_display = "[bold green]Remove privileged flag from owning Deployment/DaemonSet[/bold green]"
                    panel_title = "[bold green]Auto-Fix Available[/bold green]"
                console.print(Panel(fix_display, title=panel_title,
                                    border_style="green", padding=(0, 2)))
                if fix:
                    if typer.confirm(f"  Apply fix for {target.id}?", default=False):
                        ok   = skill.apply_security_fix(target)
                        icon = "[green]✓ applied[/green]" if ok else "[red]✗ failed[/red]"
                        console.print(f"  {icon}")
            elif target.fix_command:
                console.print(Panel(
                    f"[bold yellow]{_escape(target.fix_command)}[/bold yellow]\n"
                    f"[dim](Fill in <value> before running — cannot auto-apply)[/dim]",
                    title="[bold yellow]Manual Fix Required[/bold yellow]",
                    border_style="yellow", padding=(0, 2),
                ))
            return

        # ── Filter findings based on view mode ────────────────────────
        _SEV_ORDER  = ["critical", "high", "medium", "low", "info"]
        _SEV_COLOR  = {"critical": "bold red",    "high": "bold orange1",
                       "medium":   "yellow",       "low":  "dim",
                       "info":     "cyan"}
        _SEV_BORDER = {"critical": "red",          "high": "dark_orange",
                       "medium":   "yellow",        "low":  "dim",
                       "info":     "cyan"}

        if show_all:
            visible = report.findings
        else:
            # Actionable-only: hide system_component findings
            visible = [
                f for f in report.findings
                if f.category != "system_component"
            ]

        if not visible:
            msg = (
                "[bold green]No actionable findings — cluster looks clean![/bold green]\n"
                f"[dim]{report.system_components} system components scanned and "
                f"confirmed expected (--show-all to see them)[/dim]"
            )
            console.print(Panel(msg, border_style="green"))
            return

        show_sevs = (
            ["critical", "high"] if severity == "critical"
            else [severity]      if severity in _SEV_ORDER
            else _SEV_ORDER
        )

        for sev in show_sevs:
            group = [f for f in visible if f.severity == sev]
            if not group:
                continue

            sc = _SEV_COLOR[sev]
            label_suffix = " [ACTIONABLE]" if sev not in ("info",) else " [EXPECTED]"
            tbl = Table(
                title=f"[{sc}]{sev.upper()} FINDINGS ({len(group)}){label_suffix}[/{sc}]",
                show_lines=True, header_style="bold cyan",
                border_style=_SEV_BORDER[sev],
            )
            tbl.add_column("ID",       width=5,  justify="center")
            tbl.add_column("Category", width=14)
            tbl.add_column("Finding",  min_width=34)
            tbl.add_column("Resource", min_width=26)
            tbl.add_column("Fix",      width=6,  justify="center")

            for f in group:
                fixable   = skill.is_fixable(f)
                is_system = f.category == "system_component"
                row_label = "[dim][SYS][/dim]" if is_system else ""
                tbl.add_row(
                    f"[{sc}][bold]{_escape(f.id)}[/bold][/{sc}]",
                    _escape(f.category),
                    f"{row_label} {_escape(f.title[:58])}".strip(),
                    f"[dim]{_escape(f.namespace)}[/dim]/{_escape(f.affected_resource[:28])}",
                    "[green]✓[/green]" if fixable else "[dim]—[/dim]",
                )
                if fix and fixable:
                    if f.fix_command:
                        tbl.add_row("", "", "", f"[dim]{_escape(f.fix_command[:70])}[/dim]", "")
                    elif f.category == "network":
                        tbl.add_row("", "", "", "[dim]→ apply default-deny NetworkPolicy[/dim]", "")
                    elif f.category == "privilege":
                        tbl.add_row("", "", "", "[dim]→ patch Deployment/DaemonSet[/dim]", "")

            console.print(tbl)
            console.print()

        # ── AI analysis — only for actionable findings ────────────────
        if report.claude_summary and "unavailable" not in report.claude_summary:
            real_count = report.critical + report.high + report.medium + report.low
            if real_count > 0:
                console.print(Panel(
                    f"[cyan]{_escape(report.claude_summary)}[/cyan]",
                    title="[bold cyan]AI Security Analysis — Actionable Issues[/bold cyan]",
                    border_style="cyan", padding=(0, 2),
                ))
                console.print()

        # ── Fix prompts ───────────────────────────────────────────────
        if fix:
            fixable_list = [f for f in visible if skill.is_fixable(f)]
            if fixable_list:
                console.print(Rule("[dim]Applying Fixes[/dim]", style="dim"))
                for f in fixable_list:
                    console.print(
                        f"\n  [{_SEV_COLOR.get(f.severity,'white')}]{f.id}[/] "
                        f"{_escape(f.title)}"
                    )
                    if f.fix_command:
                        console.print(f"  [bold green]{_escape(f.fix_command)}[/bold green]")
                    elif f.category == "network":
                        console.print("  [dim]→ apply default-deny NetworkPolicy (via tempfile)[/dim]")
                    elif f.category == "privilege":
                        console.print("  [dim]→ patch Deployment/DaemonSet: remove privileged flag[/dim]")
                    if typer.confirm("  Apply?", default=False):
                        ok = skill.apply_security_fix(f)
                        console.print(
                            "  [green]✓ applied[/green]" if ok
                            else "  [red]✗ failed[/red]"
                        )
            manual = [
                f for f in visible
                if not skill.is_fixable(f)
                and f.fix_command
                and f.category != "system_component"
            ]
            if manual:
                console.print()
                console.print(Rule("[dim]Manual Fixes Required[/dim]", style="dim"))
                for f in manual:
                    console.print(
                        f"\n  [{_SEV_COLOR.get(f.severity,'white')}]{f.id}[/] "
                        f"{_escape(f.title)}"
                    )
                    console.print(f"  [bold yellow]{_escape(f.fix_command)}[/bold yellow]")
                    console.print("  [dim](Fill in <value> before running)[/dim]")
        else:
            auto_fixable = sum(1 for f in visible if skill.is_fixable(f))
            if auto_fixable:
                console.print(
                    f"  [dim]Deep-dive:[/dim]  agent k8s security --finding C1\n"
                    f"  [dim]Auto-fix:[/dim]   agent k8s security --fix"
                    f"  [dim]({auto_fixable} fixable)[/dim]\n"
                )

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Multi-cluster commands
# ---------------------------------------------------------------------------

@k8s_app.command("contexts")
def k8s_contexts() -> None:
    """List all configured Kubernetes clusters with health status."""
    try:
        from agent.skills.multi_cluster import MultiClusterSkill

        skill = MultiClusterSkill()
        console.print()

        with console.status("[bold cyan]Checking cluster contexts...", spinner="dots"):
            clusters = skill.list_clusters(check_health=True)

        if not clusters:
            console.print(Panel(
                "[yellow]No Kubernetes contexts found.[/yellow]\n"
                "[dim]Add a cluster with: agent k8s add-cluster[/dim]",
                border_style="yellow",
            ))
            return

        current = next((c for c in clusters if c.is_current), None)
        curr_name = current.name if current else "none"

        console.print(Panel(
            f"  [bold]{len(clusters)}[/bold] cluster(s) configured  |  "
            f"Current: [bold cyan]{_escape(curr_name)}[/bold cyan]",
            title="[bold]KUBERNETES CLUSTERS[/bold]",
            border_style="cyan", padding=(0, 2),
        ))
        console.print()

        _ENV_COLOR  = {"production": "bold red", "staging": "yellow",
                       "development": "bold green", "testing": "cyan", "unknown": "dim"}
        _PROV_ICON  = {"aws": "AWS", "gcp": "GCP", "azure": "AZ", "local": "LOCAL", "unknown": "?"}
        _HLTH_ICON  = {"healthy": "[green]✓[/green]", "unreachable": "[red]✗[/red]",
                       "unknown": "[dim]?[/dim]"}

        tbl = Table(show_lines=True, header_style="bold cyan", border_style="dim", padding=(0, 1))
        tbl.add_column("",          width=2,  justify="center")
        tbl.add_column("Context",   min_width=28)
        tbl.add_column("Provider",  width=8,  justify="center")
        tbl.add_column("Region",    width=14)
        tbl.add_column("Env",       width=12)
        tbl.add_column("Nodes",     width=7,  justify="center")
        tbl.add_column("Health",    width=8,  justify="center")

        for c in clusters:
            is_cur   = "►" if c.is_current else " "
            ec       = _ENV_COLOR.get(c.environment, "dim")
            env_lbl  = f"[{ec}]{c.environment.upper()[:8]}[/{ec}]"
            nodes    = str(c.node_count) if c.node_count > 0 else "—"
            hlth     = _HLTH_ICON.get(c.health, "?")
            prov     = _PROV_ICON.get(c.cloud_provider, "?")

            tbl.add_row(
                f"[bold cyan]{is_cur}[/bold cyan]",
                f"[bold]{_escape(c.name)}[/bold]" if c.is_current else _escape(c.name),
                prov, _escape(c.region or "—"),
                env_lbl, nodes, hlth,
            )

        console.print(tbl)
        console.print()
        console.print("  [dim]Switch:[/dim]  [bold]agent k8s switch <name-or-env>[/bold]")
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@k8s_app.command("switch")
def k8s_switch(
    target: str = typer.Argument(..., help="Context name or environment (prod/staging/dev)"),
) -> None:
    """Switch active Kubernetes cluster context."""
    try:
        from agent.skills.multi_cluster import MultiClusterSkill
        from agent.integrations.kubectl import get_current_context, get_all_contexts

        skill = MultiClusterSkill()
        console.print()

        current_name = get_current_context()
        all_ctx      = get_all_contexts()
        current_ctx  = next((c for c in all_ctx if c.is_current), None)

        # Resolve target
        target_ctx = next((c for c in all_ctx if c.name == target), None)
        if not target_ctx:
            target_ctx = next(
                (c for c in all_ctx
                 if target.lower() in c.environment.lower()
                 or target.lower() in c.name.lower()),
                None,
            )

        if not target_ctx:
            _print_error(f"No context matching '{target}' found. Run: agent k8s contexts")
            raise typer.Exit(1)

        if target_ctx.is_current:
            console.print(f"[yellow]Already on context:[/yellow] [bold]{_escape(target_ctx.name)}[/bold]")
            return

        # Show switch summary
        from_env = current_ctx.environment if current_ctx else "unknown"
        to_env   = target_ctx.environment

        _ENV_COL = {"production": "bold red", "staging": "yellow",
                    "development": "bold green", "testing": "cyan"}
        from_col = _ENV_COL.get(from_env, "dim")
        to_col   = _ENV_COL.get(to_env,   "dim")

        console.print(Panel(
            f"  From: [{from_col}]{_escape(current_name)}  ({from_env.upper()})[/{from_col}]\n"
            f"  To:   [{to_col}]{_escape(target_ctx.name)}  ({to_env.upper()})[/{to_col}]",
            title="[bold]Switch Cluster Context[/bold]",
            border_style="yellow", padding=(0, 2),
        ))

        if to_env == "production":
            console.print("[bold red]  ⚠  You are switching TO PRODUCTION[/bold red]")
        if from_env == "production":
            console.print("[bold red]  ⚠  You are leaving PRODUCTION[/bold red]")

        console.print()
        if not typer.confirm("  Confirm switch?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            return

        with console.status("[cyan]Switching context...", spinner="dots"):
            result = skill.switch_cluster(target_ctx.name)

        if result:
            console.print(f"\n  [bold green]✓  Switched to {_escape(result.name)}[/bold green]")
            if result.node_count > 0:
                console.print(f"  Nodes ready: [bold]{result.node_count}[/bold]")
            console.print(f"  Environment: [{to_col}]{to_env.upper()}[/{to_col}]")
        else:
            _print_error("Switch failed — check kubeconfig and cluster connectivity.")
            raise typer.Exit(1)

        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@k8s_app.command("add-cluster")
def k8s_add_cluster(
    name:    str = typer.Option(..., "--name",    help="EKS cluster name"),
    region:  str = typer.Option(..., "--region",  help="AWS region (e.g. us-east-1)"),
    profile: str = typer.Option("default", "--profile", help="AWS CLI profile", show_default=True),
    rename:  str = typer.Option("", "--rename",  help="Rename context after adding"),
) -> None:
    """Add an EKS cluster to your kubeconfig via aws eks update-kubeconfig."""
    try:
        from agent.skills.multi_cluster import MultiClusterSkill

        skill = MultiClusterSkill()
        console.print()
        console.print(Panel(
            f"  Cluster: [bold]{_escape(name)}[/bold]\n"
            f"  Region:  [bold]{_escape(region)}[/bold]\n"
            f"  Profile: [bold]{_escape(profile)}[/bold]",
            title="[bold]Add EKS Cluster[/bold]",
            border_style="cyan", padding=(0, 2),
        ))
        console.print()

        with console.status("[cyan]Running aws eks update-kubeconfig...", spinner="dots"):
            result = skill.add_eks(name, region, profile, rename_to=rename)

        if result["success"]:
            console.print(f"  [bold green]✓  Cluster added[/bold green]")
            console.print(f"  Context: [bold]{_escape(result['context'])}[/bold]")
            if result["node_count"] > 0:
                console.print(f"  Nodes:   [bold]{result['node_count']}[/bold] ready")
            console.print()
            console.print(f"  Switch now: [bold]agent k8s switch {_escape(result['context'])}[/bold]")
        else:
            _print_error(result.get("error", "Failed to add cluster."))
            raise typer.Exit(1)

        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@k8s_app.command("compare")
def k8s_compare(
    from_ctx: str = typer.Option(..., "--from", help="First context name"),
    to_ctx:   str = typer.Option(..., "--to",   help="Second context name"),
) -> None:
    """Compare two Kubernetes clusters side by side with AI analysis."""
    try:
        from agent.skills.multi_cluster import MultiClusterSkill

        skill = MultiClusterSkill()
        console.print()

        with console.status(f"[cyan]Comparing {from_ctx} ↔ {to_ctx}...", spinner="dots"):
            result = skill.compare_clusters(from_ctx, to_ctx)

        s1, s2 = result["ctx1"], result["ctx2"]

        tbl = Table(
            title="[bold]CLUSTER COMPARISON[/bold]",
            show_lines=True, header_style="bold cyan",
            border_style="cyan", padding=(0, 1),
        )
        tbl.add_column("Property",    min_width=18)
        tbl.add_column(_escape(from_ctx), min_width=22, justify="center")
        tbl.add_column(_escape(to_ctx),   min_width=22, justify="center")

        def _row(label: str, v1, v2, good_if_equal: bool = True) -> None:
            s1v, s2v = str(v1), str(v2)
            col = "green" if (v1 == v2 and good_if_equal) else "yellow"
            tbl.add_row(label, f"[{col}]{s1v}[/{col}]", f"[{col}]{s2v}[/{col}]")

        _row("Reachable",    "✓" if s1["reachable"] else "✗", "✓" if s2["reachable"] else "✗")
        _row("Pods",         s1["pod_count"],       s2["pod_count"],       False)
        _row("Namespaces",   s1["namespace_count"], s2["namespace_count"], False)
        _row("K8s Version",  s1["k8s_version"],     s2["k8s_version"])

        console.print(tbl)
        console.print()

        if result["analysis"] and "unavailable" not in result["analysis"]:
            console.print(Panel(
                f"[cyan]{_escape(result['analysis'])}[/cyan]",
                title="[bold cyan]AI Analysis[/bold cyan]",
                border_style="cyan", padding=(0, 2),
            ))
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@k8s_app.command("broadcast")
def k8s_broadcast(
    command: str = typer.Argument(..., help="Read-only kubectl command to run on all clusters"),
) -> None:
    """Run a read-only kubectl command across ALL configured clusters."""
    try:
        from agent.skills.multi_cluster import MultiClusterSkill
        from agent.integrations.kubectl import get_all_contexts

        skill    = MultiClusterSkill()
        contexts = [c.name for c in get_all_contexts()]

        if not contexts:
            console.print("[yellow]No contexts found.[/yellow]")
            return

        console.print()
        console.print(Panel(
            f"  Command:  [bold cyan]{_escape(command)}[/bold cyan]\n"
            f"  Clusters: [bold]{len(contexts)}[/bold]",
            title="[bold]Broadcast Command[/bold]",
            border_style="cyan", padding=(0, 2),
        ))
        console.print()

        with console.status("[cyan]Running on all clusters...", spinner="dots"):
            results = skill.run_readonly_on_all(command, contexts)

        for ctx_name, output in sorted(results.items()):
            is_err = output.startswith("ERROR")
            col    = "red" if is_err else "green"
            console.print(f"  [{col}]{'✗' if is_err else '✓'}[/{col}]  [bold]{_escape(ctx_name)}[/bold]")
            if output.strip():
                for line in output.strip().splitlines()[:10]:
                    console.print(f"    [dim]{_escape(line)}[/dim]")
            console.print()

    except PermissionError as e:
        _print_error(str(e))
        raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Deployment Management helpers + commands
# ---------------------------------------------------------------------------

def _deployment_analyze_and_apply(action, auto_rollback: bool = True) -> None:
    """Show analysis panel, get approval (with prod gate), execute."""
    from agent.skills.deployment import DeploymentSkill

    risk_color = {"low": "green", "medium": "yellow", "high": "bold red"}.get(action.risk_level, "white")
    divider    = "[dim]" + "-" * 56 + "[/dim]"

    lines = [
        f"[dim]Action   :[/dim] [bold]{action.action_type.upper()}[/bold]",
        f"[dim]Namespace:[/dim] [cyan]{_escape(action.namespace)}[/cyan]",
        f"[dim]Current  :[/dim] {_escape(action.current_state or '—')}",
        f"[dim]Proposed :[/dim] [bold]{_escape(action.proposed_state or '—')}[/bold]",
        f"[dim]Risk     :[/dim] [{risk_color}]{action.risk_level.upper()}[/{risk_color}]",
        f"[dim]Downtime :[/dim] {_escape(action.downtime_estimate or 'unknown')}",
    ]

    if action.warnings:
        lines.append(divider)
        lines.append("[bold yellow]WARNINGS[/bold yellow]")
        for w in action.warnings:
            lines.append(f"  [yellow]• {_escape(w)}[/yellow]")

    lines.append(divider)
    lines.append("[bold]AI ANALYSIS[/bold]")
    lines.append(_escape(action.claude_analysis or "No analysis available."))

    if action.command:
        lines.append(divider)
        lines.append(f"[dim]Command  :[/dim] [bold cyan]{_escape(action.command)}[/bold cyan]")

    safe_icon  = "[green]SAFE[/green]" if action.is_safe else "[bold red]UNSAFE[/bold red]"
    border     = "red" if action.risk_level == "high" else ("yellow" if action.risk_level == "medium" else "green")

    console.print(Panel(
        "\n".join(lines),
        title=f"[bold]DEPLOYMENT ANALYSIS[/bold]  {safe_icon}",
        border_style=border,
        padding=(1, 2),
    ))
    console.print()

    if action.is_production:
        console.print(Panel(
            f"[bold red]PRODUCTION NAMESPACE DETECTED:[/bold red] [cyan]{_escape(action.namespace)}[/cyan]\n\n"
            "This operation targets a production environment and cannot be undone quickly.\n"
            f"Type the deployment name [bold yellow]{_escape(action.deployment)}[/bold yellow] to confirm:",
            title="[bold red]⚠  PRODUCTION SAFETY GATE[/bold red]",
            border_style="red",
            padding=(1, 2),
        ))
        typed = typer.prompt("  Confirm deployment name")
        if typed.strip() != action.deployment:
            console.print("[bold red]Name mismatch — operation cancelled.[/bold red]")
            raise typer.Exit(0)
    else:
        apply = typer.confirm("  Apply?", default=False)
        if not apply:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    console.print()
    with console.status(
        f"[bold cyan]Executing {action.action_type}[/bold cyan]"
        + (" (monitoring 60s for auto-rollback)..." if action.action_type == "deploy" and auto_rollback else "..."),
        spinner="dots",
    ):
        success = DeploymentSkill().execute_action(action, auto_rollback=auto_rollback)

    if success:
        console.print(f"[bold green]✓ {action.action_type.capitalize()} succeeded.[/bold green]")
    else:
        console.print(f"[bold red]✗ {action.action_type.capitalize()} failed or auto-rollback fired.[/bold red]")
        console.print("  Check pod health:  [bold]agent k8s scan[/bold]")
        raise typer.Exit(1)

    console.print()


@k8s_app.command("rollback")
def k8s_rollback(
    deployment: str = typer.Argument(..., help="Deployment name to roll back."),
    namespace: str = typer.Option(
        "default", "--namespace", "-n",
        help="Kubernetes namespace.",
        show_default=True,
    ),
    to_revision: Optional[int] = typer.Option(
        None, "--to-revision",
        help="Specific revision number to roll back to (default: previous revision).",
    ),
    watch_seconds: int = typer.Option(
        120, "--watch-seconds",
        help="How long to watch after rollback.", show_default=True,
    ),
    no_auto_rollback: bool = typer.Option(
        False, "--no-auto-rollback",
        help="Skip post-rollback health watcher.",
    ),
) -> None:
    """Roll back a deployment with risk scoring, health gate, live watcher, and report."""
    try:
        import time as _time
        from agent.integrations.kubectl import get_deployment_info, rollout_undo, wait_for_rollout
        from agent.observability.costs import get_session_total
        from agent.skills.deployment import DeploymentSkill

        console.print()
        console.print(Rule(
            f"[bold cyan]Rollback:[/bold cyan] {_escape(deployment)} [dim]({namespace})[/dim]"
        ))
        console.print()

        # Get current state
        with console.status("[cyan]Fetching deployment info...", spinner="dots"):
            info = get_deployment_info(deployment, namespace)

        if info is None:
            _print_error(f"Deployment '{deployment}' not found in namespace '{namespace}'.")
            raise typer.Exit(1)

        current_image = info.current_image
        skill = DeploymentSkill()

        # Score risk
        with console.status("[cyan]Scoring rollback risk...", spinner="dots"):
            risk = skill.score_deployment_risk(
                deployment, namespace,
                new_image=f"{current_image.rsplit(':', 1)[0]}:previous",
                current_image=current_image,
            )

        _print_risk_score(risk)

        # Health gate
        with console.status("[cyan]Running pre-rollback health checks...", spinner="dots"):
            gate = skill.pre_deploy_health_check(deployment, namespace, current_image)

        _print_health_gate(gate)

        if gate.blocked:
            _print_error("Rollback blocked by health gate.")
            raise typer.Exit(1)

        # Production gate / confirm
        if _is_production(namespace):
            console.print(Panel(
                f"[bold red]PRODUCTION NAMESPACE: {_escape(namespace)}[/bold red]\n"
                f"Type the deployment name to confirm rollback:",
                border_style="red", padding=(0, 2),
            ))
            typed = typer.prompt("  Confirm deployment name")
            if typed.strip() != deployment:
                console.print("[bold red]Name mismatch — rollback cancelled.[/bold red]")
                raise typer.Exit(0)
        else:
            if not typer.confirm("  Execute rollback?", default=False):
                console.print("[dim]Cancelled.[/dim]")
                raise typer.Exit(0)

        start = _time.time()
        console.print()
        console.print("[bold cyan]Rolling back...[/bold cyan]")

        with console.status("[bold cyan]Executing rollout undo...", spinner="dots"):
            r = rollout_undo(deployment, namespace, to_revision)

        if not r.success:
            _print_error(f"Rollback failed: {r.error[:200]}")
            raise typer.Exit(1)

        with console.status("[bold cyan]Waiting for rollout to complete...", spinner="dots"):
            wait_for_rollout(deployment, namespace, timeout=120)

        dur_so_far = int(_time.time() - start)

        if not no_auto_rollback:
            _run_watcher(skill, deployment, namespace, current_image, "previous", watch_seconds, risk.total, risk.label, gate, dur_so_far)
        else:
            console.print("[bold green]✓ Rollback complete (watcher disabled).[/bold green]")

        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@k8s_app.command("scale")
def k8s_scale(
    deployment: str = typer.Argument(..., help="Deployment name to scale."),
    replicas: int = typer.Argument(..., help="Target number of replicas (0 = stop all pods)."),
    namespace: str = typer.Option(
        "default", "--namespace", "-n",
        help="Kubernetes namespace.",
        show_default=True,
    ),
) -> None:
    """Scale a deployment with risk scoring and capacity check."""
    try:
        from agent.integrations.kubectl import get_deployment_info, scale_deployment
        from agent.observability.costs import get_session_total
        from agent.skills.deployment import DeploymentSkill

        console.print()
        console.print(Rule(
            f"[bold cyan]Scale:[/bold cyan] {_escape(deployment)} → "
            f"[green]{replicas}[/green] replicas [dim]({namespace})[/dim]"
        ))
        console.print()

        # Get current state
        with console.status("[cyan]Fetching deployment info...", spinner="dots"):
            info = get_deployment_info(deployment, namespace)

        if info is None:
            _print_error(f"Deployment '{deployment}' not found in namespace '{namespace}'.")
            raise typer.Exit(1)

        current_image = info.current_image
        current_replicas = info.replicas_desired
        skill = DeploymentSkill()

        # Risk score
        with console.status("[cyan]Scoring scale risk...", spinner="dots"):
            risk = skill.score_deployment_risk(
                deployment, namespace,
                new_image=current_image,
                current_image=current_image,
            )

        _print_risk_score(risk)

        # Show scale summary
        direction = (
            "[green]↑ Scale UP[/green]" if replicas > current_replicas
            else "[yellow]↓ Scale DOWN[/yellow]" if replicas < current_replicas
            else "[dim]= No change[/dim]"
        )
        console.print(Panel(
            f"  [dim]Current :[/dim] [bold]{current_replicas}[/bold] replicas\n"
            f"  [dim]Target  :[/dim] [bold]{replicas}[/bold] replicas\n"
            f"  [dim]Delta   :[/dim] {direction}  ([bold]{replicas - current_replicas:+d}[/bold])\n"
            f"  [dim]Risk    :[/dim] {risk.label.upper()}",
            title="[bold]SCALE PLAN[/bold]",
            border_style="cyan", padding=(0, 2),
        ))
        console.print()

        if replicas == 0 and _is_production(namespace):
            console.print(Panel(
                f"[bold red]WARNING: Scaling to 0 in PRODUCTION namespace '{_escape(namespace)}'[/bold red]\n"
                f"This will take the service completely offline.\nType the deployment name to confirm:",
                border_style="red", padding=(0, 2),
            ))
            typed = typer.prompt("  Confirm deployment name")
            if typed.strip() != deployment:
                console.print("[bold red]Name mismatch — scale cancelled.[/bold red]")
                raise typer.Exit(0)
        elif _is_production(namespace):
            console.print(Panel(
                f"[bold red]PRODUCTION NAMESPACE: {_escape(namespace)}[/bold red]\n"
                f"Type the deployment name to confirm:",
                border_style="red", padding=(0, 2),
            ))
            typed = typer.prompt("  Confirm deployment name")
            if typed.strip() != deployment:
                console.print("[bold red]Name mismatch — scale cancelled.[/bold red]")
                raise typer.Exit(0)
        else:
            if not typer.confirm(f"  Scale to {replicas} replicas?", default=False):
                console.print("[dim]Cancelled.[/dim]")
                raise typer.Exit(0)

        console.print()
        with console.status("[bold cyan]Scaling...", spinner="dots"):
            r = scale_deployment(deployment, namespace, replicas)

        if not r.success:
            _print_error(f"Scale failed: {r.error[:200]}")
            raise typer.Exit(1)

        console.print(
            f"[bold green]✓ Scaled {_escape(deployment)} to {replicas} replicas.[/bold green]"
        )
        console.print()
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@k8s_app.command("deploy")
def k8s_deploy(
    deployment: str = typer.Argument(..., help="Deployment name to update."),
    image: str = typer.Option(
        ..., "--image",
        help="New container image tag (e.g. myapp:v2.1.0).",
    ),
    namespace: str = typer.Option(
        "default", "--namespace", "-n",
        help="Kubernetes namespace.",
        show_default=True,
    ),
    watch_seconds: int = typer.Option(
        120, "--watch-seconds",
        help="How long to watch after deploy (0 = skip watcher).", show_default=True,
    ),
    no_auto_rollback: bool = typer.Option(
        False, "--no-auto-rollback",
        help="Disable automatic rollback on crash detection.",
    ),
) -> None:
    """
    Deploy a new image with risk scoring, health gate, live watcher, auto-rollback, and report.
    """
    try:
        import time as _time
        from agent.integrations.kubectl import get_deployment_info, set_image, wait_for_rollout
        from agent.observability.costs import get_session_total
        from agent.skills.deployment import DeploymentSkill

        console.print()
        console.print(Rule(
            f"[bold cyan]Deploy:[/bold cyan] {_escape(deployment)} → "
            f"[green]{_escape(image)}[/green] [dim]({namespace})[/dim]"
        ))
        console.print()

        skill = DeploymentSkill()

        # Get current image for risk scoring
        with console.status("[cyan]Fetching current deployment state...", spinner="dots"):
            info = get_deployment_info(deployment, namespace)

        if info is None:
            _print_error(f"Deployment '{deployment}' not found in namespace '{namespace}'.")
            raise typer.Exit(1)

        current_image = info.current_image
        container = info.containers[0] if info.containers else deployment

        # 1. Risk score
        with console.status("[cyan]Scoring deployment risk...", spinner="dots"):
            risk = skill.score_deployment_risk(deployment, namespace, image, current_image)

        _print_risk_score(risk)

        # 2. Health gate
        with console.status("[cyan]Running pre-deploy health checks...", spinner="dots"):
            gate = skill.pre_deploy_health_check(deployment, namespace, image)

        _print_health_gate(gate)

        if gate.blocked:
            _print_error("Deploy blocked by health gate. Fix issues before deploying.")
            raise typer.Exit(1)

        # 3. Confirm
        if _is_production(namespace) or risk.label in ("high", "critical"):
            lbl = risk.label.upper()
            console.print(Panel(
                f"[bold red]{'PRODUCTION: ' + namespace if _is_production(namespace) else ''}[/bold red]"
                f"  [bold red]RISK: {lbl}[/bold red]\n"
                f"Type the deployment name to confirm:",
                border_style="red", padding=(0, 2),
            ))
            typed = typer.prompt("  Confirm deployment name")
            if typed.strip() != deployment:
                console.print("[bold red]Name mismatch — deploy cancelled.[/bold red]")
                raise typer.Exit(0)
        else:
            if not typer.confirm("  Proceed with deploy?", default=False):
                console.print("[dim]Cancelled.[/dim]")
                raise typer.Exit(0)

        # 4. Deploy
        console.print()
        console.print("[bold cyan]Applying new image...[/bold cyan]")
        start = _time.time()

        with console.status("[bold cyan]Setting image...", spinner="dots"):
            r = set_image(deployment, namespace, container, image)

        if not r.success:
            _print_error(f"Deploy failed: {r.error[:200]}")
            raise typer.Exit(1)

        with console.status("[bold cyan]Waiting for rollout...", spinner="dots"):
            wait_for_rollout(deployment, namespace, timeout=120)

        dur_so_far = int(_time.time() - start)

        # 5. Watch + report
        if watch_seconds > 0 and not no_auto_rollback:
            _run_watcher(skill, deployment, namespace, current_image, image, watch_seconds, risk.total, risk.label, gate, dur_so_far)
        else:
            console.print("[bold green]✓ Deploy complete.[/bold green]")

        console.print()
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@k8s_app.command("restart")
def k8s_restart_deployment(
    deployment: str = typer.Argument(..., help="Deployment name to restart."),
    namespace: str = typer.Option(
        "default", "--namespace", "-n",
        help="Kubernetes namespace.",
        show_default=True,
    ),
    watch_seconds: int = typer.Option(
        60, "--watch-seconds",
        help="How long to watch pods after restart.", show_default=True,
    ),
) -> None:
    """Rolling restart a deployment (same image, fresh containers) with live watcher."""
    try:
        import time as _time
        from agent.integrations.kubectl import get_deployment_info, rollout_restart, wait_for_rollout
        from agent.observability.costs import get_session_total
        from agent.skills.deployment import DeploymentSkill

        console.print()
        console.print(Rule(
            f"[bold cyan]Restart:[/bold cyan] {_escape(deployment)} [dim]({namespace})[/dim]"
        ))
        console.print()

        with console.status("[cyan]Fetching deployment info...", spinner="dots"):
            info = get_deployment_info(deployment, namespace)

        if info is None:
            _print_error(f"Deployment '{deployment}' not found in namespace '{namespace}'.")
            raise typer.Exit(1)

        current_image = info.current_image
        health_color  = "green" if info.healthy else "yellow"

        console.print(Panel(
            f"  [dim]Image    :[/dim] [bold]{_escape(current_image)}[/bold]\n"
            f"  [dim]Replicas :[/dim] [{health_color}]{info.replicas_ready}/{info.replicas_desired} ready[/{health_color}]\n"
            f"  [dim]Namespace:[/dim] {_escape(namespace)}",
            title="[bold]RESTART PLAN[/bold]", border_style="cyan", padding=(0, 2),
        ))
        console.print()

        if _is_production(namespace):
            console.print(Panel(
                f"[bold red]PRODUCTION NAMESPACE: {_escape(namespace)}[/bold red]\n"
                f"Type the deployment name to confirm restart:",
                border_style="red", padding=(0, 2),
            ))
            typed = typer.prompt("  Confirm deployment name")
            if typed.strip() != deployment:
                console.print("[bold red]Name mismatch — restart cancelled.[/bold red]")
                raise typer.Exit(0)
        else:
            if not typer.confirm("  Proceed with rolling restart?", default=False):
                console.print("[dim]Cancelled.[/dim]")
                raise typer.Exit(0)

        console.print()
        start = _time.time()
        with console.status("[bold cyan]Restarting...", spinner="dots"):
            r = rollout_restart(deployment, namespace)

        if not r.success:
            _print_error(f"Restart failed: {r.error[:200]}")
            raise typer.Exit(1)

        with console.status("[bold cyan]Waiting for rollout...", spinner="dots"):
            wait_for_rollout(deployment, namespace, timeout=120)

        dur_so_far = int(_time.time() - start)

        # Watch briefly
        if watch_seconds > 0:
            skill = DeploymentSkill()
            from agent.core.models import HealthGateResult
            dummy_gate = HealthGateResult(
                passed=True, blocked=False, checks=[], warnings=[], blockers=[],
                recommendation="restart", check_duration_ms=0,
            )
            _run_watcher(
                skill, deployment, namespace,
                current_image, current_image,
                watch_seconds, 0, "low", dummy_gate, dur_so_far,
            )
        else:
            console.print("[bold green]✓ Restart complete.[/bold green]")

        console.print()
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@k8s_app.command("deploy-history")
def k8s_deploy_history(
    deployment: str = typer.Argument(..., help="Deployment name to inspect."),
    namespace: str = typer.Option(
        "default", "--namespace", "-n",
        help="Kubernetes namespace.",
        show_default=True,
    ),
) -> None:
    """Show rollout revision history for a deployment (revisions, images, change causes)."""
    try:
        from agent.integrations.kubectl import get_deployment_history, get_deployment_info

        console.print()
        console.print(Rule(
            f"[bold cyan]Deploy History:[/bold cyan] {_escape(deployment)} [dim]({namespace})[/dim]"
        ))
        console.print()

        with console.status("[bold cyan]Fetching deployment history...", spinner="dots"):
            info    = get_deployment_info(deployment, namespace)
            history = get_deployment_history(deployment, namespace)

        if info is None:
            _print_error(f"Deployment '{deployment}' not found in namespace '{namespace}'.")
            raise typer.Exit(1)

        health_color = "green" if info.healthy else "red"
        console.print(Panel(
            f"  [dim]Image    :[/dim] [bold]{_escape(info.current_image)}[/bold]\n"
            f"  [dim]Replicas :[/dim] [{health_color}]{info.replicas_ready}/{info.replicas_desired} ready[/{health_color}]\n"
            f"  [dim]Strategy :[/dim] {_escape(info.strategy)}\n"
            f"  [dim]Revision :[/dim] [cyan]{info.revision}[/cyan]  (current)",
            title=f"[bold]CURRENT STATE[/bold]  [cyan]{_escape(deployment)}[/cyan]",
            border_style="cyan",
            padding=(0, 2),
        ))
        console.print()

        if not history:
            console.print("[dim]No rollout history available.[/dim]")
            console.print()
            return

        tbl = Table(
            title=f"Rollout History  [dim]({len(history)} revisions)[/dim]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        tbl.add_column("Rev",          justify="center", width=5)
        tbl.add_column("Image",        max_width=50, no_wrap=True)
        tbl.add_column("Change Cause", max_width=38)
        tbl.add_column("Created At",   width=22)
        tbl.add_column("Status",       justify="center", width=10)

        for rev in history:
            is_current = (rev.revision_number == info.revision)
            rev_color  = "bold cyan" if is_current else "dim"
            status_col = "[bold green]CURRENT[/bold green]" if is_current else "[dim]old[/dim]"
            tbl.add_row(
                f"[{rev_color}]{rev.revision_number}[/{rev_color}]",
                _escape(rev.image),
                _escape(rev.change_cause or "—"),
                _fmt_revision_time(rev.created_at),
                status_col,
            )

        console.print(tbl)
        console.print()

        if len(history) >= 2:
            prev = history[1]
            console.print(
                f"  [dim]Rollback to previous revision {prev.revision_number}:[/dim]\n"
                f"    [bold]agent k8s rollback {_escape(deployment)} -n {_escape(namespace)}[/bold]\n"
            )
            console.print(
                f"  [dim]Rollback to specific revision:[/dim]\n"
                f"    [bold]agent k8s rollback {_escape(deployment)} -n {_escape(namespace)} "
                f"--to-revision <N>[/bold]\n"
            )

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Resources commands
# ---------------------------------------------------------------------------

@resources_app.command("scan")
def resources_scan(
    namespace: str = typer.Option(
        "all", "--namespace", "-n",
        help="Namespace to scan, or 'all'.",
        show_default=True,
    ),
    show_healthy: bool = typer.Option(
        False, "--show-healthy",
        help="Also list healthy pods.",
    ),
    slack: bool = typer.Option(
        False, "--slack",
        help="Send critical resource alerts to Slack.",
    ),
) -> None:
    """Scan CPU and memory for all pods and nodes. Alerts on limit approaches."""
    try:
        from agent.skills.resource_monitor import ResourceMonitorSkill
        from agent.observability.costs import get_session_total

        skill = ResourceMonitorSkill()
        console.print()

        with console.status("[bold cyan]Scanning resources (nodes + pods + limits)...", spinner="dots"):
            report = skill.scan_resources(namespace)

        # Slack: alert on critical resource issues
        if slack:
            from agent.integrations.slack import send_alert_generic, is_configured as slack_ok
            from agent.integrations.alert_dedup import AlertDeduplicator, make_fingerprint
            dedup = AlertDeduplicator()
            if slack_ok():
                sent = 0
                for alert in report.alerts:
                    if alert.severity not in ("critical", "warning"):
                        continue
                    fp = make_fingerprint(alert.pod_or_node, alert.namespace or "", alert.alert_type)
                    if dedup.should_send(fp, alert.severity):
                        ok = send_alert_generic(
                            title=f"Resource: {alert.alert_type} — {alert.pod_or_node}",
                            message=alert.recommendation,
                            severity=alert.severity,
                            fields={
                                "Resource":  alert.pod_or_node,
                                "Usage":     f"{alert.current_usage} ({alert.percent_used}% of {alert.limit})",
                                "Namespace": alert.namespace or "—",
                            },
                            fix_command=alert.fix_command,
                        )
                        if ok:
                            dedup.record_sent(fp, alert.severity)
                            sent += 1
                if sent:
                    console.print(f"  [green]Slack:[/green] {sent} resource alert(s) sent")
                else:
                    console.print("  [dim]Slack: no new alerts (all deduplicated)[/dim]")
            else:
                console.print("  [yellow]Slack: webhook not configured (set SLACK_WEBHOOK_URL in .env)[/yellow]")

        console.print()

        # Metrics-server missing — friendly guidance
        if not report.nodes and not report.pods:
            console.print(Panel(
                "[yellow]No metrics returned.[/yellow]\n\n"
                "This usually means [bold]metrics-server[/bold] is not installed.\n\n"
                "Install it:\n"
                "  [cyan]kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/"
                "releases/latest/download/components.yaml[/cyan]\n\n"
                "If you're on a self-signed / bare-metal cluster also run:\n"
                "  [cyan]kubectl patch deployment metrics-server -n kube-system "
                "--type=json -p='[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args/-\","
                "\"value\":\"--kubelet-insecure-tls\"}]'[/cyan]\n\n"
                "Wait ~60s then re-run: [bold]agent resources scan[/bold]",
                title="[bold yellow]metrics-server not available[/bold yellow]",
                border_style="yellow",
            ))
            console.print()
            return

        # Summary panel
        crit_color = "red" if report.critical_count else ("yellow" if report.warning_count else "green")
        console.print(Panel(
            f"[bold]Scanned [cyan]{len(report.nodes)}[/cyan] nodes, "
            f"[cyan]{len(report.pods)}[/cyan] pods[/bold]\n"
            f"[bold red]Critical alerts: {report.critical_count}[/bold red]   "
            f"[yellow]Warnings: {report.warning_count}[/yellow]   "
            f"[green]Healthy: {report.healthy_count}[/green]",
            title="[bold]RESOURCE HEALTH SCAN[/bold]",
            border_style=crit_color,
        ))
        console.print()

        # Node table
        if report.nodes:
            ntbl = Table(
                title="NODE RESOURCES",
                show_lines=False,
                header_style="bold cyan",
                border_style="dim",
            )
            ntbl.add_column("Node",    min_width=16)
            ntbl.add_column("CPU Used", width=12, justify="right")
            ntbl.add_column("CPU %",    width=10, justify="right")
            ntbl.add_column("Mem Used", width=12, justify="right")
            ntbl.add_column("Mem %",    width=10, justify="right")

            for n in sorted(report.nodes, key=lambda x: x.cpu_percent + x.memory_percent, reverse=True):
                cc = "bold red" if n.cpu_percent >= 85 else ("yellow" if n.cpu_percent >= 70 else "green")
                mc = "bold red" if n.memory_percent >= 90 else ("yellow" if n.memory_percent >= 75 else "green")
                ntbl.add_row(
                    f"[bold]{_escape(n.name)}[/bold]",
                    n.cpu_usage,
                    f"[{cc}]{n.cpu_percent}% {'✗' if n.cpu_percent >= 70 else '✓'}[/{cc}]",
                    n.memory_usage,
                    f"[{mc}]{n.memory_percent}% {'✗' if n.memory_percent >= 75 else '✓'}[/{mc}]",
                )
            console.print(ntbl)
            console.print()

        # Alert tables per severity
        for sev, title, color in (
            ("critical", "CRITICAL ALERTS (fix immediately)", "bold red"),
            ("warning",  "WARNINGS",                          "yellow"),
        ):
            sev_alerts = [a for a in report.alerts if a.severity == sev]
            if not sev_alerts:
                continue

            atbl = Table(
                title=f"[{color}]{title}[/{color}]",
                show_lines=True,
                header_style="bold cyan",
                border_style="dim",
            )
            atbl.add_column("Pod / Node",  max_width=30, no_wrap=True)
            atbl.add_column("Namespace",   width=16)
            atbl.add_column("Type",        width=12)
            atbl.add_column("Usage",       width=14, justify="right")
            atbl.add_column("Limit",       width=10, justify="right")
            atbl.add_column("Recommendation", min_width=36)

            for a in sev_alerts:
                type_label = a.alert_type.replace("_", " ")
                pct_str    = f" ({a.percent_used}%)" if a.percent_used > 0 else ""
                if a.alert_type == "OOM_RISK":
                    type_label = f"[bold red]OOM RISK{pct_str}[/bold red]"
                elif a.alert_type == "NO_LIMITS":
                    type_label = "[yellow]NO LIMIT[/yellow] ⚠"
                elif sev == "critical":
                    type_label = f"[bold red]{type_label}{pct_str}[/bold red]"
                else:
                    type_label = f"[yellow]{type_label}{pct_str}[/yellow]"

                atbl.add_row(
                    f"[bold]{_escape(a.pod_or_node)}[/bold]",
                    _escape(a.namespace or "—"),
                    type_label,
                    _escape(a.current_usage),
                    _escape(a.limit),
                    _escape(a.recommendation),
                )
            console.print(atbl)
            console.print()

        # Pods without limits warning
        if report.pods_without_limits:
            console.print(
                f"  [yellow]⚠  {len(report.pods_without_limits)} pod(s) have no resource limits:[/yellow]"
            )
            for name in report.pods_without_limits[:10]:
                console.print(f"    [dim]{name}[/dim]")
            if len(report.pods_without_limits) > 10:
                console.print(f"    [dim]... and {len(report.pods_without_limits) - 10} more[/dim]")
            console.print()

        # HPA status if any critical
        if report.critical_count > 0:
            from agent.integrations.kubectl import get_hpa_status
            with console.status("[dim]Checking HPA...[/dim]", spinner="dots"):
                hpas = get_hpa_status(namespace)
            if hpas:
                htbl = Table(
                    title="HPA STATUS",
                    show_lines=False,
                    header_style="bold cyan",
                    border_style="dim",
                )
                htbl.add_column("Name",      max_width=28)
                htbl.add_column("Namespace", width=16)
                htbl.add_column("Target",    width=22)
                htbl.add_column("Replicas",  width=16, justify="center")
                htbl.add_column("CPU Cur/Target", width=18, justify="center")
                for h in hpas:
                    color = "red" if h.cpu_current >= 85 else ("yellow" if h.cpu_current >= 70 else "green")
                    htbl.add_row(
                        _escape(h.name), _escape(h.namespace), _escape(h.target),
                        f"{h.current_replicas}/{h.max_replicas}",
                        f"[{color}]{h.cpu_current}%[/{color}]/{h.cpu_target}%",
                    )
                console.print(htbl)
                console.print()

        # Claude AI analysis
        if report.claude_analysis:
            console.print(Panel(
                _escape(report.claude_analysis),
                title="[bold magenta]AI ANALYSIS[/bold magenta]",
                border_style="magenta",
                padding=(0, 1),
            ))
            console.print()

        # Healthy pods (optional)
        if show_healthy:
            healthy_pods = [p for p in report.pods if not p.at_risk]
            if healthy_pods:
                console.print(f"  [dim]{len(healthy_pods)} healthy pod(s) — all within limits.[/dim]")
                console.print()

        # Fix hints
        hints = []
        if report.critical_count > 0:
            hints.append("  [bold]agent resources fix --all-critical[/bold]       [dim]fix high CPU/memory[/dim]")
        if report.pods_without_limits:
            hints.append("  [bold]agent resources fix --fix-limits[/bold]         [dim]add limits to all pods that have none[/dim]")
        hints.append(    "  [bold]agent resources fix --scale-down[/bold]         [dim]suggest replica reduction for idle deployments[/dim]")
        hints.append(    "  [bold]agent resources fix --all[/bold]                [dim]do everything above at once[/dim]")
        if hints:
            console.print(Rule("[dim]FIX COMMANDS[/dim]"))
            for h in hints:
                console.print(h)
            console.print()

        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@resources_app.command("fix")
def resources_fix(
    pod: Optional[str] = typer.Option(None, "--pod", help="Pod name to fix."),
    namespace: str = typer.Option(
        "all", "--namespace", "-n", help="Namespace of the pod (or 'all').",
        show_default=True,
    ),
    all_critical: bool = typer.Option(
        False, "--all-critical", help="Fix all critical resource issues.",
    ),
    fix_limits: bool = typer.Option(
        False, "--fix-limits", help="Add limits to every pod that has none.",
    ),
    scale_down: bool = typer.Option(
        False, "--scale-down", help="Suggest scale-down for over-replicated low-usage deployments.",
    ),
    all_issues: bool = typer.Option(
        False, "--all", help="Handle all issues: critical, no-limits, and scale-down suggestions.",
    ),
) -> None:
    """Fix resource issues interactively — always asks [y/N] before applying."""
    import subprocess as _sp
    import time as _time

    try:
        from agent.integrations.kubectl import (
            _parse_cpu_millicores, _parse_memory_mebibytes,
            get_deployment_for_pod, get_pod_limits, get_pod_metrics,
        )
        from agent.skills.resource_monitor import ResourceMonitorSkill, _bump_memory
        from agent.skills._fix_runner import apply_shell_fix

        skill = ResourceMonitorSkill()

        # ------------------------------------------------------------------
        # Helper: get current replica count for a deployment
        # ------------------------------------------------------------------
        def _get_replicas(dep_name: str, ns: str) -> int:
            r = _sp.run(
                ["kubectl", "get", "deployment", dep_name, "-n", ns,
                 "--no-headers",
                 "-o", "custom-columns=DESIRED:.spec.replicas,READY:.status.readyReplicas"],
                capture_output=True, text=True, timeout=15,
            )
            parts = r.stdout.strip().split()
            try:
                return int(parts[0])
            except Exception:
                return 1

        # ------------------------------------------------------------------
        # Helper: compute suggested limits from actual usage
        # ------------------------------------------------------------------
        def _suggest_limits(cpu_raw: str, mem_raw: str):
            cpu_mc  = _parse_cpu_millicores(cpu_raw)
            mem_mb  = _parse_memory_mebibytes(mem_raw)
            # limit = 2× usage, rounded, with minimums
            cpu_lim = max(((cpu_mc * 2 + 99) // 100) * 100, 200)
            mem_lim = max(((mem_mb * 2 + 63) // 64) * 64, 128)
            cpu_req = max(cpu_lim // 4, 50)
            mem_req = max(mem_lim // 4, 32)
            mem_lim_str = f"{mem_lim // 1024}Gi" if mem_lim >= 1024 else f"{mem_lim}Mi"
            mem_req_str = f"{mem_req // 1024}Gi" if mem_req >= 1024 else f"{mem_req}Mi"
            return f"{cpu_lim}m", mem_lim_str, f"{cpu_req}m", mem_req_str

        # ------------------------------------------------------------------
        # Fix: add limits to a no-limits pod
        # ------------------------------------------------------------------
        def _fix_no_limits(pod_m, owner_name: str | None) -> None:
            ns = pod_m.namespace
            cpu_lim, mem_lim, cpu_req, mem_req = _suggest_limits(
                pod_m.cpu_usage, pod_m.memory_usage
            )

            # Bare pod — can't use kubectl set resources, show recreate command
            if not owner_name:
                image_cmd = f"kubectl get pod {pod_m.name} -n {ns} -o jsonpath='{{.spec.containers[0].image}}'"
                recreate  = (
                    f"# Step 1 — get current image\n"
                    f"IMAGE=$({image_cmd})\n\n"
                    f"# Step 2 — delete and recreate with limits\n"
                    f"kubectl delete pod {pod_m.name} -n {ns}\n"
                    f"kubectl run {pod_m.name} --image=$IMAGE -n {ns} \\\n"
                    f"  --limits=cpu={cpu_lim},memory={mem_lim} \\\n"
                    f"  --requests=cpu={cpu_req},memory={mem_req}"
                )
                console.print(Panel(
                    f"  [bold]Pod:[/bold]      {pod_m.name} [{ns}]\n"
                    f"  [bold]Current:[/bold]  CPU {pod_m.cpu_usage}  MEM {pod_m.memory_usage}\n"
                    f"  [bold]Proposed:[/bold] CPU limit {cpu_lim}  MEM limit {mem_lim}\n\n"
                    f"  [yellow]Bare pod — Kubernetes cannot patch limits on running pods.[/yellow]\n"
                    f"  Delete and recreate with limits:\n\n"
                    f"  {recreate}",
                    title=f"[cyan]NO LIMITS: {pod_m.name} (bare pod)[/cyan]",
                    border_style="yellow",
                ))
                console.print()
                return

            cmd = (
                f"kubectl set resources deployment/{owner_name} -n {ns} "
                f"--limits=cpu={cpu_lim},memory={mem_lim} "
                f"--requests=cpu={cpu_req},memory={mem_req}"
            )
            console.print(Panel(
                f"  [bold]Problem:[/bold]  No resource limits — pod can starve others\n"
                f"  [bold]Current:[/bold]  CPU {pod_m.cpu_usage}  MEM {pod_m.memory_usage}\n\n"
                f"  [bold]PROPOSED LIMITS[/bold]  (2× current usage)\n"
                f"  CPU limit:    [yellow]none[/yellow] → [green]{cpu_lim}[/green]\n"
                f"  Memory limit: [yellow]none[/yellow] → [green]{mem_lim}[/green]\n"
                f"  CPU request:  none → {cpu_req}\n"
                f"  Memory req:   none → {mem_req}\n\n"
                f"  [bold]COMMAND[/bold]\n  {cmd}",
                title=f"[cyan]NO LIMITS: {pod_m.name}[/cyan]",
                border_style="yellow",
            ))
            if not typer.confirm("  Apply this fix?", default=False):
                console.print("  [dim]Skipped.[/dim]\n")
                return
            res = apply_shell_fix(cmd)
            icon = "[green]✓[/green]" if res == "ok" else "[red]✗[/red]"
            console.print(f"  {icon} {res}\n")

        # ------------------------------------------------------------------
        # Fix: increase memory limit for high-memory pod
        # ------------------------------------------------------------------
        def _fix_high_memory(pod_m, limits, owner_name: str | None) -> None:
            ns      = pod_m.namespace
            factor  = 1.5 if pod_m.memory_percent >= 90 else 1.25
            new_mem = _bump_memory(limits.memory_limit, factor)
            pct_tag = "+50%" if factor == 1.5 else "+25%"
            risk    = "OOMKill imminent" if pod_m.memory_percent >= 90 else "memory warning"
            target  = f"deployment/{owner_name}" if owner_name else f"pod/{pod_m.name}"
            cmd     = f"kubectl set resources {target} -n {ns} --limits=memory={new_mem}"
            console.print(Panel(
                f"  [bold]Problem:[/bold]  Memory at {pod_m.memory_percent}% "
                f"({pod_m.memory_usage} / {limits.memory_limit})\n"
                f"  [bold]Risk:[/bold]     {risk}\n\n"
                f"  [bold]PROPOSED CHANGE[/bold]\n"
                f"  Memory limit: [yellow]{limits.memory_limit}[/yellow] → [green]{new_mem}[/green] ({pct_tag})\n\n"
                f"  [bold]COMMAND[/bold]\n  {cmd}",
                title=f"[red]MEM HIGH: {pod_m.name}[/red]",
                border_style="red",
            ))
            if not typer.confirm("  Apply this fix?", default=False):
                console.print("  [dim]Skipped.[/dim]\n")
                return
            if not owner_name:
                console.print("  [yellow]Cannot determine owner — apply manually.[/yellow]\n")
                return
            res = apply_shell_fix(cmd)
            if res == "ok":
                console.print("  [bold green]✓ Applied.[/bold green]  Waiting 10s for rollout...")
                _time.sleep(10)
                new_pods = get_pod_metrics(ns)
                new_m    = next((p for p in new_pods if p.name == pod_m.name), None)
                if new_m:
                    ok = new_m.memory_percent < 75
                    console.print(
                        f"  Memory: {new_m.memory_usage} / {new_mem} ({new_m.memory_percent}%) "
                        + ("[green]✓[/green]" if ok else "[yellow]still high[/yellow]")
                    )
            else:
                console.print("  [bold red]✗ Fix failed.[/bold red]")
            console.print()

        # ------------------------------------------------------------------
        # Fix: scale up for high-CPU pod
        # ------------------------------------------------------------------
        def _fix_high_cpu(pod_m, limits, owner_name: str | None) -> None:
            ns       = pod_m.namespace
            cur_reps = _get_replicas(owner_name, ns) if owner_name else 1
            new_reps = cur_reps + 1
            target   = f"deployment/{owner_name}" if owner_name else pod_m.name
            cmd      = f"kubectl scale {target} -n {ns} --replicas={new_reps}"
            console.print(Panel(
                f"  [bold]Problem:[/bold]  CPU at {pod_m.cpu_percent}% "
                f"({pod_m.cpu_usage} / {limits.cpu_limit})\n"
                f"  [bold]Risk:[/bold]     CPU throttling\n\n"
                f"  [bold]PROPOSED CHANGE[/bold]\n"
                f"  Scale: [yellow]{cur_reps}[/yellow] → [green]{new_reps}[/green] replicas\n\n"
                f"  [bold]COMMAND[/bold]\n  {cmd}",
                title=f"[red]CPU HIGH: {pod_m.name}[/red]",
                border_style="red",
            ))
            if not typer.confirm("  Apply this fix?", default=False):
                console.print("  [dim]Skipped.[/dim]\n")
                return
            if not owner_name:
                console.print("  [yellow]Cannot determine deployment — apply manually.[/yellow]\n")
                return
            res  = apply_shell_fix(cmd)
            icon = "[green]✓[/green]" if res == "ok" else "[red]✗[/red]"
            console.print(f"  {icon} {res}\n")

        # ------------------------------------------------------------------
        # Suggestion: scale down over-replicated low-usage deployments
        # ------------------------------------------------------------------
        def _suggest_scale_downs(pods_metrics, scan_ns: str) -> None:
            import json as _json

            # get all deployments with replica counts
            r = _sp.run(
                ["kubectl", "get", "deployments", "-A", "--no-headers",
                 "-o", "custom-columns="
                 "NS:.metadata.namespace,NAME:.metadata.name,"
                 "DESIRED:.spec.replicas,READY:.status.readyReplicas"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return

            suggested = 0
            for line in r.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                dep_ns, dep_name = parts[0], parts[1]
                try:
                    desired = int(parts[2])
                except ValueError:
                    continue
                if desired < 2:
                    continue  # already single replica
                if scan_ns != "all" and dep_ns != scan_ns:
                    continue

                # find pods belonging to this deployment in metrics
                dep_pods = [
                    p for p in pods_metrics
                    if p.namespace == dep_ns and dep_name in p.name
                ]
                if not dep_pods:
                    continue

                avg_cpu = sum(p.cpu_percent for p in dep_pods) / len(dep_pods)
                avg_mem = sum(p.memory_percent for p in dep_pods) / len(dep_pods)

                # Only suggest scale-down if both metrics comfortably below thresholds
                if avg_cpu < 20 and avg_mem < 40:
                    suggested += 1
                    new_reps = desired - 1
                    console.print(Panel(
                        f"  [bold]Deployment:[/bold]  {dep_name}  [{dep_ns}]\n"
                        f"  [bold]Replicas:[/bold]    {desired} running\n"
                        f"  [bold]Avg usage:[/bold]   CPU {avg_cpu:.0f}%  MEM {avg_mem:.0f}%  "
                        f"[dim](well below thresholds)[/dim]\n\n"
                        f"  [bold]PROPOSED CHANGE[/bold]\n"
                        f"  Scale: [yellow]{desired}[/yellow] → [green]{new_reps}[/green] replicas  "
                        f"[dim](saves ~{100 // desired}% of resources)[/dim]\n\n"
                        f"  [bold]COMMAND[/bold]\n"
                        f"  kubectl scale deployment/{dep_name} -n {dep_ns} --replicas={new_reps}",
                        title=f"[dim]SCALE DOWN SUGGESTION: {dep_name}[/dim]",
                        border_style="dim",
                    ))
                    if typer.confirm("  Scale this down?", default=False):
                        cmd = f"kubectl scale deployment/{dep_name} -n {dep_ns} --replicas={new_reps}"
                        res  = apply_shell_fix(cmd)
                        icon = "[green]✓[/green]" if res == "ok" else "[red]✗[/red]"
                        console.print(f"  {icon} {res}")
                    else:
                        console.print("  [dim]Skipped.[/dim]")
                    console.print()

            if suggested == 0:
                console.print("  [dim]No over-replicated low-usage deployments found.[/dim]\n")

        # ------------------------------------------------------------------
        # Dispatch
        # ------------------------------------------------------------------
        run_all      = all_issues
        run_critical = all_critical or run_all
        run_limits   = fix_limits   or run_all
        run_scale    = scale_down   or run_all

        if not any([pod, run_critical, run_limits, run_scale]):
            console.print(
                "[yellow]Nothing to do. Use one of:[/yellow]\n"
                "  [bold]--pod <name> -n <ns>[/bold]          fix a specific pod\n"
                "  [bold]--all-critical[/bold]                fix all critical resource alerts\n"
                "  [bold]--fix-limits[/bold]                  add limits to all pods that have none\n"
                "  [bold]--scale-down[/bold]                  suggest replica reduction for idle deployments\n"
                "  [bold]--all[/bold]                         do all of the above"
            )
            raise typer.Exit(1)

        console.print()
        scan_ns = namespace

        with console.status("[bold cyan]Scanning resources...", spinner="dots"):
            report      = skill.scan_resources(scan_ns)
            all_pod_met = report.pods

        # ── 1. Critical alerts (high memory / high CPU) ────────────────────
        if run_critical:
            critical = [
                a for a in report.alerts
                if a.severity == "critical" and a.namespace and a.pod_or_node
            ]
            if critical:
                console.print(Rule(f"[bold red]CRITICAL ISSUES ({len(critical)})[/bold red]"))
                console.print()
                for alert in critical:
                    pod_m = next(
                        (p for p in all_pod_met
                         if p.name == alert.pod_or_node and p.namespace == alert.namespace),
                        None,
                    )
                    if not pod_m:
                        continue
                    _, owner_name = get_deployment_for_pod(alert.pod_or_node, alert.namespace)
                    limits = get_pod_limits(alert.pod_or_node, alert.namespace)
                    if alert.alert_type in ("OOM_RISK", "MEM_HIGH"):
                        _fix_high_memory(pod_m, limits, owner_name)
                    elif alert.alert_type == "CPU_HIGH":
                        _fix_high_cpu(pod_m, limits, owner_name)
            else:
                console.print("[green]No critical resource alerts.[/green]\n")

        # ── 2. No-limits pods ──────────────────────────────────────────────
        if run_limits:
            from agent.skills.resource_monitor import _is_system_component
            no_lim_pods = [
                p for p in all_pod_met
                if p.risk_type == "no_limits"
                and not _is_system_component(p.name, p.namespace)
            ]
            if no_lim_pods:
                console.print(Rule(f"[yellow]PODS WITHOUT LIMITS ({len(no_lim_pods)})[/yellow]"))
                console.print()
                for pod_m in no_lim_pods:
                    if scan_ns != "all" and pod_m.namespace != scan_ns:
                        continue
                    _, owner_name = get_deployment_for_pod(pod_m.name, pod_m.namespace)
                    _fix_no_limits(pod_m, owner_name)
            else:
                console.print("[green]All pods have resource limits set.[/green]\n")

        # ── 3. Scale-down suggestions ──────────────────────────────────────
        if run_scale:
            console.print(Rule("[dim]SCALE-DOWN SUGGESTIONS[/dim]"))
            console.print()
            _suggest_scale_downs(all_pod_met, scan_ns)

        # ── 4. Single pod fix ──────────────────────────────────────────────
        if pod:
            ns    = namespace if namespace != "all" else "default"
            pod_m = next((p for p in all_pod_met if p.name == pod), None)
            if not pod_m:
                # pod not in metrics — try loading limits directly
                limits = get_pod_limits(pod, ns)
                console.print(f"  [yellow]{pod} has no live metrics — checking limits...[/yellow]")
                if not limits.has_limits:
                    console.print(f"  [yellow]No limits set on {pod}.[/yellow]")
                return
            _, owner_name = get_deployment_for_pod(pod, pod_m.namespace)
            limits        = get_pod_limits(pod, pod_m.namespace)

            if pod_m.memory_percent >= 75:
                _fix_high_memory(pod_m, limits, owner_name)
            elif pod_m.cpu_percent >= 85:
                _fix_high_cpu(pod_m, limits, owner_name)
            elif pod_m.risk_type == "no_limits":
                _fix_no_limits(pod_m, owner_name)
            else:
                console.print(
                    f"  [green]{pod}: CPU {pod_m.cpu_percent}%  MEM {pod_m.memory_percent}% — no fix needed.[/green]"
                )

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@resources_app.command("watch")
def resources_watch(
    namespace: str = typer.Option(
        "all", "--namespace", "-n",
        help="Namespace to monitor.",
        show_default=True,
    ),
    interval: int = typer.Option(
        30, "--interval",
        help="Refresh interval in seconds.",
        show_default=True,
    ),
    alert_only: bool = typer.Option(
        False, "--alert-only",
        help="Only print output when an alert is triggered.",
    ),
) -> None:
    """Live resource monitor — updates every --interval seconds. Ctrl+C to stop."""
    try:
        from agent.skills.resource_monitor import ResourceMonitorSkill

        skill = ResourceMonitorSkill()
        console.print()
        skill.watch_resources(
            namespace=namespace,
            interval=interval,
            alert_only=alert_only,
            console=console,
        )

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@resources_app.command("history")
def resources_history(
    last: str = typer.Option("24h", "--last", help="Time window (e.g. 24h, 7d)."),
) -> None:
    """Show resource usage history from saved memory snapshots."""
    try:
        from agent.memory.retrieval import get_memories_by_source

        memories = get_memories_by_source("resource-monitor") + \
                   get_memories_by_source("resource-snapshot")

        if not memories:
            console.print(Panel(
                "[dim]No resource history found yet.\n"
                "Run [bold]agent resources scan[/bold] or "
                "[bold]agent resources watch[/bold] to start collecting.[/dim]",
                border_style="dim",
            ))
            return

        console.print()
        tbl = Table(
            title="[bold]Resource History[/bold]",
            show_lines=True,
            header_style="bold cyan",
            border_style="dim",
        )
        tbl.add_column("Time",    width=20)
        tbl.add_column("Source",  width=18)
        tbl.add_column("Summary", min_width=50)

        for m in sorted(memories, key=lambda x: x.created_at, reverse=True)[:50]:
            tbl.add_row(
                m.created_at.strftime("%Y-%m-%d %H:%M"),
                _escape(m.source),
                _escape(m.content[:100]),
            )

        console.print(tbl)
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@resources_app.command("report")
def resources_report(
    namespace: str = typer.Option(
        "all", "--namespace", "-n",
        help="Namespace to report on.",
        show_default=True,
    ),
) -> None:
    """Full resource report — all pods sorted by usage, no-limit offenders, recommendations."""
    try:
        from agent.skills.resource_monitor import ResourceMonitorSkill
        from agent.observability.costs import get_session_total

        skill = ResourceMonitorSkill()
        console.print()

        with console.status("[bold cyan]Building full resource report...", spinner="dots"):
            report = skill.scan_resources(namespace)

        console.print()
        console.print(Rule("[bold]FULL RESOURCE REPORT[/bold]"))
        console.print()

        # All pods sorted by memory usage descending
        all_pods = sorted(
            report.pods,
            key=lambda p: p.memory_percent if p.memory_percent > 0 else (
                999 if p.risk_type == "no_limits" else 0
            ),
            reverse=True,
        )

        ptbl = Table(
            title=f"ALL PODS ({len(all_pods)} total)",
            show_lines=False,
            header_style="bold cyan",
            border_style="dim",
        )
        ptbl.add_column("Pod",       max_width=32, no_wrap=True)
        ptbl.add_column("Namespace", width=16)
        ptbl.add_column("CPU",       width=14, justify="right")
        ptbl.add_column("Memory",    width=14, justify="right")
        ptbl.add_column("Risk",      width=14)

        for p in all_pods:
            cpu_str = (
                f"{p.cpu_usage} ({p.cpu_percent}% of {p.cpu_limit})"
                if p.cpu_limit != "none"
                else f"{p.cpu_usage}"
            )
            mem_str = (
                f"{p.memory_usage} ({p.memory_percent}% of {p.memory_limit})"
                if p.memory_limit != "none"
                else f"{p.memory_usage}"
            )
            if p.risk_type == "no_limits":
                risk_str = "[yellow]NO LIMITS ⚠[/yellow]"
            elif p.risk_type in ("memory", "both"):
                risk_str = (
                    "[bold red]OOM RISK[/bold red]"
                    if p.memory_percent >= 90
                    else "[yellow]MEM HIGH[/yellow]"
                )
            elif p.risk_type == "cpu":
                risk_str = "[yellow]CPU HIGH[/yellow]"
            else:
                risk_str = "[green]OK[/green]"

            ptbl.add_row(
                f"[bold]{_escape(p.name)}[/bold]",
                _escape(p.namespace),
                _escape(cpu_str),
                _escape(mem_str),
                risk_str,
            )

        console.print(ptbl)
        console.print()

        # Pods without limits
        if report.pods_without_limits:
            console.print(Panel(
                "[yellow]PODS WITHOUT RESOURCE LIMITS[/yellow]\n\n"
                + "\n".join(f"  {_escape(n)}" for n in report.pods_without_limits),
                title="[yellow]Dangerous — can cause node OOMKill[/yellow]",
                border_style="yellow",
            ))
            console.print()

        # Recommendations sorted by severity
        if report.alerts:
            console.print(Rule("[bold]RECOMMENDATIONS (by priority)[/bold]"))
            console.print()
            critical = [a for a in report.alerts if a.severity == "critical"]
            warnings = [a for a in report.alerts if a.severity == "warning"]
            for i, alert in enumerate(critical + warnings, 1):
                col = "bold red" if alert.severity == "critical" else "yellow"
                console.print(
                    f"  [dim]{i:02d}.[/dim] [{col}]{alert.severity.upper()}[/{col}]"
                    f"  [bold]{_escape(alert.pod_or_node)}[/bold]"
                    + (f" [{alert.namespace}]" if alert.namespace else "")
                )
                console.print(f"       {_escape(alert.recommendation)}")
                console.print()

        # AI analysis
        if report.claude_analysis:
            console.print(Panel(
                _escape(report.claude_analysis),
                title="[bold magenta]AI ANALYSIS[/bold magenta]",
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
# Commands: agent cost *
# ---------------------------------------------------------------------------

_WASTE_COLOR = {
    "over-provisioned":  "yellow",
    "under-provisioned": "red",
    "OK":                "green",
    "unknown":           "dim",
}


@cost_app.command("scan")
def cost_scan(
    namespace: str = typer.Option(
        "all",
        "--namespace", "-n",
        help="Namespace to scan. Defaults to all.",
        show_default=True,
    ),
    top: int = typer.Option(
        10,
        "--top",
        help="Number of top wasteful pods to show.",
        show_default=True,
    ),
    ai: bool = typer.Option(
        False,
        "--ai/--no-ai",
        help="Run Claude AI analysis on results (adds ~5s).",
        show_default=True,
    ),
    usage: bool = typer.Option(
        False,
        "--usage/--no-usage",
        help="Fetch actual CPU/memory via kubectl top (adds 10-20s, needs metrics-server).",
        show_default=True,
    ),
    include_system: bool = typer.Option(
        False,
        "--system/--no-system",
        help="Include kube-system and AWS DaemonSet pods.",
        show_default=True,
    ),
) -> None:
    """Estimate cluster costs from resource requests and detect waste."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.cost import CostAnalysisSkill

        skill = CostAnalysisSkill()
        console.print()
        console.print(Rule("[bold cyan]Cluster Cost Analysis[/bold cyan]"))
        console.print()

        with console.status("[bold green]Analysing pod costs...", spinner="dots"):
            report = skill.analyze_cluster_cost(
                namespace,
                include_system=include_system,
                with_ai=ai,
                with_usage=usage,
            )

        # Summary panel
        waste_col  = "yellow" if report.waste_percent >= 30 else "green"
        nodes_note = (
            " [dim](Fargate / managed — no EC2 nodes visible)[/dim]"
            if report.total_nodes == 0 else ""
        )
        console.print(Panel(
            f"[bold]Cluster:[/bold]  {_escape(report.cluster_name)}\n"
            f"[bold]Nodes:[/bold]    {report.total_nodes}{nodes_note}  |  "
            f"[bold]Node cost:[/bold]  [cyan]${report.total_monthly_node_cost:.2f}/mo[/cyan]\n"
            f"[bold]Requested:[/bold] [cyan]${report.total_requested_cost:.2f}/mo[/cyan]  |  "
            f"[bold]Waste:[/bold] [{waste_col}]${report.total_waste_cost:.2f}/mo ({report.waste_percent}%)[/{waste_col}]",
            title="[bold cyan]COST SUMMARY[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        ))
        console.print()

        # Namespace breakdown
        if report.namespaces:
            ns_tbl = Table(
                title="Cost by Namespace",
                show_lines=False,
                header_style="bold cyan",
                border_style="dim",
            )
            ns_tbl.add_column("Namespace",   min_width=20)
            ns_tbl.add_column("Pods",        width=6,  justify="right")
            ns_tbl.add_column("Est. Monthly", width=14, justify="right")
            for ns in report.namespaces[:15]:
                ns_tbl.add_row(
                    _escape(ns.namespace),
                    str(ns.pod_count),
                    f"[cyan]${ns.est_monthly_cost:.2f}[/cyan]",
                )
            console.print(ns_tbl)
            console.print()

        # Top deployments
        if report.deployments:
            dep_tbl = Table(
                title="Top Deployments by Cost",
                show_lines=True,
                header_style="bold cyan",
                border_style="dim",
            )
            dep_tbl.add_column("Deployment",   min_width=22, no_wrap=True)
            dep_tbl.add_column("Namespace",    width=16)
            dep_tbl.add_column("Pods",         width=5,  justify="right")
            dep_tbl.add_column("CPU Req",      width=9,  justify="right")
            dep_tbl.add_column("CPU Act",      width=9,  justify="right")
            dep_tbl.add_column("Monthly",      width=12, justify="right")
            dep_tbl.add_column("Waste",        width=10, justify="center")

            for d in report.deployments[:20]:
                col   = _WASTE_COLOR.get(d.waste_label, "dim")
                waste = f"[{col}]{d.waste_percent}% {d.waste_label}[/{col}]"
                dep_tbl.add_row(
                    f"[bold]{_escape(d.deployment)}[/bold]",
                    _escape(d.namespace),
                    str(d.pod_count),
                    f"{d.total_cpu_request:.2f}",
                    f"{d.total_cpu_actual:.2f}",
                    f"[cyan]${d.est_monthly_cost:.2f}[/cyan]",
                    waste,
                )
            console.print(dep_tbl)
            console.print()

        # Top wasteful pods
        if report.top_wasteful_pods:
            pod_tbl = Table(
                title=f"Top {min(top, len(report.top_wasteful_pods))} Most Over-Provisioned Pods",
                show_lines=False,
                header_style="bold yellow",
                border_style="yellow",
            )
            pod_tbl.add_column("Pod",          max_width=30, no_wrap=True)
            pod_tbl.add_column("Namespace",    width=16)
            pod_tbl.add_column("CPU Req",      width=9,  justify="right")
            pod_tbl.add_column("CPU Act",      width=9,  justify="right")
            pod_tbl.add_column("Instance",     width=14)
            pod_tbl.add_column("Monthly",      width=12, justify="right")
            pod_tbl.add_column("Waste",        width=7,  justify="right")

            for p in report.top_wasteful_pods[:top]:
                pod_tbl.add_row(
                    _escape(p.pod),
                    _escape(p.namespace),
                    f"{p.cpu_request:.3f}",
                    f"{p.cpu_actual:.3f}",
                    _escape(p.node_instance_type),
                    f"[cyan]${p.est_monthly_cost:.2f}[/cyan]",
                    f"[yellow]{p.waste_percent}%[/yellow]",
                )
            console.print(pod_tbl)
            console.print()

        # AI analysis
        if report.claude_analysis:
            console.print(Panel(
                _escape(report.claude_analysis),
                title="[bold magenta]AI COST RECOMMENDATIONS[/bold magenta]",
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


def _trend_arrow(change_pct: float) -> str:
    """Return a coloured trend arrow string for a percentage change."""
    if change_pct > 1:
        return f"[red]↑ {abs(change_pct):.1f}%[/red]"
    if change_pct < -1:
        return f"[green]↓ {abs(change_pct):.1f}%[/green]"
    return "[dim]→[/dim]"


def _render_aws_analysis(analysis) -> None:
    """Render a full AWSCostAnalysis to the console with Rich."""
    a = analysis
    # 1 — Header panel
    change = _trend_arrow(a.month_change_percent)
    console.print(Panel(
        f"[bold]Total spend (last {a.period_days}d):[/bold]  "
        f"[bold red]${a.total_spend:,.2f}[/bold red]\n"
        f"[bold]Last month:[/bold]  [cyan]${a.last_month_spend:,.2f}[/cyan]  "
        f"({change} vs last month)\n"
        f"[bold]Forecast this month:[/bold]  [yellow]${a.forecast_this_month:,.2f}[/yellow]\n"
        f"[bold]Waste identified:[/bold]  "
        f"[bold yellow]${a.total_monthly_waste:,.2f}/mo[/bold yellow]  "
        f"([green]${a.total_monthly_waste * 12:,.2f}/yr[/green])",
        title="[bold cyan]AWS COST OPTIMIZATION[/bold cyan]",
        border_style="cyan",
        padding=(0, 1),
    ))
    console.print()

    # 2 — Spend by service
    if a.by_service:
        svc_tbl = Table(
            title="SPEND BY SERVICE",
            show_lines=False, header_style="bold cyan", border_style="dim",
        )
        svc_tbl.add_column("Service", min_width=28)
        svc_tbl.add_column("Cost", width=14, justify="right")
        svc_tbl.add_column("% of Bill", width=12, justify="right")
        svc_tbl.add_column("Trend", width=8, justify="center")
        total = a.total_spend or 1.0
        for svc, amt in list(a.by_service.items())[:15]:
            pct = round(amt / total * 100, 1)
            bar = "█" * max(1, int(pct / 5))
            # Per-service trend not available — use overall month trend as hint.
            svc_tbl.add_row(
                _escape(svc),
                f"[cyan]${amt:,.2f}[/cyan]",
                f"[dim]{bar}[/dim] {pct}%",
                _trend_arrow(a.month_change_percent),
            )
        console.print(svc_tbl)
        console.print()

    # 3 — Idle resources
    idle = a.idle_resources
    idle_rows: list[tuple[str, str, str, str]] = []
    for v in idle.unattached_volumes:
        idle_rows.append((
            "Unattached EBS", _escape(v.get("VolumeId", "")),
            f"${v.get('cost_per_month', 0):,.2f}", "[green]Safe[/green]"))
    for e in idle.unused_eips:
        idle_rows.append((
            "Unused EIP", _escape(e.get("PublicIp", "")),
            f"${e.get('cost_per_month', 0):,.2f}", "[green]Safe[/green]"))
    for s in idle.old_snapshots:
        idle_rows.append((
            "Old snapshot", _escape(s.get("SnapshotId", "")),
            f"${s.get('cost_per_month', 0):,.2f}", "[yellow]Manual[/yellow]"))
    for lb in idle.idle_load_balancers:
        idle_rows.append((
            "Idle LB", _escape(lb.get("LoadBalancerName", "")),
            f"${lb.get('cost_per_month', 0):,.2f}", "[yellow]Manual[/yellow]"))
    for si in idle.stopped_instances:
        idle_rows.append((
            "Stopped EC2", _escape(si.get("InstanceId", "")),
            f"${si.get('cost_per_month', 0):,.2f}", "[yellow]Manual[/yellow]"))

    if idle_rows:
        idle_tbl = Table(
            title="IDLE RESOURCES",
            show_lines=False, header_style="bold yellow", border_style="yellow",
        )
        idle_tbl.add_column("Type", min_width=16)
        idle_tbl.add_column("Resource", min_width=24)
        idle_tbl.add_column("Waste/mo", width=12, justify="right")
        idle_tbl.add_column("Action", width=10, justify="center")
        for row in idle_rows[:25]:
            idle_tbl.add_row(*row)
        console.print(idle_tbl)
        console.print()

    # 4 — Optimization opportunities
    opp_rows: list[tuple[str, str, str, str]] = []
    for o in a.ebs_opportunities:
        opp_rows.append((
            "gp2 → gp3", _escape(o.get("VolumeId", "")),
            f"${o.get('monthly_savings', 0):,.2f}", "[green]Auto[/green]"))
    for g in a.cloudwatch_logs.get("groups_no_retention", []):
        if g.get("cost_per_month", 0) < 0.01:
            continue
        opp_rows.append((
            "Log retention", _escape(g.get("logGroupName", "")),
            f"${g.get('cost_per_month', 0) * 0.7:,.2f}", "[green]Auto[/green]"))
    for r in a.rightsizing:
        if r.get("estimated_monthly_savings", 0) <= 0:
            continue
        opp_rows.append((
            "Rightsize", _escape(r.get("instance_id", "")),
            f"${r.get('estimated_monthly_savings', 0):,.2f}", "[yellow]Manual[/yellow]"))
    rvo = a.reserved_vs_ondemand
    if rvo.get("potential_reserved_savings", 0) > 0:
        opp_rows.append((
            "Savings Plan", f"{rvo.get('coverage_pct', 0)}% covered",
            f"${rvo.get('potential_reserved_savings', 0):,.2f}", "[yellow]Manual[/yellow]"))

    if opp_rows:
        opp_tbl = Table(
            title="OPTIMIZATION OPPORTUNITIES",
            show_lines=False, header_style="bold green", border_style="green",
        )
        opp_tbl.add_column("Type", min_width=16)
        opp_tbl.add_column("Resource", min_width=24)
        opp_tbl.add_column("Savings/mo", width=12, justify="right")
        opp_tbl.add_column("Fix", width=10, justify="center")
        for row in opp_rows[:25]:
            opp_tbl.add_row(*row)
        console.print(opp_tbl)
        console.print()

    # 4b — Spend anomalies
    anomalies = a.anomalies or {}
    anomaly_days = anomalies.get("anomaly_days", [])
    if anomaly_days:
        anom_tbl = Table(
            title=f"SPEND ANOMALIES (last {a.period_days} days)",
            show_lines=False, header_style="bold red", border_style="red",
        )
        anom_tbl.add_column("Date", width=12)
        anom_tbl.add_column("Spend", width=10, justify="right")
        anom_tbl.add_column("% Above Avg", width=13, justify="right")
        anom_tbl.add_column("Culprit Services", min_width=32)
        for day in anomaly_days[:15]:
            culprits = ", ".join(
                f"{c.get('service', '')} (+${c.get('delta', 0):,.0f})"
                for c in day.get("culprit_services", [])[:3]
            ) or "—"
            anom_tbl.add_row(
                _escape(day.get("date", "")),
                f"${day.get('amount', 0):,.2f}",
                f"[red]+{day.get('pct_above_mean', 0)}%[/red]",
                _escape(culprits),
            )
        console.print(anom_tbl)
        console.print()

    trending_up = anomalies.get("trending_up", [])
    if trending_up:
        console.print("[bold yellow]TRENDING UP (week-over-week)[/bold yellow]")
        for t in trending_up[:8]:
            console.print(
                f"  {_escape(t.get('service', '')):<28} "
                f"[cyan]${t.get('recent_avg', 0):,.2f}/day[/cyan]  "
                f"[red]↑ {t.get('change_pct', 0)}%[/red]  "
                f"[dim](was ${t.get('prior_avg', 0):,.2f}/day)[/dim]"
            )
        console.print()

    # 4c — Savings plan opportunity
    sp = a.savings_plans or {}
    sp_recs = sp.get("recommendations", [])
    if sp_recs:
        sp_tbl = Table(
            title="SAVINGS PLAN OPPORTUNITY",
            show_lines=False, header_style="bold green", border_style="green",
        )
        sp_tbl.add_column("Plan", width=14)
        sp_tbl.add_column("Commitment/hr", width=16, justify="right")
        sp_tbl.add_column("Monthly Savings", width=18, justify="right")
        sp_tbl.add_column("Savings %", width=12, justify="right")
        for rec in sp_recs:
            sp_tbl.add_row(
                f"{_escape(rec.get('term', ''))} SP",
                f"${rec.get('hourly_commitment', 0):,.2f}/hr",
                f"[green]${rec.get('estimated_monthly_savings', 0):,.2f}/month[/green]",
                f"{rec.get('estimated_savings_pct', 0)}%",
            )
        console.print(sp_tbl)
        coverage = sp.get("existing_sp_coverage_pct", 0)
        rec_text = sp.get("recommendation", "")
        line = f"Current on-demand coverage: {coverage}%"
        if rec_text:
            line += f"  →  {rec_text}"
        console.print(f"[dim]{_escape(line)}[/dim]")
        console.print()

    # 4d — Cost by environment tag
    cbt = a.cost_by_tag or {}
    by_tag = cbt.get("by_tag", {})
    if by_tag:
        tag_key = cbt.get("tag_key", "Environment")
        total_tagged = sum(by_tag.values()) or 1.0
        console.print(f"[bold cyan]COST BY {tag_key.upper()} TAG[/bold cyan]")
        for value, amt in by_tag.items():
            pct = round(amt / total_tagged * 100)
            label = f"{value}:"
            line = f"  {label:<11} ${amt:,.2f}  ({pct}%)"
            if value == "untagged":
                line += (
                    f"  [yellow]← ${amt:,.0f}/month impossible to attribute[/yellow]"
                )
            console.print(_escape(line) if value != "untagged" else line)
        console.print()

    # 5 — AI analysis
    if a.claude_analysis:
        console.print(Panel(
            _escape(a.claude_analysis),
            title="[bold magenta]AI FINOPS ANALYSIS[/bold magenta]",
            border_style="magenta",
            padding=(0, 1),
        ))
        console.print()

    # 6 — Footer
    total_savings = sum(f.monthly_savings for f in a.savings_plan)
    auto_count = sum(1 for f in a.savings_plan if f.auto_fixable)
    console.print(Panel(
        f"[bold green]Total potential savings: "
        f"${total_savings:,.2f}/month  (${total_savings * 12:,.2f}/year)[/bold green]\n"
        f"[dim]{len(a.savings_plan)} opportunity(ies), "
        f"{auto_count} auto-fixable.[/dim]\n\n"
        f"  Apply safe fixes:  [bold]agent cost fix --auto[/bold]\n"
        f"  Review everything: [bold]agent cost fix --all[/bold]\n"
        f"  Monitor spend:     [bold]agent cost monitor[/bold]",
        title="[bold]NEXT STEPS[/bold]",
        border_style="dim",
        padding=(0, 1),
    ))
    console.print()


@cost_app.command("aws")
def cost_aws(
    days: int = typer.Option(
        30,
        "--days", "-d",
        help="Number of past days to query from Cost Explorer.",
        show_default=True,
    ),
    fix: bool = typer.Option(
        False,
        "--fix",
        help="After the analysis, jump straight into applying safe fixes.",
        show_default=True,
    ),
) -> None:
    """Full AWS cost optimization analysis (spend, waste, opportunities, AI plan)."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.cost import CostAnalysisSkill

        skill = CostAnalysisSkill()
        console.print()
        console.print(Rule("[bold cyan]AWS Cost Optimization[/bold cyan]"))
        console.print()

        with console.status(
            f"[bold green]Analysing AWS account (last {days} days)...",
            spinner="dots",
        ):
            analysis = skill.full_aws_analysis(days)

        _render_aws_analysis(analysis)
        _cost_footer(get_session_total())

        if fix:
            console.print()
            _apply_fixes(analysis.savings_plan, auto=True, all_fixes=False,
                         fix_id=None, dry_run=False)

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


def _apply_fixes(
    plan: list,
    auto: bool,
    all_fixes: bool,
    fix_id: str | None,
    dry_run: bool,
) -> None:
    """Walk a savings plan and apply / review fixes interactively."""
    from agent.skills.cost import CostAnalysisSkill

    skill = CostAnalysisSkill()

    if fix_id:
        targets = [f for f in plan if f.id == fix_id]
        if not targets:
            console.print(f"[yellow]No fix found with id '{_escape(fix_id)}'.[/yellow]")
            return
    elif all_fixes:
        targets = list(plan)
    else:
        # --auto (default): only auto-fixable fixes.
        targets = [f for f in plan if f.auto_fixable]

    if not targets:
        console.print("[green]Nothing to do — no matching fixes.[/green]")
        return

    applied = 0
    saved = 0.0
    for fix in targets:
        risk_col = {
            "safe": "green", "low": "cyan", "medium": "yellow", "high": "red",
        }.get(fix.risk, "dim")
        body = (
            f"[bold]{_escape(fix.title)}[/bold]\n"
            f"{_escape(fix.description)}\n\n"
            f"  Savings:  [bold green]${fix.monthly_savings:,.2f}/mo[/bold green]  "
            f"([green]${fix.annual_savings:,.2f}/yr[/green])\n"
            f"  Effort:   {_escape(fix.effort)}   "
            f"Risk: [{risk_col}]{_escape(fix.risk)}[/{risk_col}]   "
            f"Auto: {'[green]yes[/green]' if fix.auto_fixable else '[yellow]no[/yellow]'}"
        )
        if fix.fix_command:
            body += f"\n\n  [dim]{_escape(fix.fix_command)}[/dim]"
        console.print(Panel(
            body,
            title=f"[bold]#{fix.priority}  {_escape(fix.category)}[/bold]",
            border_style=risk_col,
            padding=(0, 1),
        ))

        if dry_run:
            console.print("  [dim]dry-run: no changes made.[/dim]\n")
            continue

        if not fix.auto_fixable:
            console.print(
                "  [yellow]Manual fix — run the command above or use the "
                "AWS console.[/yellow]\n"
            )
            continue

        if not typer.confirm(f"  Apply this fix (saves ${fix.monthly_savings:,.2f}/mo)?",
                             default=False):
            console.print("  [dim]skipped.[/dim]\n")
            continue

        with console.status("[bold green]Applying fix...", spinner="dots"):
            ok = skill.apply_cost_fix(fix)
        if ok:
            applied += 1
            saved += fix.monthly_savings
            console.print(f"  [green]✓ Applied.[/green]\n")
        else:
            console.print("  [red]✗ Failed (see logs). Skipping.[/red]\n")

    if not dry_run:
        console.print(Panel(
            f"[bold green]Applied {applied} fix(es), "
            f"saving ${saved:,.2f}/month (${saved * 12:,.2f}/year).[/bold green]",
            border_style="green",
            padding=(0, 1),
        ))
        console.print()


@cost_app.command("fix")
def cost_fix(
    auto: bool = typer.Option(False, "--auto", help="Apply all auto-fixable fixes with confirmation."),
    fix_id: str | None = typer.Option(None, "--id", help="Apply a specific fix by ID."),
    all_fixes: bool = typer.Option(False, "--all", help="Review all fixes including manual."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen without making changes."),
    days: int = typer.Option(30, "--days", "-d", help="Lookback window for the analysis."),
) -> None:
    """Apply AWS cost-saving fixes (re-runs the analysis to find them)."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.cost import CostAnalysisSkill

        skill = CostAnalysisSkill()
        console.print()
        console.print(Rule("[bold cyan]Apply Cost Fixes[/bold cyan]"))
        console.print()

        with console.status("[bold green]Re-analysing AWS account...", spinner="dots"):
            analysis = skill.full_aws_analysis(days)

        if not analysis.savings_plan:
            console.print("[green]No optimisation opportunities found.[/green]")
            console.print()
            _cost_footer(get_session_total())
            return

        # Default behaviour when no mode flag given is --auto.
        if not (auto or all_fixes or fix_id):
            auto = True

        _apply_fixes(analysis.savings_plan, auto=auto, all_fixes=all_fixes,
                     fix_id=fix_id, dry_run=dry_run)
        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@cost_app.command("monitor")
def cost_monitor(
    alert_threshold: float = typer.Option(20.0, "--alert-threshold", help="Alert if daily spend exceeds this ($)."),
) -> None:
    """Check recent daily AWS spend and alert (Slack) if it exceeds a threshold."""
    try:
        from agent.integrations import aws_cost
        from agent.integrations.slack import send_alert_generic

        console.print()
        console.print(Rule("[bold cyan]AWS Spend Monitor[/bold cyan]"))
        console.print()

        with console.status("[bold green]Checking last 7 days of spend...", spinner="dots"):
            data = aws_cost.get_cost_and_usage(days=7)

        daily = data.get("daily", [])
        if not daily:
            console.print(
                "[yellow]No Cost Explorer data available "
                "(check boto3 + ce:GetCostAndUsage permission).[/yellow]"
            )
            console.print()
            return

        amounts = [d.get("amount", 0.0) for d in daily]
        avg = round(sum(amounts) / len(amounts), 2) if amounts else 0.0
        latest = daily[-1]

        console.print(Panel(
            f"[bold]7-day daily average:[/bold]  [cyan]${avg:,.2f}/day[/cyan]\n"
            f"[bold]Most recent day:[/bold]  "
            f"[cyan]${latest.get('amount', 0):,.2f}[/cyan] ({_escape(latest.get('date', ''))})\n"
            f"[bold]Alert threshold:[/bold]  [yellow]${alert_threshold:,.2f}/day[/yellow]",
            title="[bold cyan]DAILY SPEND[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        ))
        console.print()

        if avg > alert_threshold:
            console.print(
                f"[bold red]ALERT: daily average ${avg:,.2f} exceeds "
                f"threshold ${alert_threshold:,.2f}.[/bold red]"
            )
            sent = send_alert_generic(
                title="AWS daily spend over threshold",
                message=(
                    f"7-day average daily spend is ${avg:,.2f}, above the "
                    f"${alert_threshold:,.2f}/day threshold."
                ),
                severity="warning",
                fields={
                    "Avg/day": f"${avg:,.2f}",
                    "Threshold": f"${alert_threshold:,.2f}",
                    "Latest day": f"${latest.get('amount', 0):,.2f}",
                },
            )
            if sent:
                console.print("[dim]Slack alert sent.[/dim]")
            else:
                console.print("[dim]Slack not configured — set SLACK_WEBHOOK_URL to enable alerts.[/dim]")
        else:
            console.print(
                f"[green]OK — daily average ${avg:,.2f} is within the "
                f"${alert_threshold:,.2f}/day threshold.[/green]"
            )
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@cost_app.command("optimize")
def cost_optimize(
    namespace: str = typer.Option(
        "all",
        "--namespace", "-n",
        help="Namespace to optimise. Defaults to all.",
        show_default=True,
    ),
    min_waste: int = typer.Option(
        50,
        "--min-waste",
        help="Only show pods with at least this waste %.",
        show_default=True,
    ),
    ai: bool = typer.Option(
        True,
        "--ai/--no-ai",
        help="Run Claude AI optimisation plan (adds ~5s).",
        show_default=True,
    ),
    usage: bool = typer.Option(
        True,
        "--usage/--no-usage",
        help="Fetch actual CPU/memory via kubectl top for waste detection (needs metrics-server).",
        show_default=True,
    ),
) -> None:
    """Show right-sizing commands for the most wasteful workloads."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.cost import CostAnalysisSkill

        skill = CostAnalysisSkill()
        console.print()
        console.print(Rule("[bold yellow]Right-Sizing Recommendations[/bold yellow]"))
        console.print()

        with console.status("[bold green]Scanning for waste...", spinner="dots"):
            report = skill.analyze_cluster_cost(namespace, with_ai=ai, with_usage=usage)

        targets = [
            d for d in report.deployments
            if d.waste_label == "over-provisioned" and d.waste_percent >= min_waste
        ]

        if not targets:
            console.print(
                f"[green]No deployments with ≥{min_waste}% CPU waste found. "
                "Cluster looks well-sized.[/green]"
            )
            console.print()
            _cost_footer(get_session_total())
            return

        console.print(
            f"[yellow]{len(targets)} over-provisioned deployment(s) "
            f"with ≥{min_waste}% waste:[/yellow]\n"
        )

        for d in sorted(targets, key=lambda x: x.waste_percent, reverse=True):
            # Suggest ~120% of actual usage as new request
            suggested_cpu = round(max(d.total_cpu_actual * 1.2, 0.05), 3)
            savings = round(
                d.est_monthly_cost * (d.waste_percent / 100) * 0.7, 2
            )
            console.print(Panel(
                f"[bold]{_escape(d.namespace)}/{_escape(d.deployment)}[/bold]  "
                f"[dim]({d.pod_count} pod{'s' if d.pod_count != 1 else ''})[/dim]\n\n"
                f"  CPU request: [red]{d.total_cpu_request:.3f}[/red] cores  →  "
                f"actual: [green]{d.total_cpu_actual:.3f}[/green] cores  "
                f"([yellow]{d.waste_percent}% idle[/yellow])\n"
                f"  Est. cost:   [cyan]${d.est_monthly_cost:.2f}/mo[/cyan]  |  "
                f"Potential saving: [bold green]~${savings:.2f}/mo[/bold green]\n\n"
                f"  [dim]kubectl set resources deployment/{_escape(d.deployment)} "
                f"-n {_escape(d.namespace)} "
                f"--requests=cpu={suggested_cpu}[/dim]",
                title=f"[yellow]{d.waste_percent}% OVER-PROVISIONED[/yellow]",
                border_style="yellow",
                padding=(0, 1),
            ))
            console.print()

        total_potential = sum(
            round(d.est_monthly_cost * (d.waste_percent / 100) * 0.7, 2)
            for d in targets
        )
        console.print(
            f"[bold green]Total potential savings: ~${total_potential:.2f}/month[/bold green]"
        )
        console.print()

        if report.claude_analysis:
            console.print(Panel(
                _escape(report.claude_analysis),
                title="[bold magenta]AI OPTIMISATION PLAN[/bold magenta]",
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


@cost_app.command("report")
def cost_report(
    namespace: str = typer.Option(
        "all",
        "--namespace", "-n",
        help="Namespace to include. Defaults to all.",
        show_default=True,
    ),
    aws_days: int = typer.Option(
        0,
        "--aws-days",
        help="Also pull AWS Cost Explorer data for this many days (0 = skip).",
        show_default=True,
    ),
    ai: bool = typer.Option(
        True,
        "--ai/--no-ai",
        help="Run Claude AI analysis (adds ~5s).",
        show_default=True,
    ),
    usage: bool = typer.Option(
        False,
        "--usage/--no-usage",
        help="Fetch actual CPU/memory via kubectl top (adds 10-20s, needs metrics-server).",
        show_default=True,
    ),
    save: bool = typer.Option(
        False,
        "--save",
        help="Save the report as markdown under data/reports/.",
        show_default=True,
    ),
) -> None:
    """Full cost report: K8s estimates + optional AWS billing overlay."""
    try:
        from agent.observability.costs import get_session_total
        from agent.skills.cost import CostAnalysisSkill

        skill = CostAnalysisSkill()
        console.print()
        console.print(Rule("[bold cyan]Full Cost Report[/bold cyan]"))
        console.print()

        with console.status("[bold green]Building cost report...", spinner="dots"):
            k8s = skill.analyze_cluster_cost(namespace, with_ai=ai, with_usage=usage)

        # ── K8s summary ──────────────────────────────────────────────────
        waste_col = "yellow" if k8s.waste_percent >= 30 else "green"
        console.print(Panel(
            f"[bold]Cluster:[/bold]   {_escape(k8s.cluster_name)}   "
            f"[bold]Nodes:[/bold] {k8s.total_nodes}\n"
            f"[bold]Node cost:[/bold] [cyan]${k8s.total_monthly_node_cost:.2f}/mo[/cyan]  |  "
            f"[bold]Requested:[/bold] [cyan]${k8s.total_requested_cost:.2f}/mo[/cyan]  |  "
            f"[bold]Waste:[/bold] [{waste_col}]${k8s.total_waste_cost:.2f}/mo ({k8s.waste_percent}%)[/{waste_col}]",
            title="[bold cyan]K8S COST ESTIMATES[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        ))
        console.print()

        # ── AWS overlay ───────────────────────────────────────────────────
        if aws_days > 0:
            with console.status(
                f"[bold green]Fetching AWS billing (last {aws_days} days)...",
                spinner="dots",
            ):
                try:
                    aws = skill.get_aws_costs(aws_days)
                    comparison = skill.compare_k8s_vs_aws(k8s, aws)
                    util_col = (
                        "green" if comparison["utilization_percent"] >= 70
                        else "yellow" if comparison["utilization_percent"] >= 40
                        else "red"
                    )
                    console.print(Panel(
                        f"[bold]EC2 bill (last {aws_days}d):[/bold]  "
                        f"[bold red]${comparison['ec2_bill']:.2f}[/bold red]\n"
                        f"[bold]K8s requested:[/bold]  "
                        f"[cyan]${comparison['k8s_requested']:.2f}/mo[/cyan]\n"
                        f"[bold]Idle overhead:[/bold]  "
                        f"[yellow]${comparison['idle_overhead']:.2f}/mo[/yellow]\n"
                        f"[bold]Utilisation:[/bold]   "
                        f"[{util_col}]{comparison['utilization_percent']}%[/{util_col}]\n\n"
                        f"[dim]{_escape(comparison['insight'])}[/dim]",
                        title="[bold cyan]AWS BILLING OVERLAY[/bold cyan]",
                        border_style="cyan",
                        padding=(0, 1),
                    ))
                    console.print()
                except (RuntimeError, PermissionError) as exc:
                    console.print(
                        f"[yellow]AWS Cost Explorer skipped:[/yellow] {exc}\n"
                    )

        # ── AI analysis ───────────────────────────────────────────────────
        if k8s.claude_analysis:
            console.print(Panel(
                _escape(k8s.claude_analysis),
                title="[bold magenta]AI COST ANALYSIS[/bold magenta]",
                border_style="magenta",
                padding=(0, 1),
            ))
            console.print()

        console.print(f"[dim]Generated: {k8s.generated_at}[/dim]")
        console.print()

        if save:
            from datetime import datetime as _dt
            from pathlib import Path as _Path

            reports_dir = _Path("data/reports")
            reports_dir.mkdir(parents=True, exist_ok=True)
            date_str = _dt.now().strftime("%Y-%m-%d")
            out_path = reports_dir / f"cost-{date_str}.md"

            md: list[str] = [
                f"# Cost Report — {k8s.cluster_name}",
                "",
                f"_Generated: {k8s.generated_at}_",
                "",
                "## K8s Cost Estimates",
                "",
                f"- Nodes: {k8s.total_nodes}",
                f"- Node cost: ${k8s.total_monthly_node_cost:,.2f}/mo",
                f"- Requested: ${k8s.total_requested_cost:,.2f}/mo",
                f"- Waste: ${k8s.total_waste_cost:,.2f}/mo ({k8s.waste_percent}%)",
                "",
            ]
            if k8s.deployments:
                md += ["## Top Deployments by Cost", "",
                       "| Deployment | Namespace | Pods | Monthly | Waste |",
                       "|---|---|---|---|---|"]
                for d in k8s.deployments[:20]:
                    md.append(
                        f"| {d.deployment} | {d.namespace} | {d.pod_count} | "
                        f"${d.est_monthly_cost:,.2f} | {d.waste_percent}% "
                        f"{d.waste_label} |"
                    )
                md.append("")
            if k8s.claude_analysis:
                md += ["## AI Cost Analysis", "", k8s.claude_analysis, ""]

            out_path.write_text("\n".join(md), encoding="utf-8")
            console.print(f"[green]Report saved to[/green] [bold]{out_path}[/bold]")
            console.print()

        _cost_footer(get_session_total())

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Monitor daemon
# ---------------------------------------------------------------------------

@monitor_app.command("start")
def monitor_start(
    slack:    bool = typer.Option(True,  "--slack/--no-slack", help="Send alerts to Slack."),
    interval: int  = typer.Option(60,    "--interval",         help="Check interval in seconds.", show_default=True),
) -> None:
    """
    Continuous monitoring daemon — checks pod health + resources every --interval seconds,
    security every hour, cost every 6 hours. Deduplicated Slack alerts.

    Run in a tmux/screen session for 24/7 monitoring.
    """
    import time as _time
    from agent.integrations.slack import is_configured as slack_ok, send_alert_generic, test_connection
    from agent.integrations.alert_dedup import AlertDeduplicator, make_fingerprint
    from agent.skills.k8s import K8sSkill
    from agent.skills.resource_monitor import ResourceMonitorSkill
    from agent.skills.security import SecurityAuditSkill
    from rich.table import Table
    from rich.live import Live

    dedup   = AlertDeduplicator()
    k8s_sk  = K8sSkill()
    res_sk  = ResourceMonitorSkill()
    sec_sk  = SecurityAuditSkill()

    slack_live  = slack and slack_ok()
    log_lines: list[str] = []
    alerts_today = 0
    sec_timer    = 0
    cost_timer   = 0

    def _log(msg: str) -> None:
        ts = __import__("datetime").datetime.now().strftime("%H:%M:%S")
        log_lines.append(f"{ts}  {msg}")
        if len(log_lines) > 12:
            log_lines.pop(0)

    def _send(title: str, msg: str, sev: str, pod: str, ns: str, etype: str, fix: str | None = None) -> bool:
        nonlocal alerts_today
        fp = make_fingerprint(pod, ns, etype)
        if dedup.should_send(fp, sev):
            ok = send_alert_generic(title=title, message=msg, severity=sev, fix_command=fix,
                                    fields={"Pod/Resource": pod, "Namespace": ns})
            if ok:
                dedup.record_sent(fp, sev)
                alerts_today += 1
                return True
        return False

    if slack_live:
        console.print("[green]Slack: connected — alerts will be sent[/green]")
        test_connection()
    else:
        console.print("[yellow]Slack: not configured (run without --slack or set SLACK_WEBHOOK_URL)[/yellow]")

    console.print(f"[dim]Monitor started — interval={interval}s. Ctrl+C to stop.[/dim]\n")

    _log("Monitor started")

    def _build_panel() -> Table:
        stats = dedup.get_stats()
        tbl   = Table(box=None, show_header=False, padding=(0, 1))
        tbl.add_column(width=14, style="dim")
        tbl.add_column()
        for line in log_lines:
            tbl.add_row("", line)
        tbl.add_row("", "")
        tbl.add_row("Firing alerts:", str(stats["firing"]))
        tbl.add_row("Sent today:",    str(stats["sent_today"]))
        tbl.add_row("Slack:",         "[green]ON[/green]" if slack_live else "[dim]off[/dim]")
        tbl.add_row("", "[dim]Ctrl+C to stop[/dim]")
        return tbl

    try:
        with Live(console=console, refresh_per_second=0.5, auto_refresh=False) as live:
            while True:
                # ── Pod health check ──────────────────────────────────────
                try:
                    issues = k8s_sk.full_cluster_scan("all")
                    crit   = [i for i in issues if i.get("severity") == "critical"]
                    if crit:
                        _log(f"Pod check   ⚠ {len(crit)} critical issue(s)")
                        if slack_live:
                            for iss in crit:
                                sent = _send(
                                    title=f"Pod issue: {iss.get('resource','')}",
                                    msg=iss.get("root_cause", iss.get("description", "")),
                                    sev="critical",
                                    pod=iss.get("resource", ""),
                                    ns=iss.get("namespace", ""),
                                    etype=iss.get("problem_type", "K8S"),
                                    fix=iss.get("fix_command"),
                                )
                                if sent:
                                    _log(f"  ALERT → {iss.get('resource','')}")
                    else:
                        _log("Pod check   ✓ all healthy")
                except Exception as exc:
                    _log(f"Pod check   ✗ error: {exc}")

                # ── Resource check ────────────────────────────────────────
                try:
                    report = res_sk.scan_resources("all")
                    c_alerts = [a for a in report.alerts if a.severity == "critical"]
                    if c_alerts:
                        _log(f"Resources   ⚠ {len(c_alerts)} critical alert(s)")
                        if slack_live:
                            for a in c_alerts:
                                _send(
                                    title=f"Resource: {a.alert_type} — {a.pod_or_node}",
                                    msg=a.recommendation,
                                    sev="critical",
                                    pod=a.pod_or_node,
                                    ns=a.namespace or "",
                                    etype=a.alert_type,
                                    fix=a.fix_command,
                                )
                    else:
                        w = sum(1 for a in report.alerts if a.severity == "warning")
                        _log(f"Resources   ✓ {w} warning(s)" if w else "Resources   ✓ clean")
                except Exception as exc:
                    _log(f"Resources   ✗ error: {exc}")

                # ── Security check (every hour) ───────────────────────────
                sec_timer += interval
                if sec_timer >= 3600:
                    sec_timer = 0
                    try:
                        sec_report = sec_sk.run_audit("all")
                        new_crit   = [f for f in sec_report.findings
                                      if f.severity == "critical" and f.actionable]
                        _log(f"Security    ✓ {len(new_crit)} critical finding(s)")
                        if slack_live and new_crit:
                            for f in new_crit[:5]:
                                _send(
                                    title=f"Security: {f.title}",
                                    msg=f.description,
                                    sev="critical",
                                    pod=f.resource,
                                    ns=f.namespace or "",
                                    etype=f.check_type,
                                    fix=f.fix_command,
                                )
                    except Exception as exc:
                        _log(f"Security    ✗ error: {exc}")

                live.update(
                    Panel(
                        _build_panel(),
                        title="[bold cyan]INFRAGPT MONITOR — RUNNING[/bold cyan]",
                        border_style="cyan",
                        padding=(0, 1),
                    ),
                    refresh=True,
                )

                _time.sleep(interval)

    except KeyboardInterrupt:
        console.print("\n[dim]Monitor stopped.[/dim]")


@monitor_app.command("test-slack")
def monitor_test_slack() -> None:
    """Send a test message to verify Slack webhook is working."""
    from agent.integrations.slack import is_configured, test_connection
    if not is_configured():
        console.print("[red]Slack not configured.[/red] Set SLACK_WEBHOOK_URL in .env")
        raise typer.Exit(1)
    with console.status("Sending test message…"):
        ok = test_connection()
    if ok:
        console.print("[bold green]✓ Slack test message sent![/bold green]")
    else:
        console.print("[bold red]✗ Slack test failed — check webhook URL[/bold red]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Deploy commands — GitHub webhook mapping management
# ---------------------------------------------------------------------------

_MAPPINGS_FILE = "data/webhook_mappings.yaml"


@deploy_app.command("setup")
def deploy_setup() -> None:
    """Quick webhook setup — generates secret + adds a mapping in under a minute."""
    import os, secrets as _secrets
    from agent.core.models import WebhookMapping
    from agent.integrations.mapping_loader import get_loader

    console.print()
    console.print(Rule("[bold cyan]GitHub Webhook Setup[/bold cyan]"))
    console.print()

    loader = get_loader()

    # ── Secret: generate once, reuse if already set ───────────────────────
    existing_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if existing_secret:
        console.print(f"  [dim]Webhook secret already set:[/dim] [yellow]{existing_secret[:8]}…[/yellow]")
        webhook_secret = existing_secret
    else:
        webhook_secret = _secrets.token_hex(16)
        # Write to .env
        env_path = Path(".env")
        env_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        env_lines = [l for l in env_lines if not l.startswith("GITHUB_WEBHOOK_SECRET=")]
        env_lines.append(f"GITHUB_WEBHOOK_SECRET={webhook_secret}")
        env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        os.environ["GITHUB_WEBHOOK_SECRET"] = webhook_secret
        console.print(Panel(
            f"[bold yellow]Webhook secret (save this — shown once):[/bold yellow]\n\n"
            f"  [bold cyan]{webhook_secret}[/bold cyan]\n\n"
            "[dim]Saved to .env as GITHUB_WEBHOOK_SECRET[/dim]",
            border_style="yellow", padding=(1, 2),
        ))

    console.print()

    # ── Mapping loop ──────────────────────────────────────────────────────
    while True:
        console.print("[bold]Add a repo mapping:[/bold]")
        console.print()

        repo       = typer.prompt("  GitHub repo (owner/name)")
        if "/" not in repo:
            console.print("[red]  Must be owner/name format — try again.[/red]\n")
            continue

        branch     = typer.prompt("  Branch", default="main")
        deployment = typer.prompt("  Kubernetes deployment name")
        namespace  = typer.prompt("  Kubernetes namespace", default="default")
        img_prefix = typer.prompt("  Image prefix (ECR URL without tag, or leave blank)", default="")

        mapping = WebhookMapping(
            repo                  = repo,
            branch                = branch,
            deployment            = deployment,
            namespace             = namespace,
            image_prefix          = img_prefix,
            auto_approve_low_risk = False,
        )

        console.print()
        with console.status("  Validating against cluster…", spinner="dots"):
            result = loader.validate_mapping(mapping)

        if result.valid:
            console.print("  [green]✓ Cluster validation passed[/green]")
        else:
            for err in result.errors:
                console.print(f"  [yellow]⚠ {_escape(err)}[/yellow]")

        loader.add_mapping(mapping)
        console.print(
            f"  [bold green]✓ Saved:[/bold green] "
            f"[cyan]{_escape(repo)}:{_escape(branch)}[/cyan] → "
            f"[green]{_escape(deployment)}/{_escape(namespace)}[/green]"
        )
        console.print()

        if not typer.confirm("  Add another repo?", default=False):
            break

    # ── Instructions ──────────────────────────────────────────────────────
    cfg  = loader.load()
    repos_txt = "\n".join(
        f"    github.com/{m.repo}/settings/hooks"
        for m in cfg.mappings
    )
    console.print()
    console.print(Panel(
        "[bold]For each repo, add a GitHub webhook:[/bold]\n\n"
        f"{repos_txt}\n\n"
        "  Settings:\n"
        "    Payload URL :  http://YOUR-SERVER-IP:8080/webhook/github\n"
        "    Content type:  application/json\n"
        f"    Secret      :  {webhook_secret}\n"
        "    Events      :  Just the push event\n\n"
        "Then start the listener:\n"
        "  [bold cyan]agent deploy webhook-start --port 8080[/bold cyan]",
        title="[bold green]Next Steps[/bold green]",
        border_style="green", padding=(1, 2),
    ))
    console.print()
    console.print(f"  [dim]View all mappings:[/dim]  [bold]agent deploy mappings[/bold]")
    console.print(f"  [dim]Test a mapping:   [/dim]  [bold]agent deploy test-webhook --repo {_escape(repo)}[/bold]")
    console.print()


@deploy_app.command("mappings")
def deploy_mappings() -> None:
    """List all configured GitHub → Kubernetes webhook mappings."""
    from agent.integrations.mapping_loader import get_loader

    cfg = get_loader().load()
    console.print()

    if not cfg.mappings:
        console.print(Panel(
            "[dim]No webhook mappings configured yet.[/dim]\n\n"
            "Run [bold]agent setup[/bold] → Step 5, or:\n"
            "  [bold]agent deploy add-mapping --repo owner/repo --deployment myapp --namespace default[/bold]",
            title="[bold]GitHub Webhook Mappings[/bold]",
            border_style="dim",
        ))
        console.print()
        return

    tbl = Table(
        title=f"[bold]GitHub Webhook Mappings[/bold]  [dim]({len(cfg.mappings)} configured)[/dim]",
        show_lines=True,
        header_style="bold cyan",
        border_style="dim",
    )
    tbl.add_column("Repo",          max_width=32, no_wrap=True)
    tbl.add_column("Branch",        width=10)
    tbl.add_column("Deployment",    width=18)
    tbl.add_column("Namespace",     width=14)
    tbl.add_column("Image Prefix",  max_width=36, no_wrap=True)
    tbl.add_column("Auto Low-Risk", justify="center", width=13)

    for m in cfg.mappings:
        auto = "[green]Yes[/green]" if m.auto_approve_low_risk else "[dim]No[/dim]"
        prefix = _escape(m.image_prefix) if m.image_prefix else "[dim]—[/dim]"
        tbl.add_row(
            f"[cyan]{_escape(m.repo)}[/cyan]",
            _escape(m.branch),
            _escape(m.deployment),
            _escape(m.namespace),
            prefix,
            auto,
        )

    console.print(tbl)
    console.print()

    if cfg.webhook_secret:
        console.print(
            f"  [dim]Webhook secret:[/dim]  [yellow]{cfg.webhook_secret[:8]}…[/yellow]  "
            "[dim](set GITHUB_WEBHOOK_SECRET in .env)[/dim]"
        )
    console.print(
        "  [dim]Listener:[/dim]  [bold]agent deploy webhook-start --port 8080[/bold]"
    )
    console.print()


@deploy_app.command("add-mapping")
def deploy_add_mapping(
    repo: str = typer.Option(..., "--repo",        help="GitHub repo in owner/name format."),
    deployment: str = typer.Option(..., "--deployment", help="Kubernetes deployment name."),
    namespace: str = typer.Option("default", "--namespace", "-n", help="Kubernetes namespace.", show_default=True),
    branch: str = typer.Option("main",    "--branch",     help="Branch to watch.", show_default=True),
    image_prefix: str = typer.Option("",      "--image-prefix", help="ECR/registry URL without tag."),
    auto_approve: bool = typer.Option(False,   "--auto-approve", help="Auto-approve LOW risk deploys."),
) -> None:
    """Add a single GitHub → Kubernetes webhook mapping."""
    from agent.core.models import WebhookMapping
    from agent.integrations.mapping_loader import get_loader

    console.print()

    if "/" not in repo:
        _print_error("Repo must be in 'owner/name' format (e.g. salmonstone/infragpt)")
        raise typer.Exit(1)

    mapping = WebhookMapping(
        repo                  = repo,
        branch                = branch,
        deployment            = deployment,
        namespace             = namespace,
        image_prefix          = image_prefix,
        auto_approve_low_risk = auto_approve,
    )

    loader = get_loader()

    with console.status("[bold cyan]Validating against cluster...", spinner="dots"):
        result = loader.validate_mapping(mapping)

    if result.valid:
        console.print("[bold green]✓ Cluster validation passed[/bold green]")
    else:
        for err in result.errors:
            console.print(f"  [yellow]⚠ {_escape(err)}[/yellow]")
        if not typer.confirm("  Save mapping anyway?", default=True):
            console.print("[dim]Aborted.[/dim]")
            raise typer.Exit(0)

    loader.add_mapping(mapping)

    console.print(Panel(
        f"  [dim]Repo      :[/dim] [cyan]{_escape(repo)}[/cyan]  [dim]branch=[/dim][bold]{_escape(branch)}[/bold]\n"
        f"  [dim]Deployment:[/dim] [green]{_escape(deployment)}[/green]  [dim]namespace=[/dim]{_escape(namespace)}\n"
        f"  [dim]Image pfx :[/dim] {_escape(image_prefix) if image_prefix else '[dim]—[/dim]'}\n"
        f"  [dim]Auto-low  :[/dim] {'[green]Yes[/green]' if auto_approve else '[dim]No[/dim]'}",
        title="[bold green]✓ Mapping Saved[/bold green]",
        border_style="green",
        padding=(0, 2),
    ))
    console.print()
    console.print(f"  [dim]View all:[/dim]  [bold]agent deploy mappings[/bold]")
    console.print(
        f"  [dim]Test it:[/dim]   [bold]agent deploy test-webhook --repo {_escape(repo)} --branch {_escape(branch)}[/bold]"
    )
    console.print()


@deploy_app.command("remove-mapping")
def deploy_remove_mapping(
    repo: str = typer.Option(...,    "--repo",   help="GitHub repo in owner/name format."),
    branch: str = typer.Option("main", "--branch", help="Branch of the mapping to remove.", show_default=True),
) -> None:
    """Remove a webhook mapping from webhook_mappings.yaml."""
    from agent.integrations.mapping_loader import get_loader

    console.print()
    loader  = get_loader()
    cfg     = loader.load()
    match   = next((m for m in cfg.mappings if m.repo == repo and m.branch == branch), None)

    if match is None:
        _print_error(f"No mapping found for {repo}:{branch}")
        console.print(f"  [dim]Run [bold]agent deploy mappings[/bold] to see configured repos.[/dim]")
        raise typer.Exit(1)

    console.print(Panel(
        f"  [dim]Repo      :[/dim] [cyan]{_escape(match.repo)}:{_escape(match.branch)}[/cyan]\n"
        f"  [dim]Deployment:[/dim] [red]{_escape(match.deployment)}[/red]  [dim]/{_escape(match.namespace)}[/dim]",
        title="[bold red]Remove Mapping[/bold red]",
        border_style="red",
        padding=(0, 2),
    ))
    console.print()

    if not typer.confirm("  Confirm removal?", default=False):
        console.print("[dim]Cancelled.[/dim]")
        raise typer.Exit(0)

    loader.remove_mapping(repo, branch)
    console.print(f"[bold green]✓ Mapping removed:[/bold green] {_escape(repo)}:{_escape(branch)}")
    console.print()


@deploy_app.command("test-webhook")
def deploy_test_webhook(
    repo: str = typer.Option(...,    "--repo",   help="GitHub repo to simulate push for."),
    branch: str = typer.Option("main", "--branch", help="Branch to simulate.", show_default=True),
    tag: str = typer.Option("latest", "--tag",    help="Image tag to simulate deploying.", show_default=True),
) -> None:
    """
    Simulate a GitHub webhook push — shows what InfraGPT would do.

    Does NOT make any real changes. Good for validating config before going live.
    """
    from agent.integrations.mapping_loader import get_loader
    from agent.integrations.kubectl import get_deployment_info

    console.print()
    console.print(Rule(
        f"[bold cyan]Webhook Simulation[/bold cyan]  "
        f"[cyan]{_escape(repo)}[/cyan] [dim]→ {_escape(branch)}[/dim]"
    ))
    console.print()

    loader  = get_loader()
    mapping = loader.find_mapping(repo, branch)

    if mapping is None:
        _print_error(f"No mapping found for {repo}:{branch}")
        console.print(
            "  [dim]Add one with:[/dim]  "
            f"[bold]agent deploy add-mapping --repo {_escape(repo)} --deployment <name> --namespace <ns>[/bold]"
        )
        raise typer.Exit(1)

    # Simulate the payload that would arrive
    simulated_image = f"{mapping.image_prefix}:{tag}" if mapping.image_prefix else f"{mapping.deployment}:{tag}"

    console.print(Panel(
        f"  [dim]Trigger      :[/dim] push to [bold]{_escape(repo)}@{_escape(branch)}[/bold]\n"
        f"  [dim]Mapped to    :[/dim] [green]{_escape(mapping.deployment)}[/green] / [cyan]{_escape(mapping.namespace)}[/cyan]\n"
        f"  [dim]Would deploy :[/dim] [bold]{_escape(simulated_image)}[/bold]\n"
        f"  [dim]Auto low-risk:[/dim] {'[green]Yes[/green]' if mapping.auto_approve_low_risk else '[dim]No — Slack approval required[/dim]'}",
        title="[bold]Simulated Webhook Push[/bold]",
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()

    # Check current deployment state
    with console.status("[bold cyan]Checking current deployment state...", spinner="dots"):
        info = get_deployment_info(mapping.deployment, mapping.namespace)

    if info:
        health_color = "green" if info.healthy else "red"
        console.print(Panel(
            f"  [dim]Current image:[/dim] [bold]{_escape(info.current_image)}[/bold]\n"
            f"  [dim]Replicas     :[/dim] [{health_color}]{info.replicas_ready}/{info.replicas_desired} ready[/{health_color}]\n"
            f"  [dim]Strategy     :[/dim] {_escape(info.strategy)}",
            title="[bold]Current Deployment State[/bold]",
            border_style="dim",
            padding=(0, 2),
        ))
        console.print()

        # Quick risk estimate without calling Claude
        is_rolling = info.strategy == "RollingUpdate"
        enough_replicas = info.replicas_desired >= 2
        risk = "low" if (is_rolling and enough_replicas) else ("medium" if is_rolling else "high")
        risk_color = {"low": "green", "medium": "yellow", "high": "bold red"}[risk]

        action_label = (
            "[green]AUTO-DEPLOY[/green] (low risk + auto-approve enabled)"
            if risk == "low" and mapping.auto_approve_low_risk
            else "[yellow]SLACK APPROVAL[/yellow] — message sent to #alerts for human approval"
        )

        console.print(Panel(
            f"  [dim]Estimated risk :[/dim] [{risk_color}]{risk.upper()}[/{risk_color}]\n"
            f"  [dim]Action InfraGPT would take:[/dim]\n\n"
            f"    {action_label}\n\n"
            f"  [dim]Slack message would include:[/dim]\n"
            f"    • Repo: {_escape(repo)} ({_escape(branch)})\n"
            f"    • New image: {_escape(simulated_image)}\n"
            f"    • Current: {_escape(info.current_image)}\n"
            f"    • Risk: {risk.upper()}\n"
            f"    • Buttons: [Approve Deploy] [Rollback if bad]",
            title="[bold]What InfraGPT Would Do[/bold]",
            border_style=risk_color.replace("bold ", ""),
            padding=(0, 2),
        ))
    else:
        console.print(
            f"  [yellow]⚠ Deployment '{_escape(mapping.deployment)}' not found in "
            f"namespace '{_escape(mapping.namespace)}' — check mapping config.[/yellow]"
        )

    console.print()
    console.print("[dim]This was a simulation. No changes were made.[/dim]")
    console.print()


# ---------------------------------------------------------------------------
# deploy webhook-start / pending / approve / reject / reports
# ---------------------------------------------------------------------------

@deploy_app.command("webhook-start")
def deploy_webhook_start(
    port: int = typer.Option(8080,      "--port",  help="Port to listen on.", show_default=True),
    host: str = typer.Option("0.0.0.0", "--host",  help="Host to bind.",      show_default=True),
) -> None:
    """Start the GitHub webhook listener server."""
    try:
        import uvicorn
    except ImportError:
        _print_error("uvicorn not installed. Run: pip install uvicorn")
        raise typer.Exit(1)

    from agent.integrations.mapping_loader import get_loader
    cfg = get_loader().load()

    console.print()
    lines = [
        f"  [bold cyan]ATLASOS WEBHOOK LISTENER[/bold cyan]",
        f"  Listening on [green]{host}:{port}[/green]",
        f"  Mappings loaded: [bold]{len(cfg.mappings)}[/bold]",
    ]
    if cfg.mappings:
        lines.append("")
        for m in cfg.mappings:
            lines.append(
                f"  [cyan]{_escape(m.repo)}:{_escape(m.branch)}[/cyan]\n"
                f"    → [green]{_escape(m.deployment)}/{_escape(m.namespace)}[/green]"
            )
    lines.append("\n  [dim]Waiting for GitHub webhooks... (Ctrl+C to stop)[/dim]")

    console.print(Panel("\n".join(lines), border_style="cyan", padding=(0, 2)))
    console.print()

    uvicorn.run(
        "agent.integrations.webhook:app",
        host=host,
        port=port,
        log_level="warning",
    )


@deploy_app.command("pending")
def deploy_pending() -> None:
    """List all deploys waiting for approval."""
    from agent.integrations.deploy_db import list_pending

    pending = list_pending(status="pending")
    console.print()

    if not pending:
        console.print(Panel(
            "[dim]No pending deploys.[/dim]\n\n"
            "Deploys appear here when a mapped GitHub push is received.\n"
            "Start the listener: [bold]agent deploy webhook-start[/bold]",
            title="[bold]Pending Deploys[/bold]", border_style="dim",
        ))
        console.print()
        return

    tbl = Table(
        title=f"[bold]Pending Deploys[/bold]  [dim]({len(pending)} waiting)[/dim]",
        show_lines=True, header_style="bold cyan", border_style="dim",
    )
    tbl.add_column("ID",         width=10)
    tbl.add_column("Deployment", width=18)
    tbl.add_column("Namespace",  width=14)
    tbl.add_column("Image",      max_width=28, no_wrap=True)
    tbl.add_column("Risk",       justify="center", width=10)
    tbl.add_column("Author",     width=16)
    tbl.add_column("Age",        width=12)

    _RISK_COLOR = {"low": "green", "medium": "yellow", "high": "bold red", "critical": "bold red"}

    for p in pending:
        rc = _RISK_COLOR.get(p.risk_label, "white")
        console.print()
        tbl.add_row(
            f"[bold]{_escape(p.id)}[/bold]",
            _escape(p.deployment),
            _escape(p.namespace),
            _escape(p.new_image.split("/")[-1] if "/" in p.new_image else p.new_image),
            f"[{rc}]{p.risk_label.upper()}[/{rc}]",
            _escape(p.author[:14]),
            _time_ago(p.created_at) if p.created_at else "[dim]?[/dim]",
        )

    console.print(tbl)
    console.print()
    console.print(
        "  [dim]Approve:[/dim]  [bold]agent deploy approve <ID>[/bold]\n"
        "  [dim]Reject: [/dim]  [bold]agent deploy reject <ID>[/bold]"
    )
    console.print()


@deploy_app.command("approve")
def deploy_approve(
    deploy_id: str = typer.Argument(..., help="Pending deploy ID to approve."),
    watch_seconds: int = typer.Option(120, "--watch-seconds", help="How long to watch after deploy.", show_default=True),
    no_auto_rollback: bool = typer.Option(False, "--no-auto-rollback", help="Disable auto-rollback watcher."),
) -> None:
    """Approve and execute a pending webhook deploy with full health watching."""
    from agent.integrations.deploy_db import get_pending
    from agent.skills.deployment import DeploymentSkill

    console.print()
    p = get_pending(deploy_id)
    if p is None:
        _print_error(f"No pending deploy found with ID: {deploy_id}")
        raise typer.Exit(1)
    if p.status != "pending":
        _print_error(f"Deploy {deploy_id} is already '{p.status}' — cannot approve.")
        raise typer.Exit(1)

    console.print(Rule(f"[bold cyan]Approve Deploy:[/bold cyan] {_escape(p.deployment)} [dim]({p.namespace})[/dim]"))
    console.print()

    _RISK_COLOR = {"low": "green", "medium": "yellow", "high": "bold red", "critical": "bold red"}
    rc = _RISK_COLOR.get(p.risk_label, "white")

    console.print(Panel(
        f"  [dim]Repo      :[/dim] [cyan]{_escape(p.repo)}:{_escape(p.branch)}[/cyan]\n"
        f"  [dim]Commit    :[/dim] {_escape(p.commit_sha[:8])}  by {_escape(p.author)}\n"
        f"  [dim]Message   :[/dim] {_escape(p.commit_message[:80])}\n"
        f"  [dim]Image     :[/dim] [bold]{_escape(p.new_image)}[/bold]\n"
        f"  [dim]Risk      :[/dim] [{rc}]{p.risk_label.upper()} (score {p.risk_score}/100)[/{rc}]\n"
        f"  [dim]Requested :[/dim] {_time_ago(p.created_at)} ago",
        title="[bold]PENDING DEPLOY[/bold]",
        border_style=rc.replace("bold ", ""),
        padding=(0, 2),
    ))
    console.print()

    # Show health gate
    skill = DeploymentSkill()
    console.print("[bold]Running pre-deploy health gate...[/bold]")
    with console.status("[cyan]Checking cluster health...", spinner="dots"):
        gate = skill.pre_deploy_health_check(p.deployment, p.namespace, p.new_image)

    _print_health_gate(gate)

    if gate.blocked:
        _print_error("Deploy blocked by health gate — fix issues before approving.")
        raise typer.Exit(1)

    # Production / high-risk terminal confirmation
    if p.risk_label in ("high", "critical") or _is_production(p.namespace):
        console.print(Panel(
            f"[bold red]{'PRODUCTION: ' + p.namespace + '  ' if _is_production(p.namespace) else ''}"
            f"RISK: {p.risk_label.upper()}[/bold red]\n"
            f"Type the deployment name to confirm:",
            border_style="red", padding=(0, 2),
        ))
        typed = typer.prompt("  Confirm deployment name")
        if typed.strip() != p.deployment:
            console.print("[bold red]Name mismatch — approval cancelled.[/bold red]")
            raise typer.Exit(0)
    else:
        if not typer.confirm("  Proceed with deploy?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    # Delegate full execution to shared skill method
    console.print()
    console.print("[bold cyan]Deploying...[/bold cyan]")

    def _on_status(msg: str) -> None:
        console.print(f"  [dim]{_escape(msg)}[/dim]")

    ws = 0 if no_auto_rollback else watch_seconds
    report = skill.execute_approve(deploy_id, watch_seconds=ws, on_status=_on_status)

    if report is None:
        _print_error("Deploy did not complete (blocked or not found).")
        raise typer.Exit(1)

    _print_deploy_report(report)
    console.print()


@deploy_app.command("reject")
def deploy_reject(
    deploy_id: str = typer.Argument(..., help="Pending deploy ID to reject."),
    reason:    str = typer.Option("", "--reason", help="Optional rejection reason."),
) -> None:
    """Reject a pending deploy and notify Slack."""
    from agent.integrations.deploy_db import get_pending
    from agent.skills.deployment import DeploymentSkill

    console.print()
    p = get_pending(deploy_id)
    if p is None:
        _print_error(f"No pending deploy found with ID: {deploy_id}")
        raise typer.Exit(1)
    if p.status != "pending":
        _print_error(f"Deploy {deploy_id} is already '{p.status}'.")
        raise typer.Exit(1)

    console.print(Panel(
        f"  [dim]Deployment:[/dim] [bold]{_escape(p.deployment)}[/bold] / {_escape(p.namespace)}\n"
        f"  [dim]Image:    [/dim] {_escape(p.new_image)}\n"
        f"  [dim]Reason:   [/dim] {_escape(reason) if reason else '[dim]— none given[/dim]'}",
        title="[bold red]Reject Deploy[/bold red]", border_style="red", padding=(0, 2),
    ))
    console.print()

    if not typer.confirm("  Confirm rejection?", default=False):
        console.print("[dim]Cancelled.[/dim]")
        raise typer.Exit(0)

    skill = DeploymentSkill()
    skill.execute_reject(deploy_id, reason=reason)

    console.print(f"[bold green]✓ Deploy {deploy_id} rejected.[/bold green]")
    console.print()


@deploy_app.command("reports")
def deploy_reports(
    last: int = typer.Option(10, "--last", help="Number of recent reports to show.", show_default=True),
) -> None:
    """Show recent deploy history from the report log."""
    from agent.integrations.deploy_db import list_reports

    reports = list_reports(limit=last)
    console.print()

    if not reports:
        console.print("[dim]No deploy reports yet.[/dim]\n")
        return

    tbl = Table(
        title=f"[bold]Deploy Reports[/bold]  [dim](last {len(reports)})[/dim]",
        show_lines=True, header_style="bold cyan", border_style="dim",
    )
    tbl.add_column("Time",       width=14)
    tbl.add_column("Deployment", width=18)
    tbl.add_column("Namespace",  width=14)
    tbl.add_column("Status",     justify="center", width=12)
    tbl.add_column("Image",      max_width=26, no_wrap=True)
    tbl.add_column("Risk",       justify="center", width=10)
    tbl.add_column("Duration",   justify="right",  width=9)

    _STATUS_STYLE = {
        "SUCCESS":     "[bold green]✓ OK[/bold green]",
        "ROLLED_BACK": "[bold yellow]↩ ROLLED[/bold yellow]",
        "FAILED":      "[bold red]✗ FAIL[/bold red]",
    }
    _RISK_COLOR = {"low": "green", "medium": "yellow", "high": "bold red", "critical": "bold red"}

    for r in reports:
        rc = _RISK_COLOR.get(r.risk_level, "white")
        img = r.new_image.split("/")[-1] if "/" in r.new_image else r.new_image
        tbl.add_row(
            _time_ago(r.timestamp) if r.timestamp else "—",
            _escape(r.deployment),
            _escape(r.namespace),
            _STATUS_STYLE.get(r.status, r.status),
            _escape(img[-26:]),
            f"[{rc}]{r.risk_level.upper()}[/{rc}]",
            f"{r.duration_seconds}s",
        )

    console.print(tbl)
    console.print()
    console.print(
        f"  [dim]Reports saved to:[/dim]  data/reports/  "
        f"[dim]({len(reports)} shown)[/dim]"
    )
    console.print()


# ---------------------------------------------------------------------------
# Shared deploy helper — health gate display + watcher
# ---------------------------------------------------------------------------

def _print_health_gate(gate) -> None:
    """Render health gate result to console."""
    _SEV_ICON  = {"pass": "[green]✓[/green]", "warn": "[yellow]⚠[/yellow]", "block": "[bold red]✗[/bold red]"}
    _SEV_COLOR = {"pass": "green", "warn": "yellow", "block": "bold red"}

    console.print(Rule("[bold]PRE-DEPLOY HEALTH GATE[/bold]"))
    for chk in gate.checks:
        icon  = _SEV_ICON.get(chk.severity, "•")
        color = _SEV_COLOR.get(chk.severity, "white")
        console.print(
            f"  {icon}  [{color}]{_escape(chk.name):24s}[/{color}]  {_escape(chk.message)}"
        )

    console.print(Rule(style="dim"))
    if gate.blocked:
        console.print(
            f"  [bold red]BLOCKED[/bold red] — {len(gate.blockers)} blocker(s)"
        )
        for b in gate.blockers:
            console.print(f"    [red]• {_escape(b)}[/red]")
    elif gate.warnings:
        console.print(
            f"  [yellow]PASS WITH {len(gate.warnings)} WARNING(S)[/yellow]"
        )
        for w in gate.warnings:
            console.print(f"    [yellow]• {_escape(w)}[/yellow]")
    else:
        console.print("  [bold green]ALL CLEAR[/bold green]")
    console.print()


def _print_risk_score(risk) -> None:
    """Render risk score panel to console."""
    _LABEL_COLOR = {"low": "green", "medium": "yellow", "high": "dark_orange", "critical": "bold red"}
    lc = _LABEL_COLOR.get(risk.label, "white")

    factor_lines = "\n".join(
        f"  [dim]  {f:<44}[/dim] [{'red' if p > 0 else 'green'}]+{p}[/{'red' if p > 0 else 'green'}]"
        for f, p in risk.factors
    )
    console.print(Panel(
        f"  [dim]Score :[/dim]  [bold]{risk.total}/100[/bold]   "
        f"[dim]Level:[/dim]  [{lc}]{risk.label.upper()}[/{lc}]\n\n"
        f"{factor_lines}\n\n"
        f"  [dim]Recommendation:[/dim] {_escape(risk.recommendation)}",
        title="[bold]RISK ASSESSMENT[/bold]",
        border_style=lc.replace("bold ", ""),
        padding=(0, 2),
    ))
    console.print()


def _run_watcher(
    skill, deployment: str, namespace: str,
    old_image: str, new_image: str,
    watch_seconds: int, risk_score_total: int, risk_label: str,
    gate, duration_so_far: int, pending_id: str | None = None,
) -> bool:
    """Live watcher with Rich progress. Returns True if success."""
    from rich.live import Live
    from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
    from agent.integrations.deploy_db import update_pending_status
    from datetime import datetime, timezone
    from agent.core.models import RiskScore

    console.print()
    console.print(f"[bold]Watching rollout for {watch_seconds} seconds...[/bold]\n")

    log_lines: list[str] = []
    result_holder: list = []

    progress = Progress(
        TextColumn("[bold cyan]"),
        BarColumn(bar_width=40),
        TextColumn("{task.completed}s / {task.total}s"),
        TimeElapsedColumn(),
    )
    task = progress.add_task("", total=watch_seconds)

    def _on_sample(sample) -> None:
        elapsed = sample.elapsed_s
        progress.update(task, completed=min(elapsed, watch_seconds))
        icon = (
            "[bold red]✗[/bold red]" if sample.event == "rollback"
            else "[yellow]⚠[/yellow]" if sample.event == "warn"
            else "[green]✓[/green]"
        )
        log_lines.append(
            f"  {elapsed:3d}s  {icon}  {_escape(sample.message or f'Pods {sample.ready_count}/{sample.total_count} ready')}"
        )

    start = time.time()
    import threading

    def _watch_thread() -> None:
        r = skill.watch_deployment_health(
            deployment, namespace, old_image, new_image,
            watch_seconds=watch_seconds, on_sample=_on_sample,
        )
        result_holder.append(r)

    t = threading.Thread(target=_watch_thread, daemon=True)
    t.start()

    with Live(progress, refresh_per_second=4, console=console):
        while t.is_alive():
            time.sleep(0.25)

    t.join()
    progress.update(task, completed=watch_seconds)

    for line in log_lines:
        console.print(line)
    console.print()

    watch_result = result_holder[0] if result_holder else None
    if watch_result is None:
        console.print("[yellow]Watcher returned no result.[/yellow]")
        return False

    total_dur = int(time.time() - start) + duration_so_far

    if watch_result.rollback_triggered:
        console.print(Panel(
            f"[bold red]AUTO-ROLLBACK TRIGGERED[/bold red]\n"
            f"Reason: {_escape(watch_result.rollback_reason or '?')}\n"
            f"Rolling back to {_escape(old_image)}...\n"
            f"[dim]Sending failure report to Slack...[/dim]",
            border_style="red", padding=(0, 2),
        ))
        status_label = "ROLLED_BACK"
    elif watch_result.success:
        console.print(
            f"[bold green]✓ DEPLOYMENT HEALTHY[/bold green]  "
            f"[dim]{watch_result.final_ready_count} pods ready, "
            f"{watch_result.errors_detected} errors detected[/dim]"
        )
        status_label = "SUCCESS"
    else:
        console.print(
            f"[bold yellow]⚠ DEPLOY COMPLETE WITH WARNINGS[/bold yellow]  "
            f"{watch_result.final_ready_count} pods ready"
        )
        status_label = "FAILED"

    console.print()

    if pending_id:
        try:
            update_pending_status(
                pending_id, "deployed" if status_label == "SUCCESS" else "failed",
                deployed_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception:
            pass

    # Generate report
    console.print("[dim]Generating deploy report...[/dim]")
    try:
        risk_obj = RiskScore(total=risk_score_total, label=risk_label, factors=[], recommendation="")
        with console.status("[dim]Claude summarising...[/dim]", spinner="dots"):
            report = skill.generate_deploy_report(
                deployment, namespace, old_image, new_image,
                risk_obj, gate, watch_result, total_dur,
            )

        _print_deploy_report(report)

        try:
            from agent.integrations.slack import send_alert_generic, is_configured
            if is_configured():
                icon = "✓" if report.status == "SUCCESS" else ("↩" if report.status == "ROLLED_BACK" else "✗")
                send_alert_generic(
                    title=f"DEPLOY REPORT: {deployment}",
                    message=(
                        f"{icon} *{report.status}*  "
                        f"`{old_image.split(':')[-1]} → {new_image.split(':')[-1]}`  "
                        f"{total_dur}s\n"
                        f"Pods: {watch_result.final_ready_count} healthy   "
                        f"Errors: {watch_result.errors_detected}\n\n"
                        f"_{report.claude_summary}_"
                    ),
                    severity="info" if report.status == "SUCCESS" else "critical",
                    fields={
                        "Risk":     risk_label.upper(),
                        "Rollback": "Yes" if watch_result.rollback_triggered else "Available",
                    },
                )
        except Exception:
            pass
    except Exception as exc:
        console.print(f"[yellow]Report generation failed: {_escape(str(exc)[:80])}[/yellow]")

    return watch_result.success


def _print_deploy_report(report) -> None:
    _ST = {"SUCCESS": "[bold green]✓ SUCCESS[/bold green]",
           "ROLLED_BACK": "[bold yellow]↩ ROLLED BACK[/bold yellow]",
           "FAILED": "[bold red]✗ FAILED[/bold red]"}
    console.print(Panel(
        f"  [dim]Status   :[/dim] {_ST.get(report.status, report.status)}\n"
        f"  [dim]Image    :[/dim] [bold]{_escape(report.new_image.split('/')[-1])}[/bold]"
        f"  ← {_escape(report.old_image.split('/')[-1])}\n"
        f"  [dim]Duration :[/dim] {report.duration_seconds}s\n"
        f"  [dim]Pods     :[/dim] {report.pods_healthy} healthy\n"
        f"  [dim]Risk     :[/dim] {report.risk_level.upper()}\n"
        f"  [dim]Errors   :[/dim] {report.errors_detected} detected\n"
        f"  [dim]Rollback :[/dim] {'Yes — triggered' if report.rollback_triggered else 'Available'}\n\n"
        f"  [bold]AI Summary:[/bold]\n  {_escape(report.claude_summary)}",
        title=f"[bold]DEPLOY REPORT[/bold]  [cyan]{_escape(report.deployment)}[/cyan]",
        border_style="green" if report.status == "SUCCESS" else "red",
        padding=(0, 2),
    ))
    console.print()


# ---------------------------------------------------------------------------
# Daemon commands — 24/7 autonomous healing
# ---------------------------------------------------------------------------

@daemon_app.command("start")
def daemon_start(
    detach: bool = typer.Option(True, "--detach/--no-detach",
                                help="Run in background (survives terminal close)."),
) -> None:
    """Start the 24/7 autonomous healing daemon."""
    import os
    import signal
    import subprocess
    import sys
    from pathlib import Path

    pid_file = Path("data/daemon.pid")

    # Check if already running
    if pid_file.exists():
        try:
            import psutil
            pid = int(pid_file.read_text().strip())
            if psutil.pid_exists(pid):
                console.print(f"[yellow]Daemon already running (PID {pid}).[/yellow]")
                console.print("  Stop it first: [bold]agent daemon stop[/bold]")
                raise typer.Exit(0)
        except (ImportError, ValueError, OSError):
            pid_file.unlink(missing_ok=True)

    Path("data").mkdir(parents=True, exist_ok=True)

    if detach:
        log_path = open("data/daemon.log", "a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent.core.daemon"],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            stdout=log_path,
            stderr=log_path,
            close_fds=True,
        )
        pid_file.write_text(str(proc.pid))

        import time
        time.sleep(2)

        try:
            import psutil
            alive = psutil.pid_exists(proc.pid)
        except ImportError:
            alive = True  # assume alive if psutil not installed

        if not alive:
            console.print("[red]Daemon failed to start. Check data/daemon.log[/red]")
            raise typer.Exit(1)

        console.print()
        console.print(Panel(
            f"  [bold green]✅ DAEMON STARTED[/bold green]\n\n"
            f"  [dim]PID:[/dim]        [cyan]{proc.pid}[/cyan]\n"
            f"  [dim]Watching:[/dim]   pods • resources • deploys • costs\n"
            f"  [dim]Cost fixes:[/dim] nightly at 2:00 AM\n"
            f"  [dim]Logs:[/dim]       data/daemon.log\n"
            f"  [dim]Stop:[/dim]       [bold]agent daemon stop[/bold]",
            title="[bold cyan]AtlasOS Daemon[/bold cyan]",
            border_style="green", padding=(0, 2),
        ))
        console.print()
    else:
        # Foreground mode — run directly
        from agent.core.daemon import HealingDaemon
        console.print("[dim]Running daemon in foreground (Ctrl+C to stop)...[/dim]")
        HealingDaemon().start()


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the autonomous healing daemon."""
    from pathlib import Path
    pid_file = Path("data/daemon.pid")
    if not pid_file.exists():
        console.print("[yellow]No daemon PID file found — daemon may not be running.[/yellow]")
        raise typer.Exit(0)
    try:
        pid = int(pid_file.read_text().strip())
        try:
            import psutil
            p = psutil.Process(pid)
            p.terminate()
            console.print(f"[green]Daemon stopped (PID {pid}).[/green]")
        except psutil.NoSuchProcess:
            console.print(f"[yellow]Daemon was not running (PID {pid} already gone).[/yellow]")
        except ImportError:
            import os, signal as _sig
            os.kill(pid, _sig.SIGTERM)
            console.print(f"[green]Daemon stopped (PID {pid}).[/green]")
    except (ValueError, ProcessLookupError, OSError) as exc:
        console.print(f"[yellow]Could not stop daemon: {exc}[/yellow]")
    finally:
        pid_file.unlink(missing_ok=True)


@daemon_app.command("status")
def daemon_status() -> None:
    """Show daemon status and recent autonomous actions."""
    import os
    from pathlib import Path
    from datetime import datetime, timezone

    pid_file = Path("data/daemon.pid")
    pid = None
    uptime_str = "—"
    status_str = "[red]Not running[/red]"

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            try:
                import psutil
                p = psutil.Process(pid)
                if p.is_running():
                    create_ts = p.create_time()
                    uptime_s  = int(datetime.now().timestamp() - create_ts)
                    h, rem    = divmod(uptime_s, 3600)
                    m         = rem // 60
                    uptime_str  = f"{h}h {m}m"
                    status_str  = f"[green]Running[/green]  [dim](PID {pid}, uptime {uptime_str})[/dim]"
                else:
                    status_str = "[red]Stopped (stale PID)[/red]"
            except ImportError:
                status_str = f"[green]Running[/green]  [dim](PID {pid})[/dim]"
        except (ValueError, OSError):
            status_str = "[red]Not running[/red]"

    from agent.integrations import daemon_db
    actions_today = daemon_db.get_action_count_today()
    total_savings = daemon_db.get_total_savings()
    recent        = daemon_db.list_actions(limit=10, since_hours=24)

    console.print()
    console.print(Panel(
        f"  [dim]Status:[/dim]   {status_str}\n"
        f"  [dim]Watching:[/dim] pods • resources • deploys • costs\n"
        f"  [dim]Actions today:[/dim] [bold]{actions_today}[/bold]  "
        f"  [dim]Total savings:[/dim] [green]${total_savings:,.2f}[/green]",
        title="[bold cyan]DAEMON STATUS[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))
    console.print()

    if not recent:
        console.print("[dim]  No actions in the last 24 hours.[/dim]")
    else:
        tbl = Table(
            title="Recent Actions (last 24h)",
            show_lines=False, header_style="bold cyan", border_style="dim",
        )
        tbl.add_column("Time",     width=10)
        tbl.add_column("Category", width=12)
        tbl.add_column("Action",   min_width=38)
        tbl.add_column("Result",   width=8, justify="center")

        for a in recent:
            ts = a.get("timestamp", "")[:19].replace("T", " ")[11:]
            cat = a.get("category", "")
            act = a.get("action", "")[:55]
            ok  = "✅ Fixed" if a.get("success") else "❌ Failed"
            tbl.add_row(ts, cat, act, ok)

        console.print(tbl)
    console.print()


@daemon_app.command("logs")
def daemon_logs(
    limit: int            = typer.Option(50,   "--limit", "-n", help="Max rows to show."),
    category: str | None  = typer.Option(None, "--category", "-c",
                                         help="Filter: pod_heal / scale / rollback / cost"),
    since: int            = typer.Option(24,   "--since", help="Hours back to show."),
) -> None:
    """Show full daemon action history."""
    from agent.integrations import daemon_db

    rows = daemon_db.list_actions(limit=limit, category=category, since_hours=since)
    console.print()

    if not rows:
        console.print(f"[dim]No daemon actions in the last {since}h.[/dim]")
        console.print()
        return

    tbl = Table(
        title=f"Daemon Action Log  [dim](last {since}h, {len(rows)} rows)[/dim]",
        show_lines=True, header_style="bold cyan", border_style="dim",
    )
    tbl.add_column("Time",      width=20)
    tbl.add_column("Category",  width=12)
    tbl.add_column("Action",    min_width=40)
    tbl.add_column("Resource",  width=20)
    tbl.add_column("NS",        width=12)
    tbl.add_column("Result",    width=8, justify="center")
    tbl.add_column("Savings",   width=10, justify="right")

    for a in rows:
        ts      = a.get("timestamp", "")[:19].replace("T", " ")
        ok      = "✅" if a.get("success") else "❌"
        savings = f"${a['savings']:,.2f}" if a.get("savings") else "—"
        tbl.add_row(
            ts,
            a.get("category", ""),
            a.get("action", "")[:55],
            a.get("resource", "")[:20],
            a.get("namespace", "")[:12],
            ok,
            savings,
        )

    console.print(tbl)
    total = sum(r.get("savings", 0) or 0 for r in rows)
    if total:
        console.print(f"  [green]Total savings shown: ${total:,.2f}[/green]")
    console.print()


# ---------------------------------------------------------------------------
# Incident tracking
# ---------------------------------------------------------------------------

_INC_SEV_BADGE = {
    "critical": "🔴 CRIT",
    "warning":  "🟡 WARN",
    "info":     "🔵 INFO",
}


def _inc_duration_str(inc: dict) -> str:
    """Human duration: resolved → stored duration; open → elapsed since opened."""
    from datetime import datetime, timezone
    secs = int(inc.get("duration_sec") or 0)
    if inc.get("status") != "resolved":
        opened = inc.get("opened_at")
        try:
            ts = datetime.fromisoformat(opened) if opened else None
            if ts is not None:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                secs = int(max(0, (datetime.now(timezone.utc) - ts).total_seconds()))
        except Exception:
            secs = 0
    mins = secs // 60
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    return f"{h}h {m}m"


def _inc_status_badge(status: str) -> str:
    if status == "open":
        return "OPEN"
    if status == "resolved":
        return "✅ OK"
    if status == "suppressed":
        return "SUPP"
    return status.upper()


@incident_app.command("list")
def incident_list(
    status: str | None = typer.Option(None, "--status", "-s", help="open/resolved/all"),
    limit: int = typer.Option(20, "--limit", "-n"),
    since: int = typer.Option(168, "--since", help="Hours back (default 7 days)."),
) -> None:
    """List recent incidents, newest first."""
    from agent.integrations import incident_db

    filter_status = None if (status is None or status.lower() == "all") else status.lower()
    rows  = incident_db.list_incidents(status=filter_status, limit=limit, since_hours=since)
    stats = incident_db.get_stats(days=max(1, since // 24 or 1))

    console.print()
    if not rows:
        console.print(f"[dim]No incidents in the last {since}h.[/dim]")
        console.print()
        return

    tbl = Table(show_lines=False, header_style="bold cyan", border_style="dim")
    tbl.add_column("ID",       width=10)
    tbl.add_column("Title",    min_width=28)
    tbl.add_column("Severity", width=9)
    tbl.add_column("Service",  width=12)
    tbl.add_column("Status",   width=7)
    tbl.add_column("Duration", width=10, justify="right")

    for r in rows:
        tbl.add_row(
            r["id"][:8],
            r.get("title", "")[:40],
            _INC_SEV_BADGE.get(r.get("severity", ""), r.get("severity", "")),
            (r.get("service", "") or "—")[:12],
            _inc_status_badge(r.get("status", "")),
            _inc_duration_str(r),
        )

    console.print(tbl)
    mttr_min = round((stats.get("mttr_hours", 0) or 0) * 60)
    console.print(
        f"[dim]Stats: {stats['total']} total | {stats['open']} open | "
        f"{stats['resolved']} resolved | MTTR: {mttr_min} min[/dim]"
    )
    console.print()


@incident_app.command("show")
def incident_show(incident_id: str = typer.Argument(...)) -> None:
    """Show a single incident with its full timeline."""
    from agent.integrations import incident_db

    inc = incident_db.get_incident(incident_id)
    if inc is None and len(incident_id) <= 8:
        # Allow short-id lookups against recent incidents.
        for cand in incident_db.list_incidents(limit=200):
            if cand["id"].startswith(incident_id):
                inc = incident_db.get_incident(cand["id"])
                break

    console.print()
    if inc is None:
        _print_error(f"Incident not found: {incident_id}")
        raise typer.Exit(1)

    sev    = inc.get("severity", "")
    opened = (inc.get("opened_at", "") or "")[:19].replace("T", " ")[11:]
    body = (
        f"[bold]{inc.get('title','')}[/bold]\n"
        f"Severity: [bold]{sev.upper()}[/bold]   Status: [bold]{inc.get('status','').upper()}[/bold]\n"
        f"Service: {inc.get('service','') or '—'}   Namespace: {inc.get('namespace','') or '—'}\n"
        f"Opened: {opened}   Duration: {_inc_duration_str(inc)}\n"
        f"Fix attempts: {inc.get('fix_attempts',0)}   Cause: {inc.get('cause','') or '—'}"
    )
    console.print(Panel(
        body,
        title=f"[bold cyan]INCIDENT {inc['id'][:8]}[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))
    console.print()

    events = inc.get("events", [])
    console.print("[bold]TIMELINE[/bold]")
    if not events:
        console.print("  [dim]No events recorded.[/dim]")
    else:
        for ev in events:
            ts = (ev.get("timestamp", "") or "")[:19].replace("T", " ")[11:]
            console.print(
                f"  [dim]{ts}[/dim]  "
                f"{ev.get('event_type',''):<15} {ev.get('detail','')}"
            )
    console.print()


@incident_app.command("resolve")
def incident_resolve(
    incident_id: str = typer.Argument(...),
    note: str = typer.Option("", "--note", "-n"),
) -> None:
    """Manually resolve an incident."""
    from agent.integrations import incident_db

    inc = incident_db.get_incident(incident_id)
    if inc is None and len(incident_id) <= 8:
        for cand in incident_db.list_incidents(limit=200):
            if cand["id"].startswith(incident_id):
                inc = incident_db.get_incident(cand["id"])
                break

    console.print()
    if inc is None:
        _print_error(f"Incident not found: {incident_id}")
        raise typer.Exit(1)

    if inc.get("status") == "resolved":
        console.print(f"[yellow]Incident {inc['id'][:8]} is already resolved.[/yellow]")
        return

    if not typer.confirm(f"Resolve incident '{inc.get('title','')}'?"):
        console.print("[dim]Cancelled.[/dim]")
        return

    cause = note or "manually resolved"
    incident_db.resolve_incident(inc["id"], auto_fixed=False, note=cause)
    incident_db.add_event(inc["id"], "resolved", detail=cause)
    console.print(f"[green]✅ Incident {inc['id'][:8]} resolved.[/green]")
    console.print()


@incident_app.command("stats")
def incident_stats(
    days: int = typer.Option(30, "--days", "-d"),
) -> None:
    """Show incident statistics over the last N days."""
    from agent.integrations import incident_db

    s = incident_db.get_stats(days=days)
    total       = s["total"]
    auto        = s["auto_resolved"]
    needs_human = max(0, s["resolved"] - auto) + s["open"]
    auto_pct    = round(100 * auto / total) if total else 0
    human_pct   = round(100 * needs_human / total) if total else 0
    mttr_min    = round((s.get("mttr_hours", 0) or 0) * 60, 1)
    longest_min = round((s.get("longest_sec", 0) or 0) / 60)

    body = (
        f"  Total incidents:   {total:>4}\n"
        f"  Auto-resolved:     {auto:>4}  ({auto_pct}%)\n"
        f"  Needs human:       {needs_human:>4}  ({human_pct}%)\n"
        f"  Critical:          {s['critical_count']:>4}\n"
        f"  MTTR:              {mttr_min:>6} min\n"
        f"  Longest outage:    {longest_min:>4} min"
    )
    console.print()
    console.print(Panel(
        body,
        title=f"[bold cyan]INCIDENT STATS — Last {days} days[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))
    console.print()


# ---------------------------------------------------------------------------
# Eval: daemon
# ---------------------------------------------------------------------------

@eval_app.command("daemon")
def eval_daemon() -> None:
    """Run Autonomous Healing Daemon eval suite."""
    try:
        from evals.daemon.runner import main as run_evals  # type: ignore[import]
        console.print()
        console.print(Rule("[bold]Autonomous Healing Daemon Eval Suite[/bold]"))
        console.print()
        with console.status("[bold green]Running 8 eval cases...", spinner="dots"):
            passed = run_evals()
        console.print()
        if not passed:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@eval_app.command("incident")
def eval_incident() -> None:
    """Run Incident Auto-Creation eval suite."""
    try:
        from evals.incident.runner import main as run_evals  # type: ignore[import]
        console.print()
        console.print(Rule("[bold]Incident Auto-Creation Eval Suite[/bold]"))
        console.print()
        with console.status("[bold green]Running 6 eval cases...", spinner="dots"):
            passed = run_evals()
        console.print()
        if not passed:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Database health — RDS / Aurora
# ---------------------------------------------------------------------------

_DB_STATUS_BADGE = {
    "healthy":  "✅ OK",
    "warning":  "🟡 WARN",
    "critical": "🔴 CRIT",
}
_DB_ISSUE_EMOJI = {"critical": "🔴", "warning": "🟡", "info": "🔵"}


@db_app.command("scan")
def db_scan(
    region: str = typer.Option("us-east-1", "--region", "-r"),
    fix:    bool = typer.Option(False, "--fix", help="Auto-fix safe issues."),
) -> None:
    """Scan all RDS/Aurora databases for health issues."""
    try:
        from agent.skills.db_health import (
            apply_storage_fix,
            get_storage_forecast,
            scan_all_databases,
        )

        with console.status("[bold green]Scanning RDS/Aurora...", spinner="dots"):
            results = scan_all_databases(region)

        console.print()
        console.print(Panel(
            f"[bold]DATABASE HEALTH SCAN[/bold]\n"
            f"Region: {region}  |  {len(results)} instance(s) found",
            border_style="cyan",
        ))
        console.print()

        if not results:
            console.print(
                "[dim]No RDS instances found (or AWS access not configured).[/dim]"
            )
            console.print()
            return

        tbl = Table(show_lines=False, header_style="bold cyan", border_style="dim")
        tbl.add_column("Instance", no_wrap=True)
        tbl.add_column("Class")
        tbl.add_column("CPU", justify="right")
        tbl.add_column("Connections", justify="right")
        tbl.add_column("Storage", justify="right")
        tbl.add_column("Latency", justify="right")
        tbl.add_column("Status", justify="center")

        for r in results:
            inst = r["instance"]
            m = r["metrics"]
            issue_types = {i["type"] for i in r["issues"]}
            storage_flag = " ⚠" if "LOW_STORAGE" in issue_types else ""
            latency_flag = " ⚠" if "HIGH_READ_LATENCY" in issue_types else ""
            tbl.add_row(
                inst.get("id", ""),
                inst.get("class", ""),
                f"{m.get('cpu_pct', 0):.0f}%",
                f"{m.get('connections_max', 0):.0f}",
                f"{m.get('free_storage_gb', 0):.1f} GB{storage_flag}",
                f"{m.get('read_latency_ms', 0):.1f}ms{latency_flag}",
                _DB_STATUS_BADGE.get(r["status"], r["status"]),
            )

        console.print(tbl)
        console.print()

        # --- issues ----------------------------------------------------------
        all_issues = [
            (r["instance"]["id"], r["metrics"], i)
            for r in results for i in r["issues"]
        ]
        if all_issues:
            console.print("[bold]ISSUES FOUND[/bold]")
            for inst_id, metrics, issue in all_issues:
                emoji = _DB_ISSUE_EMOJI.get(issue["severity"], "•")
                body = f"{emoji} {inst_id} — {issue['type']}\n{issue['detail']}."
                if issue["type"] == "LOW_STORAGE":
                    fc = get_storage_forecast(inst_id, metrics)
                    body += f" At current rate: {fc['days_until_full']} days until full."
                body += f"\nFix: {issue['fix']}"
                color = "red" if issue["severity"] == "critical" else "yellow"
                console.print(Panel(body, border_style=color))
            console.print()
        else:
            console.print("[green]All databases healthy.[/green]")
            console.print()

        # --- optional auto-fix ----------------------------------------------
        if fix:
            fixed = 0
            for r in results:
                inst_id = r["instance"]["id"]
                if any(i["type"] == "LOW_STORAGE" for i in r["issues"]):
                    console.print(f"[cyan]Applying storage fix to {inst_id}...[/cyan]")
                    if apply_storage_fix(inst_id, additional_gb=20, region=region):
                        console.print(f"[green]✓ Increased storage for {inst_id}[/green]")
                        fixed += 1
                    else:
                        console.print(f"[red]✗ Could not fix {inst_id}[/red]")
            console.print()
            console.print(f"[bold]Applied {fixed} fix(es).[/bold]")
            console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@db_app.command("metrics")
def db_metrics(
    instance_id: str = typer.Argument(...),
    region: str = typer.Option("us-east-1", "--region", "-r"),
) -> None:
    """Show detailed metrics for a specific RDS instance."""
    try:
        from agent.integrations.rds import get_rds_metrics

        with console.status("[bold green]Fetching CloudWatch metrics...", spinner="dots"):
            m = get_rds_metrics(instance_id, region)

        body = (
            f"[bold]{instance_id}[/bold]  [dim]({region})[/dim]\n\n"
            f"CPU Utilization:    {m['cpu_pct']:.1f}%\n"
            f"Connections (avg):  {m['connections_avg']:.0f}\n"
            f"Connections (max):  {m['connections_max']:.0f}\n"
            f"Free Storage:       {m['free_storage_gb']:.2f} GB\n"
            f"Read Latency:       {m['read_latency_ms']:.2f} ms\n"
            f"Write Latency:      {m['write_latency_ms']:.2f} ms\n"
            f"Freeable Memory:    {m['freeable_memory_mb']:.1f} MB\n"
            f"Replica Lag:        {m['replica_lag_sec']:.1f} s"
        )
        console.print()
        console.print(Panel(body, title="RDS Metrics (last 1h)", border_style="cyan"))
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@db_app.command("events")
def db_events(
    region: str = typer.Option("us-east-1", "--region", "-r"),
    hours:  int = typer.Option(24, "--hours", "-H"),
) -> None:
    """Show recent RDS events (failovers, restarts, maintenance)."""
    try:
        from agent.integrations.rds import get_rds_events

        with console.status("[bold green]Fetching RDS events...", spinner="dots"):
            events = get_rds_events(region, hours)

        console.print()
        if not events:
            console.print(
                f"[dim]No RDS events in the last {hours}h "
                f"(or AWS access not configured).[/dim]"
            )
            console.print()
            return

        tbl = Table(
            title=f"RDS Events — last {hours}h ({region})",
            show_lines=False, header_style="bold cyan", border_style="dim",
        )
        tbl.add_column("Time", no_wrap=True)
        tbl.add_column("Source", no_wrap=True)
        tbl.add_column("Message")
        for ev in events:
            tbl.add_row(ev["time"], ev["source"], _escape(ev["message"]))
        console.print(tbl)
        console.print()

    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# SLO / Error budget
# ---------------------------------------------------------------------------

_SLO_STATUS_BADGE = {
    "healthy":   "🟢 Healthy",
    "warning":   "🟡 Warning",
    "critical":  "🔴 Critical",
    "exhausted": "⛔ Exhausted",
}


def _slo_resolve_id(slo_id: str):
    """Resolve a full or short (<=8 char) SLO id to its record."""
    from agent.integrations import slo_db
    slo = slo_db.get_slo(slo_id)
    if slo is None and len(slo_id) <= 8:
        for cand in slo_db.list_slos(active_only=False):
            if cand["id"].startswith(slo_id):
                return slo_db.get_slo(cand["id"])
    return slo


@slo_app.command("create")
def slo_create(
    name:       str   = typer.Option(..., "--name",      "-n", help="SLO name, e.g. 'api uptime'"),
    service:    str   = typer.Option(..., "--service",   "-s", help="Deployment name"),
    namespace:  str   = typer.Option("default", "--namespace", "-ns"),
    target:     float = typer.Option(99.9, "--target",   "-t", help="Uptime target %, e.g. 99.9"),
    window:     int   = typer.Option(30,   "--window",   "-w", help="Rolling window in days"),
) -> None:
    """Define a new SLO for a service."""
    from agent.integrations import slo_db

    slo_id  = slo_db.create_slo(name, service, namespace, target_pct=target, window_days=window)
    budget  = window * 24 * 60 * (1 - target / 100.0)

    console.print()
    body = (
        f"[bold green]✅ SLO Created[/bold green]\n\n"
        f"  Name:      {name}\n"
        f"  Service:   {service} / {namespace}\n"
        f"  Target:    {target}%\n"
        f"  Window:    {window} days\n"
        f"  Budget:    {budget:.1f} minutes downtime/window\n"
        f"  ID:        {slo_id[:8]}"
    )
    console.print(Panel(body, border_style="green", padding=(0, 2)))
    console.print()


@slo_app.command("list")
def slo_list() -> None:
    """List all SLOs with current budget status."""
    from agent.skills.slo import get_dashboard

    rows = get_dashboard()
    console.print()
    if not rows:
        console.print("[dim]No SLOs defined. Create one with `agent slo create`.[/dim]")
        console.print()
        return

    tbl = Table(show_lines=False, header_style="bold cyan", border_style="dim")
    tbl.add_column("Service",     width=18, no_wrap=True)
    tbl.add_column("Target",      width=8)
    tbl.add_column("Budget Left", width=11, justify="right")
    tbl.add_column("Used",        width=10, justify="right")
    tbl.add_column("Remaining",   width=13, justify="right")
    tbl.add_column("Status",      width=12)
    tbl.add_column("Deploys",     width=9)

    for s in rows:
        svc      = f"{s['service']}/{s['namespace']}" if s.get("namespace") else s["service"]
        deploys  = "❌ BLOCK" if s["budget_pct_remaining"] < 20 else "✅ OK"
        tbl.add_row(
            svc[:24],
            f"{s['target_pct']}%",
            f"{s['budget_pct_remaining']:.0f}%",
            f"{s['used_downtime_min']:.0f} min",
            f"{s['remaining_min']:.0f} min left",
            _SLO_STATUS_BADGE.get(s["status"], s["status"]),
            deploys,
        )

    console.print(tbl)
    console.print()


@slo_app.command("status")
def slo_status(
    service:   str = typer.Argument(...),
    namespace: str = typer.Option("default", "--namespace", "-n"),
) -> None:
    """Show detailed error budget status for a service."""
    from agent.integrations import slo_db

    slo = slo_db.get_slo_by_service(service, namespace)
    console.print()
    if slo is None:
        _print_error(f"No active SLO for {service}/{namespace}.")
        raise typer.Exit(1)

    s = slo_db.get_budget_status(slo["id"])
    pct      = s["budget_pct_remaining"]
    filled   = int(round(pct / 5.0))          # 20-cell bar
    filled   = max(0, min(20, filled))
    bar      = "█" * filled + "░" * (20 - filled)
    deploys  = "❌ Blocked" if pct < 20 else "✅ Allowed"

    body = (
        f"[bold]SLO: {s['name']}[/bold]\n"
        f"Target: {s['target_pct']}%   Window: {s['window_days']} days\n"
        f"Total budget: {s['allowed_downtime_min']:.1f} min/window\n\n"
        f"{bar}  {pct:.0f}% remaining\n"
        f"Used:      {s['used_downtime_min']:.1f} min    "
        f"Remaining: {s['remaining_min']:.1f} min\n"
        f"Status:    {_SLO_STATUS_BADGE.get(s['status'], s['status'])}   Deploys: {deploys}"
    )
    console.print(Panel(
        body,
        title=f"[bold cyan]ERROR BUDGET — {service}/{namespace}[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))
    console.print()

    console.print("[bold]DOWNTIME EVENTS THIS WINDOW[/bold]")
    burns = s["burns"]
    if not burns:
        console.print("  [dim]No downtime recorded.[/dim]")
        console.print()
        return

    tbl = Table(show_lines=False, header_style="bold cyan", border_style="dim")
    tbl.add_column("Time",     width=20)
    tbl.add_column("Duration", width=10, justify="right")
    tbl.add_column("Cause",    min_width=24)
    for b in burns:
        when = (b.get("started_at", "") or "")[:16].replace("T", " ")
        dur  = round((b.get("duration_sec", 0) or 0) / 60.0)
        tbl.add_row(when, f"{dur} min", (b.get("cause", "") or "—")[:40])
    console.print(tbl)
    console.print()


@slo_app.command("delete")
def slo_delete(slo_id: str = typer.Argument(...)) -> None:
    """Remove an SLO definition."""
    from agent.integrations import slo_db

    slo = _slo_resolve_id(slo_id)
    console.print()
    if slo is None:
        _print_error(f"SLO not found: {slo_id}")
        raise typer.Exit(1)

    if not typer.confirm(f"Delete SLO '{slo['name']}' ({slo['service']}/{slo['namespace']})?"):
        console.print("[dim]Cancelled.[/dim]")
        return

    slo_db.delete_slo(slo["id"])
    console.print(f"[green]✅ SLO {slo['id'][:8]} deleted.[/green]")
    console.print()


# ---------------------------------------------------------------------------
# Eval: slo
# ---------------------------------------------------------------------------

@eval_app.command("slo")
def eval_slo() -> None:
    """Run SLO / Error Budget eval suite."""
    try:
        from evals.slo.runner import main as run_evals  # type: ignore[import]
        console.print()
        console.print(Rule("[bold]SLO / Error Budget Eval Suite[/bold]"))
        console.print()
        with console.status("[bold green]Running 6 eval cases...", spinner="dots"):
            passed = run_evals()
        console.print()
        if not passed:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


@eval_app.command("db")
def eval_db() -> None:
    """Run Database Health (RDS/Aurora) eval suite."""
    try:
        from evals.db.runner import main as run_evals  # type: ignore[import]
        console.print()
        console.print(Rule("[bold]Database Health Eval Suite[/bold]"))
        console.print()
        with console.status("[bold green]Running 6 eval cases...", spinner="dots"):
            passed = run_evals()
        console.print()
        if not passed:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        _print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Auto-scaling commands
# ---------------------------------------------------------------------------

@scale_app.command("list")
def scale_list() -> None:
    """List all auto-scaling policies."""
    from agent.integrations import autoscale_db

    policies = autoscale_db.list_policies(enabled_only=False)
    console.print()
    if not policies:
        console.print("[dim]No scaling policies. Create one with `agent scale add` "
                      "or `agent scale setup`.[/dim]")
        console.print()
        return

    tbl = Table(show_lines=False, header_style="bold cyan", border_style="dim")
    tbl.add_column("Name", no_wrap=True)
    tbl.add_column("Deployment", no_wrap=True)
    tbl.add_column("Namespace")
    tbl.add_column("Scale Down", justify="center")
    tbl.add_column("Scale Up", justify="center")
    tbl.add_column("Down Replicas", justify="right")
    tbl.add_column("Up Replicas", justify="right")
    tbl.add_column("Status")

    for pol in policies:
        status = "[green]enabled[/green]" if pol.get("enabled") else "[dim]disabled[/dim]"
        tbl.add_row(
            str(pol.get("name", "")),
            str(pol.get("deployment", "")),
            str(pol.get("namespace", "")),
            str(pol.get("schedule_down_utc") or "-"),
            str(pol.get("schedule_up_utc") or "-"),
            str(pol.get("down_replicas", "")),
            str(pol.get("up_replicas", "")),
            status,
        )

    console.print(tbl)
    console.print()


@scale_app.command("add")
def scale_add(
    deployment:  str = typer.Argument(...),
    namespace:   str = typer.Option("default", "--namespace", "-n"),
    down_time:   str = typer.Option("17:30", "--down-time", help="UTC time HH:MM to scale down"),
    up_time:     str = typer.Option("03:30", "--up-time", help="UTC time HH:MM to scale up"),
    down_r:      int = typer.Option(1, "--down-replicas"),
    up_r:        int = typer.Option(3, "--up-replicas"),
    cpu_pct:   float = typer.Option(80.0, "--cpu-threshold"),
) -> None:
    """Add a scaling policy for a deployment."""
    from agent.integrations import autoscale_db

    policy_id = autoscale_db.add_policy(
        deployment=deployment,
        namespace=namespace,
        name=f"{deployment}-policy",
        min_r=1,
        max_r=max(10, up_r),
        down_utc=down_time,
        up_utc=up_time,
        down_r=down_r,
        up_r=up_r,
        cpu_pct=cpu_pct,
    )
    console.print()
    body = (
        f"[bold green]Scaling policy created[/bold green]\n\n"
        f"  Deployment:  {deployment} / {namespace}\n"
        f"  Scale down:  {down_time} UTC -> {down_r} replicas\n"
        f"  Scale up:    {up_time} UTC -> {up_r} replicas\n"
        f"  CPU thresh:  {cpu_pct}%\n"
        f"  ID:          {policy_id[:8]}"
    )
    console.print(Panel(body, border_style="green", padding=(0, 2)))
    console.print()


@scale_app.command("setup")
def scale_setup(
    region: str = typer.Option("us-east-1", "--region", "-r"),
) -> None:
    """Auto-discover deployments and create default scaling policies."""
    from agent.skills.autoscale import add_default_policies

    console.print()
    with console.status("[bold green]Discovering deployments...", spinner="dots"):
        created = add_default_policies()

    if not created:
        console.print(Panel(
            "[yellow]No new policies created.[/yellow]\n"
            "All discovered deployments already have policies, or no "
            "deployments were found (kube-system is always skipped).",
            border_style="yellow", padding=(0, 2),
        ))
        console.print()
        return

    body = (
        f"[bold green]Created {len(created)} default scaling policies[/bold green]\n\n"
        f"  Scale down: 17:30 UTC (11pm IST)\n"
        f"  Scale up:   03:30 UTC (9am IST)\n"
        f"  CPU thresh: 80%\n\n"
        f"View them with [cyan]agent scale list[/cyan]."
    )
    console.print(Panel(body, border_style="green", padding=(0, 2)))
    console.print()


@scale_app.command("run")
def scale_run() -> None:
    """Manually trigger scheduled scaling check now."""
    from agent.skills.autoscale import run_scheduled_scaling

    console.print()
    with console.status("[bold green]Checking scaling schedules...", spinner="dots"):
        results = run_scheduled_scaling()

    acted = [r for r in results if r.get("action") != "no_action"]
    if not acted:
        console.print(f"[dim]Checked {len(results)} policies — no schedule matched "
                      f"the current time.[/dim]")
        console.print()
        return

    tbl = Table(show_lines=False, header_style="bold cyan", border_style="dim")
    tbl.add_column("Deployment", no_wrap=True)
    tbl.add_column("Namespace")
    tbl.add_column("Action")
    tbl.add_column("Replicas", justify="center")
    tbl.add_column("Result")
    for r in acted:
        result = "[green]ok[/green]" if r.get("success") else "[red]failed[/red]"
        tbl.add_row(
            str(r.get("deployment", "")),
            str(r.get("namespace", "")),
            str(r.get("action", "")),
            f"{r.get('old_replicas')} -> {r.get('new_replicas')}",
            result,
        )
    console.print(tbl)
    console.print()


@scale_app.command("report")
def scale_report() -> None:
    """Show scaling history and stats."""
    from agent.skills.autoscale import get_scale_report

    report = get_scale_report()
    events = report["recent_events"]
    stats = report["stats"]

    console.print()
    if events:
        tbl = Table(show_lines=False, header_style="bold cyan", border_style="dim",
                    title="Recent scale events")
        tbl.add_column("When", no_wrap=True)
        tbl.add_column("Deployment", no_wrap=True)
        tbl.add_column("Namespace")
        tbl.add_column("Action")
        tbl.add_column("Replicas", justify="center")
        tbl.add_column("Status")
        for ev in events:
            when = str(ev.get("created_at", ""))[:19].replace("T", " ")
            tbl.add_row(
                when,
                str(ev.get("deployment", "")),
                str(ev.get("namespace", "")),
                str(ev.get("action", "")),
                f"{ev.get('old_replicas')} -> {ev.get('new_replicas')}",
                str(ev.get("status", "")),
            )
        console.print(tbl)
    else:
        console.print("[dim]No scale events recorded yet.[/dim]")

    console.print()
    body = (
        f"[bold]Auto-scaling stats[/bold]\n\n"
        f"  Total policies:  {stats['total_policies']}\n"
        f"  Events today:    {stats['events_today']}\n"
        f"  Replicas saved:  {stats['replicas_saved']} (scaled down today)"
    )
    console.print(Panel(body, border_style="cyan", padding=(0, 2)))
    console.print()


@eval_app.command("scale")
def eval_scale() -> None:
    """Run auto-scaling policy evals."""
    import subprocess, sys
    r = subprocess.run([sys.executable, "evals/scale/runner.py"], capture_output=False)
    raise typer.Exit(r.returncode)


# ---------------------------------------------------------------------------
# Runbook commands
# ---------------------------------------------------------------------------

@runbook_app.command("list")
def runbook_list() -> None:
    """List all available runbooks."""
    from agent.skills.runbook import list_runbooks

    items = list_runbooks()
    console.print()
    if not items:
        console.print("[dim]No runbooks found. Check data/runbooks.yaml.[/dim]")
        console.print()
        return

    tbl = Table(show_lines=True, header_style="bold cyan", border_style="dim")
    tbl.add_column("ID", no_wrap=True, width=16)
    tbl.add_column("Name", width=28)
    tbl.add_column("Steps", justify="center", width=6)
    tbl.add_column("Trigger Condition")
    for rb in items:
        tbl.add_row(
            rb["id"],
            rb["name"],
            str(rb["step_count"]),
            _escape(rb["trigger_condition"]),
        )
    console.print(tbl)
    console.print()


@runbook_app.command("run")
def runbook_run(
    runbook_id:  str = typer.Argument(...),
    pod:         str = typer.Option("", "--pod",       "-p",  help="Pod name"),
    namespace:   str = typer.Option("default", "--namespace", "-n"),
    node:        str = typer.Option("", "--node",             help="Node name (for node-not-ready)"),
    pvc:         str = typer.Option("", "--pvc",              help="PVC name (for disk-full)"),
    deployment:  str = typer.Option("", "--deployment", "-d", help="Deployment name"),
) -> None:
    """Execute a runbook by name.

    Examples:
      agent runbook run disk-full --pod web-abc --pvc pvc-data
      agent runbook run node-not-ready --node ip-10-0-1-5
      agent runbook run high-memory --pod api-xyz --deployment api
    """
    from agent.skills.runbook import run_runbook

    ctx: dict = {"namespace": namespace}
    if pod:        ctx["pod_name"]        = pod
    if node:       ctx["node_name"]       = node
    if pvc:        ctx["pvc_name"]        = pvc
    if deployment: ctx["deployment_name"] = deployment

    console.print()
    console.print(f"[bold cyan]Running runbook:[/bold cyan] {runbook_id}")
    console.print()

    result = run_runbook(runbook_id, context=ctx, trigger="manual")

    for step in result.get("steps", []):
        icon = "[green]✓[/green]" if step["status"] == "success" else "[red]✗[/red]"
        console.print(f"  {icon} {step['step']}  [dim]{step['status']}[/dim]")
        if step.get("output") and step["status"] != "success":
            console.print(f"    [dim]{_escape(step['output'][:120])}[/dim]")

    console.print()
    status = result.get("status", "unknown")
    color = "green" if status == "success" else "yellow" if status == "partial" else "red"
    console.print(f"[{color}]Runbook {status.upper()}[/{color}]  "
                  f"[dim]run_id={result.get('run_id', '')[:8]}[/dim]")
    console.print()
    if not result.get("success"):
        raise typer.Exit(1)


@runbook_app.command("history")
def runbook_history(
    runbook_id: str = typer.Option("", "--runbook", "-r"),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """Show recent runbook runs."""
    from agent.integrations import runbook_db

    runs = runbook_db.list_runs(runbook_id=runbook_id or None, limit=limit)
    console.print()
    if not runs:
        console.print("[dim]No runs recorded yet.[/dim]")
        console.print()
        return

    tbl = Table(show_lines=False, header_style="bold cyan", border_style="dim")
    tbl.add_column("When", no_wrap=True, width=20)
    tbl.add_column("Runbook", no_wrap=True, width=18)
    tbl.add_column("Trigger", width=10)
    tbl.add_column("Status", width=10)
    tbl.add_column("Steps", justify="center", width=14)
    for run in runs:
        when = str(run.get("started_at", ""))[:19].replace("T", " ")
        status = run.get("status", "")
        color = "green" if status == "success" else "yellow" if status == "partial" else "red"
        steps = f"{run.get('steps_done', 0)}/{run.get('steps_total', 0)}"
        tbl.add_row(
            when,
            str(run.get("runbook_id", "")),
            str(run.get("trigger", "")),
            f"[{color}]{status}[/{color}]",
            steps,
        )
    console.print(tbl)
    console.print()


@runbook_app.command("steps")
def runbook_steps(
    run_id: str = typer.Argument(...),
) -> None:
    """Show all steps for a specific run."""
    from agent.integrations import runbook_db

    steps = runbook_db.get_steps(run_id)
    console.print()
    if not steps:
        console.print(f"[dim]No steps found for run {run_id!r}.[/dim]")
        console.print()
        return

    tbl = Table(show_lines=True, header_style="bold cyan", border_style="dim")
    tbl.add_column("Step", no_wrap=True)
    tbl.add_column("Type", width=8)
    tbl.add_column("Status", width=10)
    tbl.add_column("Output")
    for step in steps:
        status = step.get("status", "")
        color = "green" if status == "success" else "red" if status == "failed" else "dim"
        out = step.get("output") or step.get("error") or ""
        tbl.add_row(
            step.get("step_name", ""),
            step.get("step_type", ""),
            f"[{color}]{status}[/{color}]",
            _escape(out[:80]),
        )
    console.print(tbl)
    console.print()


@eval_app.command("runbook")
def eval_runbook() -> None:
    """Run runbook automation evals."""
    import subprocess, sys
    r = subprocess.run([sys.executable, "evals/runbook/runner.py"], capture_output=False)
    raise typer.Exit(r.returncode)


# ---------------------------------------------------------------------------
# On-call paging commands
# ---------------------------------------------------------------------------

@page_app.command("send")
def page_send(
    title:       str = typer.Argument(...),
    body:        str = typer.Option("", "--body", "-b"),
    severity:    str = typer.Option("critical", "--severity", "-s",
                                    help="critical / error / warning / info"),
    service:     str = typer.Option("", "--service"),
    incident_id: str = typer.Option("", "--incident-id", "-i"),
) -> None:
    """Page the on-call engineer via PagerDuty or OpsGenie."""
    from agent.integrations.pagerduty import page_oncall

    console.print()
    with console.status("[bold red]Paging on-call...", spinner="dots"):
        result = page_oncall(
            title=title, body=body or title,
            severity=severity,  # type: ignore[arg-type]
            service=service, incident_id=incident_id,
        )

    provider = result.get("provider", "none")
    if result.get("skipped"):
        console.print(Panel(
            "[yellow]No on-call provider configured.[/yellow]\n\n"
            "Set [cyan]PAGERDUTY_ROUTING_KEY[/cyan] or [cyan]OPSGENIE_API_KEY[/cyan] in .env\n"
            "then restart the daemon.",
            border_style="yellow", padding=(0, 2),
        ))
    elif result.get("success"):
        console.print(Panel(
            f"[bold green]On-call paged via {provider}[/bold green]\n\n"
            f"  Title:    {title}\n"
            f"  Severity: {severity}\n"
            f"  Service:  {service or '(unset)'}",
            border_style="red", padding=(0, 2),
        ))
    else:
        console.print(Panel(
            f"[red]Paging failed via {provider}.[/red]\n"
            "Check your API key and network connectivity.",
            border_style="red", padding=(0, 2),
        ))
        raise typer.Exit(1)
    console.print()


@page_app.command("resolve")
def page_resolve(
    incident_id: str = typer.Argument(..., help="Incident or dedup key to resolve"),
) -> None:
    """Resolve / close an alert in PagerDuty or OpsGenie."""
    from agent.integrations.pagerduty import resolve_oncall

    console.print()
    with console.status("[bold green]Resolving alert...", spinner="dots"):
        ok = resolve_oncall(incident_id)

    if ok:
        console.print(f"[green]Alert resolved:[/green] {incident_id}")
    else:
        console.print(f"[red]Could not resolve alert[/red] {incident_id}  "
                      "[dim](no provider configured or API error)[/dim]")
    console.print()


@page_app.command("status")
def page_status() -> None:
    """Show which on-call provider is configured."""
    from agent.config import settings

    pd_key = getattr(settings, "pagerduty_routing_key", "") or ""
    og_key = getattr(settings, "opsgenie_api_key", "") or ""
    og_region = getattr(settings, "opsgenie_region", "us") or "us"

    console.print()
    if not pd_key and not og_key:
        console.print(Panel(
            "[yellow]No on-call provider configured.[/yellow]\n\n"
            "Add one of these to your .env file:\n"
            "  PAGERDUTY_ROUTING_KEY=<32-char hex key>\n"
            "  OPSGENIE_API_KEY=<your-key>",
            border_style="yellow", title="On-call Status", padding=(0, 2),
        ))
    else:
        lines = []
        if pd_key:
            masked = pd_key[:4] + "***" + pd_key[-4:]
            lines.append(f"  PagerDuty:  [green]configured[/green]  [dim]{masked}[/dim]")
        else:
            lines.append("  PagerDuty:  [dim]not configured[/dim]")
        if og_key:
            masked = og_key[:4] + "***" + og_key[-4:]
            lines.append(f"  OpsGenie:   [green]configured[/green]  [dim]{masked}[/dim]  region={og_region}")
        else:
            lines.append("  OpsGenie:   [dim]not configured[/dim]")
        active = "PagerDuty" if pd_key else "OpsGenie"
        lines.append(f"\n  Active provider: [bold cyan]{active}[/bold cyan]")
        console.print(Panel(
            "\n".join(lines), border_style="cyan",
            title="On-call Status", padding=(0, 2),
        ))
    console.print()


@eval_app.command("page")
def eval_page() -> None:
    """Run on-call paging evals."""
    import subprocess, sys
    r = subprocess.run([sys.executable, "evals/pagerduty/runner.py"], capture_output=False)
    raise typer.Exit(r.returncode)


# ---------------------------------------------------------------------------
# Dashboard commands
# ---------------------------------------------------------------------------

_DASHBOARD_PORT  = 8501
_FRONTEND_PORT   = 5173
_DASHBOARD_PID   = Path("data/dashboard.pid")
_FRONTEND_PID    = Path("data/dashboard-frontend.pid")
_FRONTEND_DIR    = Path(__file__).parent / "dashboard" / "frontend"


def _frontend_built() -> bool:
    return (_FRONTEND_DIR / "dist" / "index.html").exists()


@dashboard_app.command("start")
def dashboard_start(
    port: int  = typer.Option(_DASHBOARD_PORT, "--port", "-p"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
    rebuild: bool = typer.Option(False, "--rebuild", help="Re-bundle the frontend before starting"),
) -> None:
    """Start the AtlasOS web dashboard."""
    import subprocess, sys

    cflags = (0x00000008 | 0x00000200) if sys.platform == "win32" else 0
    node   = r"C:\Program Files\nodejs\node.exe" if sys.platform == "win32" else "node"

    # ── 1. Build frontend if needed ────────────────────────────────────────
    if rebuild or not _frontend_built():
        console.print("[dim]Building frontend (esbuild)…[/dim]")
        build_script = str(_FRONTEND_DIR / "build.mjs")
        result = subprocess.run([node, build_script], cwd=str(_FRONTEND_DIR), capture_output=True, text=True)
        if result.returncode != 0:
            console.print(f"[red]Build failed:[/red] {result.stderr[-400:]}")
            return
        console.print("[green]Frontend built.[/green]")

    # ── 2. API server ──────────────────────────────────────────────────────
    if _DASHBOARD_PID.exists():
        try:
            import psutil
            pid = int(_DASHBOARD_PID.read_text().strip())
            if psutil.pid_exists(pid):
                console.print(f"[yellow]Already running[/yellow]  [dim]pid={pid}[/dim]")
                if open_browser:
                    import webbrowser; webbrowser.open(f"http://localhost:{port}")
                return
        except Exception:
            pass
        _DASHBOARD_PID.unlink(missing_ok=True)

    _DASHBOARD_PID.parent.mkdir(parents=True, exist_ok=True)
    api_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "agent.dashboard.server:app",
         "--host", "127.0.0.1",
         "--port", str(port),
         "--log-level", "error"],
        creationflags=cflags,
    )
    _DASHBOARD_PID.write_text(str(api_proc.pid))

    ui_url = f"http://localhost:{port}"

    import time; time.sleep(2)

    console.print()
    console.print(Panel(
        f"[bold green]AtlasOS Dashboard running[/bold green]\n\n"
        f"  UI:    [cyan]{ui_url}[/cyan]\n"
        f"  API:   [cyan]http://localhost:{port}[/cyan]\n\n"
        f"  Stop:  [dim]agent dashboard stop[/dim]",
        border_style="green", padding=(0, 2),
    ))
    console.print()

    if open_browser:
        import webbrowser
        webbrowser.open(ui_url)


@dashboard_app.command("stop")
def dashboard_stop() -> None:
    """Stop the running dashboard."""
    import psutil, subprocess, sys

    stopped = []

    # Try PID files first (written by dashboard start)
    for pid_file, label in [(_DASHBOARD_PID, "API server"), (_FRONTEND_PID, "Frontend")]:
        if not pid_file.exists():
            continue
        try:
            # Read with UTF-8 fallback for UTF-16 files written by PowerShell
            raw = pid_file.read_bytes()
            text = raw.decode("utf-16").strip() if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else raw.decode().strip()
            pid = int("".join(c for c in text if c.isdigit()))
            if pid > 4 and psutil.pid_exists(pid):
                psutil.Process(pid).terminate()
                stopped.append(f"{label} (pid {pid})")
        except Exception:
            pass
        pid_file.unlink(missing_ok=True)

    # Fallback: kill by port (catches servers started outside the CLI)
    for port, label in [(_DASHBOARD_PORT, "API server"), (_FRONTEND_PORT, "Frontend")]:
        try:
            for conn in psutil.net_connections(kind="tcp"):
                if conn.laddr.port == port and conn.pid and conn.pid > 4:
                    try:
                        psutil.Process(conn.pid).terminate()
                        tag = f"{label} port {port} (pid {conn.pid})"
                        if tag not in stopped:
                            stopped.append(tag)
                    except Exception:
                        pass
        except Exception:
            pass

    if stopped:
        for s in stopped:
            console.print(f"[green]✓ Stopped[/green] {s}")
    else:
        console.print("[dim]Dashboard is not running.[/dim]")


@dashboard_app.command("open")
def dashboard_open(
    port: int = typer.Option(_FRONTEND_PORT, "--port", "-p"),
) -> None:
    """Open the dashboard in your browser."""
    import webbrowser
    url = f"http://localhost:{port}"
    webbrowser.open(url)
    console.print(f"Opening [cyan]{url}[/cyan]")


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

@app.command("setup")
def cmd_setup() -> None:
    """Interactive wizard to configure AI providers, AWS, Gmail, and integrations."""
    from agent.skills.setup import SetupWizard
    SetupWizard().run()


# ---------------------------------------------------------------------------
# Commands: agent events …
# ---------------------------------------------------------------------------

_STATUS_COLOR = {
    "pending":    "cyan",
    "processing": "yellow",
    "done":       "green",
    "failed":     "red",
    "dead":       "bold red",
}
_PRIORITY_LABEL = {1: "critical", 2: "high", 3: "normal", 4: "low"}
_PRIORITY_CLR   = {1: "bold red", 2: "red", 3: "cyan", 4: "dim"}


@events_app.command("stats")
def events_stats() -> None:
    """Show event queue counts by status."""
    from agent.integrations.event_queue import get_stats
    stats = get_stats()
    console.print()
    t = Table(title="Event Queue Stats", box=None, show_header=True,
              header_style="bold cyan")
    t.add_column("Status",  style="bold", width=14)
    t.add_column("Count",   justify="right", width=8)
    for status in ("pending", "processing", "done", "failed", "dead"):
        color = _STATUS_COLOR.get(status, "white")
        t.add_row(
            f"[{color}]{status}[/{color}]",
            str(stats.get(status, 0)),
        )
    t.add_row("─" * 14, "─" * 8)
    t.add_row("[bold]total[/bold]", f"[bold]{stats.get('total', 0)}[/bold]")
    console.print(t)
    console.print()


@events_app.command("list")
def events_list(
    status: str = typer.Option(
        "", "--status", "-s",
        help="Filter by status: pending / processing / done / failed / dead",
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Max rows to show"),
) -> None:
    """List recent events from the queue."""
    from agent.integrations.event_queue import list_events
    rows = list_events(status=status or None, limit=limit)
    console.print()
    if not rows:
        console.print("[dim]No events found.[/dim]")
        console.print()
        return

    t = Table(box=None, show_header=True, header_style="bold cyan", expand=True)
    t.add_column("ID",         width=10)
    t.add_column("Type",       width=20)
    t.add_column("Status",     width=12)
    t.add_column("Pri",        width=9)
    t.add_column("Retries",    width=8, justify="right")
    t.add_column("Created",    width=20)
    t.add_column("Error",      no_wrap=False)

    for e in rows:
        sid    = e["id"][:8]
        stype  = e["event_type"]
        st     = e["status"]
        scolor = _STATUS_COLOR.get(st, "white")
        pri    = e.get("priority", 3)
        pcolor = _PRIORITY_CLR.get(pri, "white")
        plabel = _PRIORITY_LABEL.get(pri, str(pri))
        retries = f"{e.get('retry_count', 0)}/{e.get('max_retries', 3)}"
        created = (e.get("created_at") or "")[:16].replace("T", " ")
        err     = (e.get("error") or "")[:60]
        t.add_row(
            f"[dim]{sid}[/dim]",
            stype,
            f"[{scolor}]{st}[/{scolor}]",
            f"[{pcolor}]{plabel}[/{pcolor}]",
            retries,
            created,
            f"[dim]{_escape(err)}[/dim]" if err else "",
        )
    console.print(t)
    console.print()


@events_app.command("show")
def events_show(
    event_id: str = typer.Argument(..., help="Event ID or prefix (first 8 chars)"),
) -> None:
    """Show full detail for a single event."""
    from agent.integrations.event_queue import get_event, list_events
    import json as _json

    # Support short-ID prefix lookup
    event = get_event(event_id)
    if event is None:
        # Try prefix match
        all_evts = list_events(limit=200)
        matches  = [e for e in all_evts if e["id"].startswith(event_id)]
        event    = matches[0] if matches else None

    if event is None:
        console.print(f"[red]Event not found:[/red] {event_id}")
        raise typer.Exit(1)

    lines = [
        f"[bold]ID:[/bold]         {event['id']}",
        f"[bold]Type:[/bold]       {event['event_type']}",
        f"[bold]Status:[/bold]     [{_STATUS_COLOR.get(event['status'], 'white')}]{event['status']}[/{_STATUS_COLOR.get(event['status'], 'white')}]",
        f"[bold]Priority:[/bold]   {_PRIORITY_LABEL.get(event.get('priority', 3), '?')}",
        f"[bold]Retries:[/bold]    {event.get('retry_count', 0)} / {event.get('max_retries', 3)}",
        f"[bold]Created:[/bold]    {event.get('created_at', '')}",
        f"[bold]Updated:[/bold]    {event.get('updated_at', '')}",
        f"[bold]Scheduled:[/bold]  {event.get('scheduled_at', '')}",
        f"[bold]Worker:[/bold]     {event.get('worker_id', '') or '—'}",
    ]
    if event.get("error"):
        lines.append(f"[bold red]Error:[/bold red]      {_escape(event['error'])}")
    lines.append(f"\n[bold]Payload:[/bold]\n{_escape(_json.dumps(event.get('payload', {}), indent=2))}")
    if event.get("result"):
        lines.append(f"\n[bold]Result:[/bold]\n{_escape(_json.dumps(event.get('result', {}), indent=2))}")

    console.print()
    console.print(Panel("\n".join(lines), title=f"Event {event['id'][:8]}", border_style="cyan"))
    console.print()


@events_app.command("retry")
def events_retry(
    event_id: str = typer.Argument(..., help="Event ID (or prefix) to retry"),
) -> None:
    """Move a dead-letter event back to pending for manual retry."""
    from agent.integrations.event_queue import retry_dead, list_events

    # Support short prefix
    target = event_id
    if len(event_id) < 36:
        all_evts = list_events(status="dead", limit=200)
        matches  = [e["id"] for e in all_evts if e["id"].startswith(event_id)]
        if not matches:
            console.print(f"[red]No dead event found with ID prefix:[/red] {event_id}")
            raise typer.Exit(1)
        target = matches[0]

    retry_dead(target)
    console.print(f"[green]✓ Event {target[:8]} requeued — the worker will pick it up shortly.[/green]")
    console.print()


@events_app.command("enqueue")
def events_enqueue(
    event_type: str = typer.Argument(..., help="Event type, e.g. alert.slack"),
    payload:    str = typer.Option("{}", "--payload", "-p", help="JSON payload string"),
    priority:   int = typer.Option(3, "--priority", help="1=critical 2=high 3=normal 4=low"),
) -> None:
    """Manually enqueue an event (useful for testing handlers)."""
    import json as _json
    from agent.integrations.event_queue import enqueue

    try:
        data = _json.loads(payload)
    except Exception as exc:
        console.print(f"[red]Invalid JSON payload:[/red] {exc}")
        raise typer.Exit(1)

    eid = enqueue(event_type, data, priority=priority)
    console.print(f"[green]✓ Enqueued[/green] [cyan]{event_type}[/cyan] → ID: [dim]{eid[:8]}[/dim]")
    console.print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()
