"""
Resource Monitor skill — CPU and memory monitoring for pods and nodes.

Polls kubectl top nodes / kubectl top pods, computes percent-of-limit,
generates ResourceAlerts when thresholds are breached, and calls Claude
for a prioritised fix recommendation.

Severity rules (configurable in config.py):
  CPU warning   >= 70%   CPU critical >= 85%
  MEM warning   >= 75%   MEM critical >= 90%
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import (
    NodeResourceMetrics,
    PodResourceMetrics,
    ResourceAlert,
    ResourceReport,
)
from agent.integrations.kubectl import get_hpa_status, get_node_metrics_top, get_pod_metrics
from agent.memory.retrieval import remember
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

# AWS/K8s managed components — can't set limits, don't alert on NO_LIMITS
_SYSTEM_NAMESPACES: frozenset[str] = frozenset({
    "kube-system", "kube-public", "kube-node-lease",
    "cert-manager", "ingress-nginx", "amazon-cloudwatch",
    "aws-load-balancer-controller", "cluster-autoscaler",
    "external-dns",
})

_SYSTEM_NAME_PREFIXES: tuple[str, ...] = (
    "aws-node", "kube-proxy", "coredns", "ebs-csi",
    "aws-eks-nodeagent", "node-driver-registrar", "liveness-probe",
)


def _is_system_component(name: str, namespace: str) -> bool:
    """Return True for AWS/K8s managed pods that can't have limits set."""
    if namespace in _SYSTEM_NAMESPACES:
        return any(name.startswith(p) for p in _SYSTEM_NAME_PREFIXES)
    return False


# Default thresholds — overridden by config if loaded
_CPU_WARN = 70
_CPU_CRIT = 85
_MEM_WARN = 75
_MEM_CRIT = 90

_ANALYZE_SYSTEM = """You are an SRE expert specializing in Kubernetes resource management.
Analyze this resource report and:
1. Identify pods at risk of OOMKill
2. Identify CPU throttled pods
3. Find pods with no resource limits (dangerous)
4. Suggest specific limit increases or decreases
5. Identify if cluster needs more nodes
6. Prioritize fixes by risk level
Return a concise technical analysis in 4-6 sentences."""


