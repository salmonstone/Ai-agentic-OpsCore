"""
Multi-Cluster Management Skill — manage multiple kubeconfig contexts.

Supports:
  list     — all contexts with health/environment detection
  switch   — change active context with prod warnings
  compare  — diff two clusters side by side via Claude
  add EKS  — aws eks update-kubeconfig wrapper
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import ClusterContext
from agent.integrations.kubectl import (
    add_eks_cluster,
    get_all_contexts,
    get_cluster_summary,
    get_context_node_count,
    get_current_context,
    rename_context,
    switch_context,
)
from agent.memory.retrieval import remember
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_COMPARE_SYSTEM = """You are a Kubernetes expert.
Compare two clusters and identify meaningful differences.
Focus on: version skew, workload imbalances, missing namespaces,
unhealthy pod counts, and configuration drift.
Be specific and actionable. Write 4-6 sentences."""


class MultiClusterSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "multi-cluster"

    @property
    def description(self) -> str:
        return "Manage multiple Kubernetes clusters — list, switch, compare, and add EKS clusters."

    def execute(self, input_data: dict) -> dict:
        mode = input_data.get("mode", "list")
        if mode == "list":
            return {"clusters": [c.model_dump() for c in self.list_clusters()]}
        if mode == "switch":
            ctx = self.switch_cluster(input_data["name"])
            return ctx.model_dump() if ctx else {}
        raise ValueError(f"Unknown mode: {mode!r}")

    # ------------------------------------------------------------------

    def list_clusters(self, check_health: bool = True) -> list[ClusterContext]:
        """Return all configured contexts with optional health check."""
        contexts = get_all_contexts()
        if not contexts:
            return []

        if check_health:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    pool.submit(get_context_node_count, ctx.name): ctx
                    for ctx in contexts
                }
                for future in as_completed(futures):
                    ctx = futures[future]
                    try:
                        count = future.result(timeout=12)
                        if count >= 0:
                            ctx.health     = "healthy"
                            ctx.node_count = count
                        else:
                            ctx.health = "unreachable"
                    except Exception:
                        ctx.health = "unreachable"

        remember(
            content=(
                f"Multi-cluster list: {len(contexts)} contexts — "
                + ", ".join(
                    f"{c.name} ({c.environment}, {c.health})"
                    for c in contexts
                )
            ),
            source="multi-cluster",
            metadata={"count": len(contexts)},
        )

        log.info("multicluster.list_done", count=len(contexts))
        return contexts

    def switch_cluster(self, name_or_env: str) -> ClusterContext | None:
        """Switch to a context by name or by environment keyword (prod/staging/dev)."""
        contexts   = get_all_contexts()
        name_lower = name_or_env.lower()

        # Exact match first, then environment keyword match
        target = next((c for c in contexts if c.name == name_or_env), None)
        if not target:
            target = next(
                (c for c in contexts if name_lower in c.environment.lower()
                 or name_lower in c.name.lower()),
                None,
            )

        if not target:
            log.warning("multicluster.switch_not_found", query=name_or_env)
            return None

        success = switch_context(target.name)
        if success:
            target.is_current = True
            target.node_count = get_context_node_count(target.name)
            if target.node_count >= 0:
                target.health = "healthy"

            remember(
                content=f"Switched cluster context to {target.name} ({target.environment})",
                source="multi-cluster-switch",
                metadata={"context": target.name, "environment": target.environment},
            )

        return target if success else None

    def compare_clusters(self, ctx1_name: str, ctx2_name: str) -> dict:
        """Get summary stats for two clusters and ask Claude to analyse differences."""
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(get_cluster_summary, ctx1_name)
            f2 = pool.submit(get_cluster_summary, ctx2_name)
            s1 = f1.result(timeout=20)
            s2 = f2.result(timeout=20)

        prompt = (
            f"Cluster A ({ctx1_name}):\n"
            f"  Pods: {s1['pod_count']}  Namespaces: {s1['namespace_count']}\n"
            f"  K8s version: {s1['k8s_version']}  Reachable: {s1['reachable']}\n\n"
            f"Cluster B ({ctx2_name}):\n"
            f"  Pods: {s2['pod_count']}  Namespaces: {s2['namespace_count']}\n"
            f"  K8s version: {s2['k8s_version']}  Reachable: {s2['reachable']}\n"
        )

        analysis = ""
        try:
            resp = run_sync(llm.chat(
                messages=[context.user_message(prompt)],
                system=_COMPARE_SYSTEM,
                max_tokens=400,
            ))
            analysis = resp.content.strip()
        except Exception as exc:
            log.warning("multicluster.compare_ai_failed", error=str(exc))
            analysis = "AI analysis unavailable."

        return {
            "ctx1": {"name": ctx1_name, **s1},
            "ctx2": {"name": ctx2_name, **s2},
            "analysis": analysis,
        }

    def add_eks(self, cluster_name: str, region: str, profile: str = "default",
                rename_to: str = "") -> dict:
        """Add an EKS cluster to kubeconfig and optionally rename the context."""
        success = add_eks_cluster(cluster_name, region, profile)
        if not success:
            return {"success": False, "error": "aws eks update-kubeconfig failed"}

        # The context name AWS creates is the full ARN
        contexts   = get_all_contexts()
        new_ctx    = next(
            (c for c in contexts if cluster_name in c.name and not c.is_current),
            None,
        )
        # Fallback: find most recently added (current after add)
        if not new_ctx:
            new_ctx = next((c for c in contexts if cluster_name in c.name), None)

        final_name = new_ctx.name if new_ctx else cluster_name
        if rename_to and new_ctx:
            if rename_context(new_ctx.name, rename_to):
                final_name = rename_to

        node_count = get_context_node_count(final_name)

        remember(
            content=f"Added EKS cluster {cluster_name} ({region}) as context '{final_name}' — {node_count} nodes",
            source="multi-cluster-add",
            metadata={"cluster": cluster_name, "region": region, "context": final_name},
        )

        return {
            "success":    True,
            "context":    final_name,
            "node_count": node_count,
            "region":     region,
        }

    def run_readonly_on_all(self, kubectl_args: str, contexts: list[str]) -> dict[str, str]:
        """Run a read-only kubectl command across multiple clusters."""
        # Safety: only allow read-only verbs
        _ALLOWED = {"get", "describe", "top", "logs", "explain", "version", "cluster-info"}
        first_word = kubectl_args.strip().split()[0].lower()
        if first_word not in _ALLOWED:
            raise PermissionError(
                f"'{first_word}' is not allowed in broadcast — only: {', '.join(sorted(_ALLOWED))}"
            )

        def _run(ctx_name: str) -> tuple[str, str]:
            import subprocess
            result = subprocess.run(
                ["kubectl"] + kubectl_args.split() + [f"--context={ctx_name}"],
                capture_output=True, text=True, timeout=15,
            )
            return ctx_name, result.stdout if result.returncode == 0 else f"ERROR: {result.stderr[:200]}"

        results: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for ctx_name, output in pool.map(lambda c: _run(c), contexts):
                results[ctx_name] = output

        return results
