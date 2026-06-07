"""
DNS skill — diagnoses and fixes CoreDNS / cluster-DNS problems.
"""
from __future__ import annotations

import asyncio
import json

from agent.core import llm
from agent.core.models import (
    DNSDiagnosis, DNSIssue, DNSProblemType, DNSScanReport,
)
from agent.integrations.dns_collector import collect_all_dns
from agent.integrations.kubectl import run_kubectl
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SCAN_SYSTEM = """\
You are a Kubernetes DNS expert.  Given collector data return ONLY valid JSON
(no markdown fences).  Schema:
{
  "summary": "<one paragraph>",
  "coredns_healthy": <bool>,
  "resolution_ok": <bool>,
  "external_dns_found": <bool>,
  "issues": [
    {
      "severity": "critical|warning|info",
      "problem_type": "<DNSProblemType value>",
      "resource": "<name>",
      "namespace": "<ns or empty>",
      "description": "<what is wrong>",
      "fix": "<plain-language remedy>",
      "fix_command": "<kubectl/helm command or null>"
    }
  ]
}
Valid DNSProblemType values: CoreDNSNotRunning, CoreDNSNotInstalled,
CoreDNSConfigInvalid, DNSResolutionFail, ExternalDNSFail,
NdotsMisconfigured, KubeDNSSvcMissing, NoEndpoints, Healthy, Unknown.
"""

_DIAG_SYSTEM = """\
You are a Kubernetes DNS expert.  Given details about a failing DNS component
return ONLY valid JSON (no markdown fences).  Schema:
{
  "name": "<resource name>",
  "namespace": "<ns>",
  "problem_type": "<DNSProblemType value>",
  "root_cause": "<technical cause>",
  "suggested_fix": "<step-by-step>",
  "fix_command": "<exact command or null>",
  "confidence": "high|medium|low",
  "explanation": "<why this fix works>"
}
"""


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text  = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
    return json.loads(text)


class DNSSkill(BaseSkill):
    """Scans, diagnoses, and fixes cluster DNS problems."""

    @property
    def name(self) -> str:
        return "dns"

    @property
    def description(self) -> str:
        return "Diagnose and fix cluster DNS problems (CoreDNS, resolution, external-dns)."

    def execute(self, action: str = "scan", **kwargs) -> str:
        if action == "scan":
            return self.scan()
        if action == "diagnose":
            return str(self.diagnose(kwargs.get("name", "coredns"),
                                     kwargs.get("ns", "kube-system")))
        if action == "restart-coredns":
            return self.restart_coredns()
        return self.scan()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def scan(self) -> str:
        from rich.console import Console
        from rich.panel import Panel
        from rich.rule import Rule
        from rich import print as rprint
        from rich.progress import Progress, SpinnerColumn, TextColumn

        console = Console()

        with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                      console=console, transient=True) as prog:
            task = prog.add_task("Collecting DNS data...", total=None)
            data, elapsed = collect_all_dns()
            prog.update(task, description="Analysing with Claude...")

            collector_text = "\n".join(
                f"[{area}] ok={r['ok']} | {r['summary']}"
                for area, r in data.items()
            )
            try:
                raw = asyncio.run(llm.chat(
                    messages=[{"role": "user", "content": collector_text}],
                    system=_SCAN_SYSTEM,
                    json_mode=True,
                    max_tokens=1200,
                ))
                parsed = _parse_json(raw.content)
            except Exception as exc:
                log.warning("dns.scan.llm_failed", error=str(exc))
                parsed = {"summary": "LLM unavailable.",
                          "coredns_healthy": False, "resolution_ok": False,
                          "external_dns_found": False, "issues": []}

        # Build report
        report = DNSScanReport(
            generated_at  = __import__("datetime").datetime.utcnow().isoformat(),
            collection_ms = elapsed,
            analysis      = parsed.get("summary"),
            coredns_healthy    = parsed.get("coredns_healthy", False),
            resolution_ok      = parsed.get("resolution_ok", False),
            external_dns_found = parsed.get("external_dns_found", False),
            issues = [
                DNSIssue(
                    severity     = i.get("severity", "info"),
                    problem_type = DNSProblemType(i.get("problem_type", "Unknown")),
                    resource     = i.get("resource", "?"),
                    namespace    = i.get("namespace", ""),
                    description  = i.get("description", ""),
                    fix          = i.get("fix", ""),
                    fix_command  = i.get("fix_command"),
                )
                for i in parsed.get("issues", [])
            ],
        )

        _render_scan(console, report)
        return "scan_complete"

    def diagnose(self, name: str, ns: str = "kube-system") -> DNSDiagnosis:
        from rich.console import Console
        from rich import print as rprint
        console = Console()

        data, _ = collect_all_dns()
        area_text = "\n".join(
            f"[{a}] ok={r['ok']} {r['summary']}\n{json.dumps(r.get('data', {}), indent=2)[:300]}"
            for a, r in data.items()
        )
        prompt = f"Diagnose DNS component '{name}' in namespace '{ns}'.\n\n{area_text}"

        try:
            raw = asyncio.run(llm.chat(
                messages=[{"role": "user", "content": prompt}],
                system=_DIAG_SYSTEM,
                json_mode=True,
                max_tokens=800,
            ))
            parsed = _parse_json(raw.content)
        except Exception as exc:
            log.warning("dns.diagnose.llm_failed", error=str(exc))
            parsed = {
                "name": name, "namespace": ns,
                "problem_type": "Unknown",
                "root_cause": "LLM unavailable",
                "suggested_fix": "Check kubectl get pods -n kube-system -l k8s-app=kube-dns",
                "fix_command": None, "confidence": "low", "explanation": "",
            }

        diag = DNSDiagnosis(
            name          = parsed.get("name", name),
            namespace     = parsed.get("namespace", ns),
            problem_type  = DNSProblemType(parsed.get("problem_type", "Unknown")),
            root_cause    = parsed.get("root_cause", ""),
            suggested_fix = parsed.get("suggested_fix", ""),
            fix_command   = parsed.get("fix_command"),
            confidence    = parsed.get("confidence", "low"),
            explanation   = parsed.get("explanation", ""),
        )

        _COLORS = {
            "CoreDNSNotRunning":    "red",
            "DNSResolutionFail":    "red",
            "CoreDNSConfigInvalid": "yellow",
            "Healthy":              "green",
        }
        color = _COLORS.get(diag.problem_type, "white")
        console.print(f"\n[bold]DNS Diagnosis: {name}[/bold]")
        console.print(f"  Problem:    [{color}]{diag.problem_type}[/{color}]")
        console.print(f"  Root cause: {diag.root_cause}")
        console.print(f"  Fix:        {diag.suggested_fix}")
        if diag.fix_command:
            console.print(f"  Command:    [cyan]{diag.fix_command}[/cyan]")
        return diag

    def restart_coredns(self) -> str:
        from rich.console import Console
        console = Console()
        console.print("[yellow]Restarting CoreDNS pods...[/yellow]")

        r = run_kubectl(["rollout", "restart", "deployment/coredns", "-n", "kube-system"])
        if r.success:
            console.print("[green]CoreDNS rollout restart triggered.[/green]")
            console.print("[dim]Watch: kubectl rollout status deployment/coredns -n kube-system[/dim]")
        else:
            console.print(f"[red]Failed: {r.error}[/red]")
        return "restart_done" if r.success else "restart_failed"

    def apply_fix(self, cmd: str, resource: str = "", ns: str = "") -> str:
        from rich.console import Console
        console = Console()
        import subprocess

        steps = [s.strip() for s in cmd.split("&&") if s.strip()]
        success = True

        for step in steps:
            tokens = step.split()
            if not tokens:
                continue
            binary = tokens[0]
            console.print(f"[dim]$ {step}[/dim]")

            if binary == "kubectl":
                args = tokens[1:]
                if ns and "-n" not in args and "--namespace" not in args:
                    args += ["-n", ns]
                r = run_kubectl(args)
                if r.success:
                    console.print(f"[green]✓ {r.output[:120]}[/green]")
                else:
                    console.print(f"[red]✗ {r.error[:120]}[/red]")
                    success = False
                    break
            else:
                try:
                    r2 = subprocess.run(tokens, capture_output=True, text=True, timeout=60)
                    if r2.returncode == 0:
                        console.print(f"[green]✓ {(r2.stdout or '').strip()[:120]}[/green]")
                    else:
                        console.print(f"[red]✗ {(r2.stderr or r2.stdout or '').strip()[:120]}[/red]")
                        success = False
                        break
                except Exception as exc:
                    console.print(f"[red]✗ {exc}[/red]")
                    success = False
                    break

        return "ok" if success else "failed"


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

