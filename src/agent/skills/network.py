"""
Network skill — CNI, kube-proxy, NetworkPolicy, pod connectivity, node health.
"""
from __future__ import annotations

import asyncio
import json

from agent.core import llm
from agent.core.models import (
    NetworkDiagnosis, NetworkIssue, NetworkProblemType, NetworkScanReport,
)
from agent.integrations.network_collector import collect_all_network
from agent.integrations.kubectl import run_kubectl
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SCAN_SYSTEM = """\
You are a Kubernetes networking expert.  Given collector data return ONLY valid
JSON (no markdown fences).  Schema:
{
  "summary": "<one paragraph>",
  "cni_detected": "<calico|flannel|cilium|weave|null>",
  "cni_healthy": <bool>,
  "nodes_ready": <int>,
  "nodes_total": <int>,
  "issues": [
    {
      "severity": "critical|warning|info",
      "problem_type": "<NetworkProblemType value>",
      "resource": "<name>",
      "namespace": "<ns or empty>",
      "description": "<what is wrong>",
      "fix": "<plain-language remedy>",
      "fix_command": "<kubectl command or null>"
    }
  ]
}
Valid NetworkProblemType values: CniNotRunning, CniNotDetected, KubeProxyDown,
ServiceNoEndpoints, PodConnectivityFail, NetworkPolicyBlocking,
NodeNetworkUnavailable, NodePressure, Healthy, Unknown.
"""

