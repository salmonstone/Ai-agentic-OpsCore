from __future__ import annotations

import asyncio

from agent.core import context, llm
from agent.core.models import ClusterOverview
from agent.integrations.kubectl import get_cluster_overview
from agent.memory.retrieval import remember
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SYSTEM = """You are a Kubernetes expert and SRE.
Analyze this cluster overview and provide:
1. Overall cluster health (healthy / warning / critical)
2. Any namespaces that need attention
3. Any patterns you notice (high restarts, stuck pods, resource pressure)
4. One-line summary of cluster state

Keep it concise and technical. Plain text only — no markdown, no JSON."""


class ClusterOverviewSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "cluster-overview"

    @property
    def description(self) -> str:
        return "Fetch a complete snapshot of the Kubernetes cluster and analyse it with Claude."

    def execute(self, input_data: dict) -> dict:
        overview = self.get_overview()
        return overview.model_dump()

    def get_overview(self) -> ClusterOverview:
        overview = get_cluster_overview()
        overview.analysis = self._analyse(overview)

        remember(
            content=(
                f"Cluster {overview.cluster_name}: "
                f"{overview.total_namespaces} namespaces, "
                f"{overview.total_pods} pods "
                f"({overview.healthy_pods} healthy, {overview.unhealthy_pods} unhealthy), "
                f"{overview.total_nodes} nodes. "
                f"Analysis: {overview.analysis}"
            ),
            source="cluster-overview",
            metadata={
                "cluster_name":    overview.cluster_name,
                "total_pods":      overview.total_pods,
                "unhealthy_pods":  overview.unhealthy_pods,
                "total_nodes":     overview.total_nodes,
                "generated_at":    overview.generated_at,
            },
        )

        return overview

    def _analyse(self, overview: ClusterOverview) -> str:
        # Build a compact summary for Claude — avoid sending all pod details
        ns_lines = []
        for ns in overview.namespaces:
            if ns.total_pods == 0:
                continue
            problems = []
            for pod in ns.pods:
                if pod.status != "Running":
                    problems.append(f"{pod.name} ({pod.status})")
                elif pod.restarts > 5:
                    problems.append(f"{pod.name} (restarts={pod.restarts})")
            problem_str = ", ".join(problems) if problems else "all healthy"
            ns_lines.append(
                f"  {ns.name}: {ns.total_pods} pods, "
                f"{ns.healthy_pods} healthy, {ns.unhealthy_pods} unhealthy — {problem_str}"
            )

        user_text = (
            f"Cluster   : {overview.cluster_name}\n"
            f"Nodes     : {overview.healthy_nodes}/{overview.total_nodes} healthy\n"
            f"Namespaces: {overview.total_namespaces}\n"
            f"Pods      : {overview.total_pods} total, "
            f"{overview.healthy_pods} healthy, {overview.unhealthy_pods} unhealthy\n\n"
            "--- NAMESPACE BREAKDOWN ---\n"
            + "\n".join(ns_lines)
        )

        try:
            response = asyncio.run(
                llm.chat(
                    messages=[context.user_message(user_text)],
                    system=_SYSTEM,
                    json_mode=False,
                    max_tokens=512,
                )
            )
            return response.content.strip()
        except Exception as exc:
            log.warning("cluster_overview.analysis.failed", error=str(exc))
            return "Analysis unavailable."