_SEVERITY_COLOR = {"critical": "red", "warning": "yellow", "info": "cyan"}
_PROBLEM_COLOR  = {
    "CoreDNSNotRunning":    "bold red",
    "DNSResolutionFail":    "bold red",
    "CoreDNSConfigInvalid": "yellow",
    "NdotsMisconfigured":   "yellow",
    "Healthy":              "green",
}


def _render_scan(console, report: DNSScanReport) -> None:
    from rich.panel  import Panel
    from rich.rule   import Rule
    from rich.table  import Table

    console.print(Rule("[bold cyan]DNS Scan Report[/bold cyan]"))

    status_lines = [
        f"CoreDNS healthy:    {'[green]YES[/green]' if report.coredns_healthy else '[red]NO[/red]'}",
        f"Resolution OK:      {'[green]YES[/green]' if report.resolution_ok else '[red]NO[/red]'}",
        f"External-DNS found: {'[green]YES[/green]' if report.external_dns_found else '[dim]no[/dim]'}",
        f"Collected in:       {report.collection_ms:.0f} ms",
    ]
    console.print(Panel("\n".join(status_lines), title="Status", border_style="blue"))

    if report.analysis:
        console.print(Panel(report.analysis, title="Analysis", border_style="dim"))

    if not report.issues:
        console.print("[green]No DNS issues found.[/green]")
        return

    tbl = Table(show_header=True, header_style="bold magenta")
    tbl.add_column("Severity",  width=8)
    tbl.add_column("Type",      width=24)
    tbl.add_column("Resource",  width=20)
    tbl.add_column("Description")
    tbl.add_column("Fix")

    for iss in sorted(report.issues, key=lambda i: ("critical","warning","info").index(i.severity)):
        scolor  = _SEVERITY_COLOR.get(iss.severity, "white")
        pcolor  = _PROBLEM_COLOR.get(str(iss.problem_type), "white")
        tbl.add_row(
            f"[{scolor}]{iss.severity}[/{scolor}]",
            f"[{pcolor}]{iss.problem_type}[/{pcolor}]",
            iss.resource,
            iss.description,
            iss.fix,
        )
        if iss.fix_command:
            tbl.add_row("", "", "", "[dim]cmd:[/dim]",
                        f"[cyan]{iss.fix_command}[/cyan]")

    console.print(tbl)