_DIAG_SYSTEM = """\
You are a Kubernetes networking expert.  Given details about a failing network
component return ONLY valid JSON (no markdown fences).  Schema:
{
  "name": "<resource name>",
  "namespace": "<ns>",
  "problem_type": "<NetworkProblemType value>",
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


class NetworkSkill(BaseSkill):
    """Scans, diagnoses, and fixes cluster networking problems."""

    @property
    def name(self) -> str:
        return "network"

    @property
    def description(self) -> str:
        return "Diagnose and fix cluster network problems (CNI, kube-proxy, NetworkPolicy, connectivity)."

    def execute(self, action: str = "scan", **kwargs) -> str:
        if action == "scan":
            return self.scan()
        if action == "diagnose":
            return str(self.diagnose(kwargs.get("name", "cni"),
                                     kwargs.get("ns", "kube-system")))
        if action == "restart-cni":
            return self.restart_cni(kwargs.get("cni", "calico"))
        return self.scan()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def scan(self) -> str:
        from rich.console import Console
        from rich.panel  import Panel
        from rich.rule   import Rule
        from rich.progress import Progress, SpinnerColumn, TextColumn

        console = Console()

        with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                      console=console, transient=True) as prog:
            task = prog.add_task("Collecting network data...", total=None)
            data, elapsed = collect_all_network()
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
                    max_tokens=1400,
                ))
                parsed = _parse_json(raw.content)
            except Exception as exc:
                log.warning("network.scan.llm_failed", error=str(exc))
                parsed = {"summary": "LLM unavailable.", "cni_detected": None,
                          "cni_healthy": False, "nodes_ready": 0,
                          "nodes_total": 0, "issues": []}

        report = NetworkScanReport(
            generated_at  = __import__("datetime").datetime.utcnow().isoformat(),
            collection_ms = elapsed,
            analysis      = parsed.get("summary"),
            cni_detected  = parsed.get("cni_detected"),
            cni_healthy   = parsed.get("cni_healthy", False),
            nodes_ready   = parsed.get("nodes_ready", 0),
            nodes_total   = parsed.get("nodes_total", 0),
            issues = [
                NetworkIssue(
                    severity     = i.get("severity", "info"),
                    problem_type = NetworkProblemType(i.get("problem_type", "Unknown")),
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

    def diagnose(self, name: str, ns: str = "kube-system") -> NetworkDiagnosis:
        from rich.console import Console
        console = Console()

        data, _ = collect_all_network()
        area_text = "\n".join(
            f"[{a}] ok={r['ok']} {r['summary']}\n{json.dumps(r.get('data', {}), indent=2)[:300]}"
            for a, r in data.items()
        )
        prompt = f"Diagnose network component '{name}' in namespace '{ns}'.\n\n{area_text}"

        try:
            raw = asyncio.run(llm.chat(
                messages=[{"role": "user", "content": prompt}],
                system=_DIAG_SYSTEM,
                json_mode=True,
                max_tokens=800,
            ))
            parsed = _parse_json(raw.content)
        except Exception as exc:
            log.warning("network.diagnose.llm_failed", error=str(exc))
            parsed = {
                "name": name, "namespace": ns,
                "problem_type": "Unknown",
                "root_cause": "LLM unavailable",
                "suggested_fix": "kubectl get pods -A | grep -E 'calico|flannel|cilium|weave'",
                "fix_command": None, "confidence": "low", "explanation": "",
            }

        diag = NetworkDiagnosis(
            name          = parsed.get("name", name),
            namespace     = parsed.get("namespace", ns),
            problem_type  = NetworkProblemType(parsed.get("problem_type", "Unknown")),
            root_cause    = parsed.get("root_cause", ""),
            suggested_fix = parsed.get("suggested_fix", ""),
            fix_command   = parsed.get("fix_command"),
            confidence    = parsed.get("confidence", "low"),
            explanation   = parsed.get("explanation", ""),
        )

        _COLORS = {
            "CniNotRunning":          "red",
            "KubeProxyDown":          "red",
            "PodConnectivityFail":    "red",
            "ServiceNoEndpoints":     "yellow",
            "NetworkPolicyBlocking":  "yellow",
            "Healthy":                "green",
        }
        color = _COLORS.get(str(diag.problem_type), "white")
        console.print(f"\n[bold]Network Diagnosis: {name}[/bold]")
        console.print(f"  Problem:    [{color}]{diag.problem_type}[/{color}]")
        console.print(f"  Root cause: {diag.root_cause}")
        console.print(f"  Fix:        {diag.suggested_fix}")
        if diag.fix_command:
            console.print(f"  Command:    [cyan]{diag.fix_command}[/cyan]")
        return diag

    def restart_cni(self, cni: str = "calico") -> str:
        from rich.console import Console
        console = Console()

        cni_ds_map = {
            "calico":  ("kube-system", "daemonset/calico-node"),
            "flannel": ("kube-flannel", "daemonset/kube-flannel-ds"),
            "cilium":  ("kube-system", "daemonset/cilium"),
            "weave":   ("kube-system", "daemonset/weave-net"),
        }
        if cni not in cni_ds_map:
            console.print(f"[red]Unknown CNI '{cni}'. Supported: {list(cni_ds_map)}[/red]")
            return "failed"

        ns, ds = cni_ds_map[cni]
        console.print(f"[yellow]Restarting {cni} DaemonSet ({ds}) in {ns}...[/yellow]")
        r = run_kubectl(["rollout", "restart", ds, "-n", ns])
        if r.success:
            console.print(f"[green]Rollout restart triggered for {ds}.[/green]")
            console.print(f"[dim]Watch: kubectl rollout status {ds} -n {ns}[/dim]")
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
    "CniNotRunning":          "bold red",
    "KubeProxyDown":          "bold red",
    "PodConnectivityFail":    "bold red",
    "ServiceNoEndpoints":     "yellow",
    "NetworkPolicyBlocking":  "yellow",
    "NodeNetworkUnavailable": "red",
    "NodePressure":           "yellow",
    "Healthy":                "green",
}


def _render_scan(console, report: NetworkScanReport) -> None:
    from rich.panel  import Panel
    from rich.rule   import Rule
    from rich.table  import Table

    console.print(Rule("[bold cyan]Network Scan Report[/bold cyan]"))

    cni_str = report.cni_detected or "unknown"
    status_lines = [
        f"CNI:          {cni_str} ({'[green]healthy[/green]' if report.cni_healthy else '[red]UNHEALTHY[/red]'})",
        f"Nodes ready:  {report.nodes_ready}/{report.nodes_total}",
        f"Collected in: {report.collection_ms:.0f} ms",
    ]
    console.print(Panel("\n".join(status_lines), title="Status", border_style="blue"))

    if report.analysis:
        console.print(Panel(report.analysis, title="Analysis", border_style="dim"))

    if not report.issues:
        console.print("[green]No network issues found.[/green]")
        return

    tbl = Table(show_header=True, header_style="bold magenta")
    tbl.add_column("Severity",  width=8)
    tbl.add_column("Type",      width=26)
    tbl.add_column("Resource",  width=20)
    tbl.add_column("Description")
    tbl.add_column("Fix")

    for iss in sorted(report.issues, key=lambda i: ("critical","warning","info").index(i.severity)):
        scolor = _SEVERITY_COLOR.get(iss.severity, "white")
        pcolor = _PROBLEM_COLOR.get(str(iss.problem_type), "white")
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
