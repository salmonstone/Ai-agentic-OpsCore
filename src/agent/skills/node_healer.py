"""
Node health and auto-remediation skill.

Scans every node for:
  - Memory pressure / high memory usage
  - Disk pressure / large files
  - PVC capacity issues
  - Top memory-consuming pods

Sends full context to Claude → gets a prioritised issue list → each fix
requires explicit user confirmation before being applied.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

from agent.core import context, llm
from agent.core.models import NodeHealthReport, NodeIssue
from agent.integrations.node_inspector import (
    get_node_conditions,
    get_node_metrics,
    get_pvc_status,
    get_top_pods,
)
from agent.memory.retrieval import remember
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SYSTEM = """You are a Kubernetes SRE and Linux systems expert.
Analyze the node health data and return a prioritised list of issues.

Return JSON only:
{
  "issues": [
    {
      "severity": "critical" | "warning" | "info",
      "issue_type": "<one of the types below>",
      "resource": "<node name, pod name, or pvc name>",
      "namespace": "<namespace or empty string>",
      "description": "clear explanation of the problem",
      "fix": "human-readable description of what should be done",
      "fix_command": "exact kubectl command to run, or null"
    }
  ],
  "analysis": "2-3 sentence overall summary of cluster node health"
}

Issue types:
- "MemoryPressure"      — node condition MemoryPressure=True
- "DiskPressure"        — node condition DiskPressure=True
- "PIDPressure"         — node condition PIDPressure=True
- "HighMemoryNode"      — node memory usage > 80%
- "HighCpuNode"         — node CPU usage > 80%
- "HighMemoryPod"       — a pod consuming > 80% of node memory
- "PvcAlmostFull"       — PVC at capacity (storage class may allow expansion)
- "PvcNotBound"         — PVC in Pending or Lost state
- "StaleDebugPod"       — leftover debug/inspector pod wasting resources

fix_command rules:
- MemoryPressure / HighMemoryPod: kubectl delete pod <pod> -n <ns>  (evict top consumer)
- DiskPressure: kubectl exec <pod> -n <ns> -- sh -c "find /var/log -name '*.log' -exec truncate -s 0 {} \\;"
- PvcAlmostFull: kubectl patch pvc <name> -n <ns> -p '{"spec":{"resources":{"requests":{"storage":"<2x current>"}}}}'
- PvcNotBound: kubectl describe pvc <name> -n <ns>   (investigation command)
- HighCpuNode: null  (CPU spikes usually self-resolve; eviction is disruptive)
- If no single command applies: null

Only include issues that actually exist in the data. Do not invent issues.
If everything is healthy, return an empty issues list."""


def _parse_json(content: str) -> dict:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text).strip()
    return json.loads(text)


class NodeHealerSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "node-healer"

    @property
    def description(self) -> str:
        return "Scan node memory, disk, and PVC health; diagnose with Claude; fix with permission."

    def execute(self, input_data: dict) -> dict:
        report = self.scan()
        return report.model_dump()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> NodeHealthReport:
        """Collect all node health data and run Claude analysis."""
        log.info("node_healer.scan.start")

        node_metrics  = get_node_metrics()
        node_conds    = get_node_conditions()
        pvcs          = get_pvc_status()
        top_pods      = get_top_pods()

        report = NodeHealthReport(
            generated_at = datetime.now(timezone.utc).isoformat(),
            node_metrics = node_metrics,
            pvcs         = pvcs,
            top_pods     = top_pods,
        )

        # Build context text for Claude
        context_text = self._build_context(node_metrics, node_conds, pvcs, top_pods)

        try:
            response = asyncio.run(
                llm.chat(
                    messages=[context.user_message(context_text)],
                    system=_SYSTEM,
                    json_mode=True,
                    max_tokens=2048,
                )
            )
            parsed = _parse_json(response.content)

            report.issues = [
                NodeIssue(**issue)
                for issue in parsed.get("issues", [])
            ]
            report.analysis = parsed.get("analysis", "")

        except Exception as exc:
            log.error("node_healer.scan.llm_error", error=str(exc))
            report.analysis = "Analysis unavailable."

        # Save to memory
        remember(
            content=(
                f"Node health scan: {len(report.node_metrics)} nodes, "
                f"{len(report.pvcs)} PVCs, "
                f"{len(report.issues)} issue(s) found. "
                f"{report.analysis}"
            ),
            source="node-healer",
            metadata={
                "issue_count": len(report.issues),
                "critical":    sum(1 for i in report.issues if i.severity == "critical"),
                "warning":     sum(1 for i in report.issues if i.severity == "warning"),
            },
        )

        log.info("node_healer.scan.done", issues=len(report.issues))
        return report

    # ------------------------------------------------------------------
    # Context builder
    # ------------------------------------------------------------------

    def _build_context(self, node_metrics, node_conds, pvcs, top_pods) -> str:
        lines: list[str] = ["=== NODE METRICS ==="]

        if node_metrics:
            for nm in node_metrics:
                lines.append(
                    f"  {nm.name}: CPU {nm.cpu_cores} ({nm.cpu_percent:.0f}%)  "
                    f"Memory {nm.memory_bytes} ({nm.memory_percent:.0f}%)"
                )
        else:
            lines.append("  (metrics-server not available — using conditions only)")

        lines.append("\n=== NODE CONDITIONS ===")
        for node_name, conds in node_conds.items():
            problem_conds = {
                k: v for k, v in conds.items()
                if not k.startswith("_") and k != "Ready" and v.get("status") == "True"
            }
            ready = conds.get("Ready", {}).get("status", "?")
            alloc = conds.get("_allocatable", {})
            lines.append(
                f"  {node_name}: Ready={ready}  "
                f"CPU={alloc.get('cpu','?')}  Memory={alloc.get('memory','?')}"
            )
            if problem_conds:
                for cond, info in problem_conds.items():
                    lines.append(f"    ⚠ {cond}: {info.get('message', info.get('reason', ''))}")

        lines.append("\n=== PVC STATUS ===")
        if pvcs:
            for pvc in pvcs:
                lines.append(
                    f"  {pvc.namespace}/{pvc.name}: {pvc.status}  "
                    f"capacity={pvc.capacity}  class={pvc.storage_class}"
                )
        else:
            lines.append("  (no PVCs found)")

        lines.append("\n=== TOP PODS BY MEMORY ===")
        if top_pods:
            for pod in top_pods[:15]:
                lines.append(
                    f"  {pod.get('namespace','?')}/{pod.get('name','?')}: "
                    f"CPU={pod.get('cpu','?')}  Memory={pod.get('memory','?')}"
                )
        else:
            lines.append("  (metrics-server not available)")

        return "\n".join(lines)