def _bump_memory(current: str, factor: float) -> str:
    """Increase a memory limit string by `factor`, rounding to 64Mi boundary."""
    current = current.strip()
    mb = 0
    if current.endswith("Mi"):
        mb = int(current[:-2])
    elif current.endswith("Gi"):
        mb = int(current[:-2]) * 1024
    elif current.endswith("Ki"):
        mb = max(int(current[:-2]) // 1024, 1)
    else:
        return current
    new_mb = ((int(mb * factor) + 63) // 64) * 64
    return f"{new_mb // 1024}Gi" if new_mb >= 1024 else f"{new_mb}Mi"


def _sev_color(sev: str) -> str:
    return {"critical": "bold red", "warning": "yellow", "info": "dim"}.get(sev, "white")


class ResourceMonitorSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "resource-monitor"

    @property
    def description(self) -> str:
        return "Monitor CPU/memory for pods and nodes; alert before OOMKills or throttling."

    def execute(self, input_data: dict) -> dict:
        namespace = input_data.get("namespace", "all")
        report = self.scan_resources(namespace)
        return report.model_dump()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan_resources(self, namespace: str = "all") -> ResourceReport:
        """Full scan: metrics + limits + alerts + Claude analysis."""
        try:
            from agent.config import settings
            cpu_warn = settings.resource_cpu_warning
            cpu_crit = settings.resource_cpu_critical
            mem_warn = settings.resource_mem_warning
            mem_crit = settings.resource_mem_critical
        except Exception:
            cpu_warn, cpu_crit, mem_warn, mem_crit = _CPU_WARN, _CPU_CRIT, _MEM_WARN, _MEM_CRIT

        nodes = get_node_metrics_top()
        pods  = get_pod_metrics(namespace)

        alerts   = self._generate_alerts(nodes, pods, cpu_warn, cpu_crit, mem_warn, mem_crit)
        critical = sum(1 for a in alerts if a.severity == "critical")
        warning  = sum(1 for a in alerts if a.severity == "warning")

        alerted_pods = {a.pod_or_node for a in alerts}
        healthy  = max(len(pods) - len(alerted_pods), 0)

        no_limits = [
            f"{p.namespace}/{p.name}"
            for p in pods
            if p.risk_type == "no_limits" and not _is_system_component(p.name, p.namespace)
        ]

        report = ResourceReport(
            nodes               = nodes,
            pods                = pods,
            alerts              = alerts,
            critical_count      = critical,
            warning_count       = warning,
            healthy_count       = healthy,
            pods_without_limits = no_limits,
            generated_at        = datetime.now(timezone.utc).isoformat(),
        )

        if nodes or pods:
            report.claude_analysis = self.analyze_with_claude(report)

        remember(
            content=(
                f"Resource scan: {len(nodes)} nodes, {len(pods)} pods. "
                f"{critical} critical, {warning} warnings. "
                f"{len(no_limits)} pods without limits. "
                f"{report.claude_analysis[:200]}"
            ),
            source="resource-monitor",
            metadata={
                "critical":   critical,
                "warning":    warning,
                "no_limits":  len(no_limits),
                "namespace":  namespace,
                "nodes":      len(nodes),
                "pods":       len(pods),
            },
        )

        log.info(
            "resource_monitor.scan_done",
            nodes=len(nodes), pods=len(pods),
            critical=critical, warning=warning,
        )
        return report

    def analyze_with_claude(self, report: ResourceReport) -> str:
        """Send resource data to Claude for prioritised analysis."""
        lines = ["=== NODE METRICS ==="]
        for n in report.nodes:
            lines.append(
                f"  {n.name}: CPU {n.cpu_usage} ({n.cpu_percent}%)"
                f" MEM {n.memory_usage} ({n.memory_percent}%) — {n.status}"
            )

        lines.append("\n=== AT-RISK PODS ===")
        at_risk = [p for p in report.pods if p.at_risk]
        for p in sorted(at_risk, key=lambda x: max(x.cpu_percent, x.memory_percent), reverse=True)[:20]:
            lines.append(
                f"  {p.namespace}/{p.name}: "
                f"CPU {p.cpu_usage} ({p.cpu_percent}% of {p.cpu_limit})  "
                f"MEM {p.memory_usage} ({p.memory_percent}% of {p.memory_limit}) "
                f"risk={p.risk_type}"
            )

        lines.append(f"\n=== SUMMARY ===")
        lines.append(f"  {len(report.nodes)} nodes, {len(report.pods)} pods total")
        lines.append(f"  {report.critical_count} critical, {report.warning_count} warnings")
        lines.append(f"  {len(report.pods_without_limits)} pods without resource limits")

        try:
            resp = run_sync(llm.chat(
                messages=[context.user_message("\n".join(lines))],
                system=_ANALYZE_SYSTEM,
                max_tokens=512,
            ))
            return resp.content.strip()
        except Exception as exc:
            log.warning("resource_monitor.analyze.failed", error=str(exc))
            return f"Analysis unavailable: {exc}"

    def get_recommendations(self, report: ResourceReport) -> list[ResourceAlert]:
        """Re-derive alerts from a ResourceReport (convenience wrapper)."""
        return self._generate_alerts(
            report.nodes, report.pods,
            _CPU_WARN, _CPU_CRIT, _MEM_WARN, _MEM_CRIT,
        )

    def watch_resources(
        self,
        namespace: str = "all",
        interval: int = 30,
        alert_only: bool = False,
        console=None,
    ) -> None:
        """Poll metrics every `interval` seconds with a live display."""
        if console is None:
            from rich.console import Console
            console = Console()

        from agent.integrations.slack import (
            is_configured as slack_ok,
            send_alert as slack_send,
            send_recovery as slack_recover,
            send_watch_started,
        )

        try:
            from agent.config import settings
            alert_warnings  = settings.slack_alert_on_warning
            alert_recovery  = settings.slack_alert_on_recovery
            cpu_warn = settings.resource_cpu_warning
            cpu_crit = settings.resource_cpu_critical
            mem_warn = settings.resource_mem_warning
            mem_crit = settings.resource_mem_critical
        except Exception:
            alert_warnings = False
            alert_recovery = True
            cpu_warn, cpu_crit, mem_warn, mem_crit = _CPU_WARN, _CPU_CRIT, _MEM_WARN, _MEM_CRIT

        slack_enabled = slack_ok()
        prev_critical: set[str] = set()
        snapshot_timer = 0

        slack_status = (
            "[green]Slack alerts: ON[/green]"
            if slack_enabled else
            "[dim]Slack alerts: off (set SLACK_WEBHOOK_URL in .env to enable)[/dim]"
        )
        console.print(
            f"  [dim]Watching resources every {interval}s — Ctrl+C to stop[/dim]  "
            + slack_status + "\n"
        )

        if slack_enabled:
            send_watch_started(namespace, interval)

        try:
            while True:
                try:
                    nodes  = get_node_metrics_top()
                    pods   = get_pod_metrics(namespace)
                    alerts = self._generate_alerts(
                        nodes, pods, cpu_warn, cpu_crit, mem_warn, mem_crit
                    )
                except Exception as exc:
                    console.print(f"[red]Error collecting metrics: {exc}[/red]")
                    time.sleep(interval)
                    continue

                if not alert_only:
                    console.clear()
                    _render_watch_display(console, nodes, pods, alerts, interval, slack_enabled)

                # Build maps for new-alert detection
                current_critical = {
                    f"{a.pod_or_node}/{a.namespace}"
                    for a in alerts if a.severity == "critical"
                }
                alert_map = {
                    f"{a.pod_or_node}/{a.namespace}": a
                    for a in alerts
                }

                # New critical alerts — print + send Slack
                for key in current_critical - prev_critical:
                    console.print(
                        f"[bold red][NEW CRITICAL][/bold red] "
                        f"{key} crossed critical threshold!"
                    )
                    alert = alert_map.get(key)
                    if alert:
                        console.print(f"  [dim]{alert.recommendation}[/dim]")
                        if slack_enabled:
                            sent = slack_send(alert)
                            console.print(
                                f"  [green]Slack: alert sent[/green]"
                                if sent else
                                f"  [yellow]Slack: send failed[/yellow]"
                            )

                # Warnings (if enabled)
                if slack_enabled and alert_warnings:
                    current_warnings = {
                        f"{a.pod_or_node}/{a.namespace}"
                        for a in alerts if a.severity == "warning"
                    }
                    for key in current_warnings - prev_critical:
                        alert = alert_map.get(key)
                        if alert:
                            slack_send(alert)

                # Resolved alerts — notify Slack
                if slack_enabled and alert_recovery:
                    for key in prev_critical - current_critical:
                        parts = key.split("/", 1)
                        pod, ns = (parts[0], parts[1]) if len(parts) == 2 else (key, "")
                        sent = slack_recover(pod, ns)
                        if sent:
                            console.print(f"  [green]Slack: resolved alert sent for {key}[/green]")

                prev_critical = current_critical

                snapshot_timer += interval
                if snapshot_timer >= 300:
                    self._save_snapshot(nodes, pods, namespace)
                    snapshot_timer = 0

                time.sleep(interval)

        except KeyboardInterrupt:
            console.print("\n[dim]Stopped.[/dim]")

    def apply_fix(self, alert: ResourceAlert) -> bool:
        """Execute a fix command — always called from CLI after user approval."""
        if not alert.fix_command:
            log.warning("resource_monitor.apply_fix.no_command", resource=alert.pod_or_node)
            return False

        from agent.skills._fix_runner import apply_shell_fix

        log.info("resource_monitor.apply_fix",
                 resource=alert.pod_or_node, cmd=alert.fix_command)
        success = apply_shell_fix(alert.fix_command) == "ok"

        remember(
            content=(
                f"Resource fix {alert.pod_or_node} ({alert.namespace}): "
                f"`{alert.fix_command}` — {'succeeded' if success else 'failed'}"
            ),
            source="resource-fix",
            metadata={
                "resource":    alert.pod_or_node,
                "namespace":   alert.namespace,
                "alert_type":  alert.alert_type,
                "fix_command": alert.fix_command,
                "success":     success,
            },
        )
        return success

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _generate_alerts(
        self,
        nodes: list[NodeResourceMetrics],
        pods:  list[PodResourceMetrics],
        cpu_warn: int = _CPU_WARN,
        cpu_crit: int = _CPU_CRIT,
        mem_warn: int = _MEM_WARN,
        mem_crit: int = _MEM_CRIT,
    ) -> list[ResourceAlert]:
        alerts: list[ResourceAlert] = []

        # Node alerts
        for node in nodes:
            if node.cpu_percent >= cpu_crit:
                alerts.append(ResourceAlert(
                    pod_or_node   = node.name,
                    alert_type    = "CPU_HIGH",
                    severity      = "critical",
                    current_usage = node.cpu_usage,
                    limit         = "node capacity",
                    percent_used  = node.cpu_percent,
                    recommendation = (
                        f"Node {node.name} CPU at {node.cpu_percent}% — "
                        "redistribute workloads or add nodes"
                    ),
                    fix_command   = f"kubectl describe node {node.name}",
                ))
            elif node.cpu_percent >= cpu_warn:
                alerts.append(ResourceAlert(
                    pod_or_node   = node.name,
                    alert_type    = "CPU_HIGH",
                    severity      = "warning",
                    current_usage = node.cpu_usage,
                    limit         = "node capacity",
                    percent_used  = node.cpu_percent,
                    recommendation = f"Node {node.name} CPU at {node.cpu_percent}% — monitor closely",
                ))

            if node.memory_percent >= mem_crit:
                alerts.append(ResourceAlert(
                    pod_or_node   = node.name,
                    alert_type    = "MEM_HIGH",
                    severity      = "critical",
                    current_usage = node.memory_usage,
                    limit         = "node capacity",
                    percent_used  = node.memory_percent,
                    recommendation = (
                        f"Node {node.name} memory at {node.memory_percent}% — "
                        "OOMKill risk for ALL pods on this node"
                    ),
                    fix_command   = f"kubectl describe node {node.name}",
                ))
            elif node.memory_percent >= mem_warn:
                alerts.append(ResourceAlert(
                    pod_or_node   = node.name,
                    alert_type    = "MEM_HIGH",
                    severity      = "warning",
                    current_usage = node.memory_usage,
                    limit         = "node capacity",
                    percent_used  = node.memory_percent,
                    recommendation = (
                        f"Node {node.name} memory at {node.memory_percent}% — "
                        "review pod memory limits"
                    ),
                ))

        # Pod alerts
        for pod in pods:
            # No limits — skip AWS/K8s managed system components (can't set limits on them)
            if pod.risk_type == "no_limits":
                if _is_system_component(pod.name, pod.namespace):
                    continue
                alerts.append(ResourceAlert(
                    pod_or_node   = pod.name,
                    namespace     = pod.namespace,
                    alert_type    = "NO_LIMITS",
                    severity      = "warning",
                    current_usage = f"CPU {pod.cpu_usage} / MEM {pod.memory_usage}",
                    limit         = "none",
                    percent_used  = 0,
                    recommendation = (
                        "No resource limits set — pod can starve other pods on the node. "
                        "Add limits to the deployment spec."
                    ),
                    fix_command   = None,
                    auto_fix      = False,
                ))
                continue

            # Memory alerts
            if pod.memory_percent >= mem_crit:
                new_mem = _bump_memory(pod.memory_limit, 1.5)
                alerts.append(ResourceAlert(
                    pod_or_node   = pod.name,
                    namespace     = pod.namespace,
                    alert_type    = "OOM_RISK",
                    severity      = "critical",
                    current_usage = pod.memory_usage,
                    limit         = pod.memory_limit,
                    percent_used  = pod.memory_percent,
                    recommendation = (
                        f"OOMKill imminent — increase memory limit from "
                        f"{pod.memory_limit} to {new_mem} (+50%)"
                    ),
                    fix_command   = None,  # generated at fix time from deployment owner
                    auto_fix      = False,
                ))
            elif pod.memory_percent >= mem_warn:
                new_mem = _bump_memory(pod.memory_limit, 1.25)
                alerts.append(ResourceAlert(
                    pod_or_node   = pod.name,
                    namespace     = pod.namespace,
                    alert_type    = "MEM_HIGH",
                    severity      = "warning",
                    current_usage = pod.memory_usage,
                    limit         = pod.memory_limit,
                    percent_used  = pod.memory_percent,
                    recommendation = (
                        f"Memory at {pod.memory_percent}% — consider increasing limit "
                        f"from {pod.memory_limit} to {new_mem} (+25%)"
                    ),
                    fix_command   = None,
                    auto_fix      = False,
                ))

            # CPU alerts
            if pod.cpu_percent >= cpu_crit:
                alerts.append(ResourceAlert(
                    pod_or_node   = pod.name,
                    namespace     = pod.namespace,
                    alert_type    = "CPU_HIGH",
                    severity      = "critical",
                    current_usage = pod.cpu_usage,
                    limit         = pod.cpu_limit,
                    percent_used  = pod.cpu_percent,
                    recommendation = (
                        f"CPU throttling at {pod.cpu_percent}% — "
                        "scale deployment up by 1 replica or increase CPU limit"
                    ),
                    fix_command   = None,
                    auto_fix      = False,
                ))
            elif pod.cpu_percent >= cpu_warn:
                alerts.append(ResourceAlert(
                    pod_or_node   = pod.name,
                    namespace     = pod.namespace,
                    alert_type    = "CPU_HIGH",
                    severity      = "warning",
                    current_usage = pod.cpu_usage,
                    limit         = pod.cpu_limit,
                    percent_used  = pod.cpu_percent,
                    recommendation = (
                        f"CPU at {pod.cpu_percent}% — "
                        "check if HPA is configured or increase CPU limit"
                    ),
                    fix_command   = None,
                ))

        return alerts

    def _save_snapshot(
        self,
        nodes: list[NodeResourceMetrics],
        pods:  list[PodResourceMetrics],
        namespace: str,
    ) -> None:
        at_risk   = [p for p in pods if p.at_risk]
        crit_nodes = [n for n in nodes if n.status == "critical"]
        top5 = sorted(at_risk, key=lambda x: max(x.cpu_percent, x.memory_percent), reverse=True)[:5]
        remember(
            content=(
                f"Resource snapshot [{datetime.now().strftime('%H:%M')}]: "
                f"{len(nodes)} nodes ({len(crit_nodes)} critical), "
                f"{len(pods)} pods ({len(at_risk)} at risk). "
                "High usage: "
                + ", ".join(
                    f"{p.name} MEM={p.memory_percent}% CPU={p.cpu_percent}%"
                    for p in top5
                )
            ),
            source="resource-snapshot",
            metadata={
                "namespace":   namespace,
                "nodes_total": len(nodes),
                "pods_total":  len(pods),
                "at_risk":     len(at_risk),
            },
        )


# ---------------------------------------------------------------------------
# Watch display renderer (module-level so CLI can call it too)
# ---------------------------------------------------------------------------

def _render_watch_display(
    console,
    nodes:   list[NodeResourceMetrics],
    pods:    list[PodResourceMetrics],
    alerts:  list[ResourceAlert],
    interval: int,
    slack_enabled: bool = False,
) -> None:
    from rich.table import Table

    console.print(
        f"[dim]Last updated: {datetime.now().strftime('%H:%M:%S')}"
        f"  (refreshes every {interval}s)[/dim]\n"
    )

    # Nodes
    ntbl = Table(title="NODES", show_lines=False, header_style="bold cyan", border_style="dim")
    ntbl.add_column("Node",    min_width=16)
    ntbl.add_column("CPU",     width=10, justify="right")
    ntbl.add_column("CPU %",   width=8,  justify="right")
    ntbl.add_column("Memory",  width=10, justify="right")
    ntbl.add_column("Mem %",   width=8,  justify="right")

    for n in nodes:
        cc = "red" if n.cpu_percent >= _CPU_CRIT else ("yellow" if n.cpu_percent >= _CPU_WARN else "green")
        mc = "red" if n.memory_percent >= _MEM_CRIT else ("yellow" if n.memory_percent >= _MEM_WARN else "green")
        ntbl.add_row(
            n.name,
            n.cpu_usage,
            f"[{cc}]{n.cpu_percent}% {'✗' if n.cpu_percent >= _CPU_WARN else '✓'}[/{cc}]",
            n.memory_usage,
            f"[{mc}]{n.memory_percent}% {'✗' if n.memory_percent >= _MEM_WARN else '✓'}[/{mc}]",
        )
    console.print(ntbl)
    console.print()

    # At-risk pods
    at_risk = [p for p in pods if p.at_risk]
    if at_risk:
        ptbl = Table(title="PODS AT RISK", show_lines=False, header_style="bold yellow", border_style="dim")
        ptbl.add_column("Pod",       max_width=30, no_wrap=True)
        ptbl.add_column("Namespace", width=18)
        ptbl.add_column("Status",    width=28)

        for p in sorted(at_risk, key=lambda x: max(x.cpu_percent, x.memory_percent), reverse=True)[:15]:
            crit = p.memory_percent >= _MEM_CRIT or p.cpu_percent >= _CPU_CRIT
            col  = "bold red" if crit else "yellow"
            lbl  = "CRITICAL" if crit else "WARNING"
            parts = []
            if p.memory_percent > 0:
                parts.append(f"MEM {p.memory_percent}%")
            if p.cpu_percent > 0:
                parts.append(f"CPU {p.cpu_percent}%")
            if p.risk_type == "no_limits":
                parts = ["NO LIMITS"]
            ptbl.add_row(p.name, p.namespace, f"[{col}]{' '.join(parts)} ← {lbl}[/{col}]")

        console.print(ptbl)
    else:
        console.print("[green]All pods healthy.[/green]")

    console.print()
    slack_line = (
        "[green]Slack: ON[/green]" if slack_enabled
        else "[dim]Slack: off — set SLACK_WEBHOOK_URL in .env[/dim]"
    )
    console.print(f"  [dim]Ctrl+C to stop[/dim]  {slack_line}")
