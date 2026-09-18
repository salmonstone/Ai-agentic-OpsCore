"""
Topology skill — service dependency graph and blast-radius analysis.

Every diagnosis skill in this repo answers questions about ONE object: this
pod, this job, this certificate. None of them could answer "what else breaks
if this goes away", because nothing in the system knew that service A talks
to service B. This builds that graph.

Where the edges come from, and how much each can be trusted:

  ingress -> service   ROUTES   — declared in the Ingress spec. Authoritative.
  service -> workload  SELECTS  — label selector evaluated against real pod
                                  labels, then folded up to the owning
                                  controller. Authoritative.
  workload -> service  CALLS    — INFERRED from container env values that
                                  name a known Service. A heuristic, labelled
                                  as one everywhere it surfaces.

That last one is the honest weak point and is treated as such: an app that
builds URLs from a ConfigMap at runtime will be missing an edge, and a stale
env var pointing at a retired service will produce a phantom one. It is still
worth having, because it catches the common case for free — and when traces
arrive (Tier 3) they replace exactly this edge kind with measured truth.
"""
from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import (
    BlastRadius, TopologyEdge, TopologyEdgeKind, TopologyGraph, TopologyNode,
    TopologyNodeKind,
)
from agent.core.parsing import LLMParseError, parse_llm_json
from agent.integrations.kubectl import (
    get_all_ingresses, get_endpoints, get_pod_labels, get_services_detail,
    get_workload_service_refs, get_workloads,
)
from agent.memory.retrieval import remember
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SYSTEM = """You are an SRE assessing the blast radius of taking one component down.

The dependency facts below were derived from live cluster state. Treat them as
given — do not invent dependencies that are not listed.

Return JSON only with this exact structure:
{
  "explanation": "string — plain-language impact, one short paragraph",
  "user_facing": true or false,
  "severity": "critical" | "warning" | "info",
  "safe_to_disrupt": true or false,
  "precaution": "string — the one thing to check first, or empty"
}

Rules:
- user_facing is true ONLY if an ingress path reaches this component.
- Edges marked (inferred) come from environment variables and may be wrong.
  Never let an inferred edge alone justify "critical".
- If the component has no dependents at all, say so plainly and set severity
  to "info" — do not manufacture risk.
"""


def _node_id(kind: TopologyNodeKind, namespace: str, name: str) -> str:
    return f"{kind.value}/{namespace}/{name}"


def _selector_matches(selector: dict, labels: dict) -> bool:
    """Kubernetes label-selector semantics: every key/value in the selector
    must be present on the pod. An empty selector matches nothing here —
    Kubernetes treats it as 'match everything', but a Service with no
    selector is an externally-managed endpoint, not a fan-out to every pod
    in the namespace, and treating it as the latter would produce a graph
    that is technically defensible and operationally useless.
    """
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


class TopologySkill(BaseSkill):
    @property
    def name(self) -> str:
        return "topology"

    @property
    def description(self) -> str:
        return ("Build a service dependency graph from live cluster state and "
                "compute the blast radius of disrupting any component.")

    def execute(self, input_data: dict) -> dict:
        mode = input_data.get("mode", "build")
        if mode == "build":
            return self.build(input_data.get("namespace", "all")).model_dump()
        if mode == "blast":
            return self.blast_radius(
                input_data.get("kind", "workload"),
                input_data["name"],
                input_data.get("namespace", "default"),
            ).model_dump()
        raise ValueError(f"Unknown mode: {mode!r}")

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def build(self, namespace: str = "all") -> TopologyGraph:
        """Build the dependency graph for a namespace (or the whole cluster)."""
        log.info("topology.build.start", namespace=namespace)
        graph = TopologyGraph(
            namespace=namespace,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        services = get_services_detail(namespace)
        pods = get_pod_labels(namespace)
        endpoints = {(e["namespace"], e["name"]): e for e in get_endpoints(namespace)}

        workloads = []
        for kind in ("deployment", "statefulset", "daemonset"):
            workloads.extend(get_workloads(kind, namespace))

        if not services and not workloads:
            graph.warnings.append(
                "No services or workloads returned — cluster unreachable, empty "
                "namespace, or kubectl lacks list permission here."
            )
            return graph

        # --- workload nodes
        workload_ids: dict[tuple[str, str], str] = {}
        for w in workloads:
            nid = _node_id(TopologyNodeKind.WORKLOAD, w.namespace, w.name)
            workload_ids[(w.namespace, w.name)] = nid
            graph.nodes.append(TopologyNode(
                id=nid, kind=TopologyNodeKind.WORKLOAD, name=w.name,
                namespace=w.namespace, detail=w.kind.value,
                healthy=(w.ready >= w.desired and w.desired > 0),
                replicas=f"{w.ready}/{w.desired}",
            ))

        # --- service nodes + service -> workload edges
        service_ids: dict[tuple[str, str], str] = {}
        for svc in services:
            ns, name = svc["namespace"], svc["name"]
            sid = _node_id(TopologyNodeKind.SERVICE, ns, name)
            service_ids[(ns, name)] = sid
            ep = endpoints.get((ns, name), {})
            ready = int(ep.get("ready", 0) or 0)
            graph.nodes.append(TopologyNode(
                id=sid, kind=TopologyNodeKind.SERVICE, name=name, namespace=ns,
                detail=f"{svc['type']} {', '.join(svc['ports'])}".strip(),
                healthy=ready > 0 or not svc["selector"],
                replicas=f"{ready} endpoints",
            ))

            # Which controllers back this Service? Resolve through real pod
            # labels rather than guessing from naming conventions.
            owners: set[tuple[str, str]] = set()
            for pod in pods:
                if pod["namespace"] != ns:
                    continue
                if not _selector_matches(svc["selector"], pod["labels"]):
                    continue
                if pod["owner_name"]:
                    owners.add((pod["namespace"], pod["owner_name"]))

            for owner_ns, owner_name in owners:
                target = workload_ids.get((owner_ns, owner_name))
                if target is None:
                    continue
                graph.edges.append(TopologyEdge(
                    source=sid, target=target, kind=TopologyEdgeKind.SELECTS,
                    detail=f"selector {svc['selector']}",
                ))

            if svc["selector"] and ready == 0:
                graph.warnings.append(
                    f"Service {ns}/{name} has a selector but zero ready endpoints — "
                    f"anything routing to it is currently returning 503."
                )

        # --- ingress nodes + ingress -> service edges
        for ing in get_all_ingresses():
            if namespace != "all" and ing.namespace != namespace:
                continue
            iid = _node_id(TopologyNodeKind.INGRESS, ing.namespace, ing.name)
            if graph.node(iid) is None:
                graph.nodes.append(TopologyNode(
                    id=iid, kind=TopologyNodeKind.INGRESS, name=ing.name,
                    namespace=ing.namespace,
                    detail=f"{ing.domain}{' (TLS)' if ing.tls_enabled else ''}",
                    healthy=bool(ing.address),
                ))
            if not ing.backend_service:
                continue
            target = service_ids.get((ing.namespace, ing.backend_service))
            if target is None:
                graph.warnings.append(
                    f"Ingress {ing.namespace}/{ing.name} routes to service "
                    f"{ing.backend_service!r}, which does not exist."
                )
                continue
            graph.edges.append(TopologyEdge(
                source=iid, target=target, kind=TopologyEdgeKind.ROUTES,
                detail=ing.domain or "",
            ))

        # --- workload -> service edges, inferred from env values
        service_names = {(ns, n) for (ns, n) in service_ids}
        for ref in get_workload_service_refs(namespace):
            src = workload_ids.get((ref["namespace"], ref["name"]))
            if src is None:
                continue
            for value in ref["env_values"]:
                for (ns, svc_name) in service_names:
                    if not self._value_names_service(value, svc_name, ns, ref["namespace"]):
                        continue
                    target = service_ids[(ns, svc_name)]
                    if src == target:
                        continue
                    if any(e.source == src and e.target == target
                           and e.kind == TopologyEdgeKind.CALLS for e in graph.edges):
                        continue
                    graph.edges.append(TopologyEdge(
                        source=src, target=target, kind=TopologyEdgeKind.CALLS,
                        detail=f"(inferred) env value {value[:60]}",
                    ))

        log.info("topology.build.done", namespace=namespace,
                 nodes=len(graph.nodes), edges=len(graph.edges))
        return graph

    @staticmethod
    def _hosts_in(value: str) -> set[str]:
        """Extract hostname-shaped tokens from an env value.

        Parsing the host out is what makes the match exact. A substring test
        cannot tell `elasticsearch` from `elasticsearch-svc`, and against a
        real cluster it silently produced an edge to BOTH — the classic
        prefix false positive.
        """
        hosts: set[str] = set()
        for token in re.split(r"[\s,;]+", value.strip()):
            if not token:
                continue
            # strip scheme and any path/query, then the port
            token = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", token)
            token = token.split("/")[0].split("?")[0]
            token = token.rsplit(":", 1)[0] if ":" in token else token
            token = token.strip().strip(".").lower()
            if token and re.fullmatch(r"[a-z0-9._-]+", token):
                hosts.add(token)
        return hosts

    @classmethod
    def _value_names_service(cls, value: str, svc: str, svc_ns: str,
                             workload_ns: str) -> bool:
        """Does this env value reference that Service?

        Deliberately conservative, and exact on the hostname. Cross-namespace
        references require the fully-qualified form a pod would actually have
        to use anyway, so a same-named service in an unrelated namespace does
        not produce a phantom edge.
        """
        if len(svc) < 3:
            return False        # "db", "ui" — too short to match safely
        s = svc.lower()
        ns = svc_ns.lower()
        hosts = cls._hosts_in(value)
        if not hosts:
            return False

        qualified = {f"{s}.{ns}", f"{s}.{ns}.svc", f"{s}.{ns}.svc.cluster.local"}
        if hosts & qualified:
            return True
        # A bare service name only resolves within the same namespace.
        return svc_ns == workload_ns and s in hosts

    # ------------------------------------------------------------------
    # Blast radius
    # ------------------------------------------------------------------

    def blast_radius(self, kind: str, name: str, namespace: str = "default",
                     use_llm: bool = True) -> BlastRadius:
        """What else is affected if this component degrades?

        Walks the graph against the edge direction: edges point from consumer
        to provider, so the things that break are the ones pointing AT the
        target.
        """
        node_kind = {
            "workload": TopologyNodeKind.WORKLOAD,
            "deployment": TopologyNodeKind.WORKLOAD,
            "statefulset": TopologyNodeKind.WORKLOAD,
            "daemonset": TopologyNodeKind.WORKLOAD,
            "service": TopologyNodeKind.SERVICE,
            "ingress": TopologyNodeKind.INGRESS,
        }.get(kind.lower(), TopologyNodeKind.WORKLOAD)

        graph = self.build(namespace)
        target_id = _node_id(node_kind, namespace, name)

        if graph.node(target_id) is None:
            return BlastRadius(
                target=f"{namespace}/{name}", target_kind=node_kind.value,
                severity="info",
                explanation=(f"No {node_kind.value} named {name!r} found in namespace "
                             f"{namespace!r}. Nothing to analyse — check the name, or "
                             f"run `agent topology graph -n {namespace}` to see what exists."),
            )

        # BFS upstream through in-edges.
        direct = [e.source for e in graph.in_edges(target_id)]
        seen: set[str] = set(direct)
        queue = deque(direct)
        transitive: list[str] = []
        while queue:
            current = queue.popleft()
            for e in graph.in_edges(current):
                if e.source in seen:
                    continue
                seen.add(e.source)
                transitive.append(e.source)
                queue.append(e.source)

        ingress_paths = [
            n.detail or n.name for n in graph.nodes
            if n.kind == TopologyNodeKind.INGRESS and n.id in seen
        ]
        user_facing = bool(ingress_paths)
        total = len(seen)

        if user_facing:
            severity = "critical"
        elif total >= 3:
            severity = "warning"
        else:
            severity = "info"

        radius = BlastRadius(
            target=f"{namespace}/{name}", target_kind=node_kind.value,
            direct=direct, transitive=transitive, ingress_paths=ingress_paths,
            severity=severity, user_facing=user_facing,
        )
        radius.explanation = (
            self._narrate(radius, graph) if use_llm
            else self._fallback_explanation(radius)
        )

        remember(
            content=(f"Blast radius {namespace}/{name} ({node_kind.value}): "
                     f"{total} dependents, user_facing={user_facing}, severity={severity}"),
            source="topology",
            metadata={"target": name, "namespace": namespace,
                      "dependents": total, "user_facing": user_facing,
                      "severity": severity},
        )
        return radius

    def dependencies_of(self, kind: str, name: str, namespace: str = "default") -> list[str]:
        """What this component depends on — the other direction. Answers
        'what do I need to be healthy for this to work'."""
        node_kind = (TopologyNodeKind.SERVICE if kind.lower() == "service"
                     else TopologyNodeKind.WORKLOAD)
        graph = self.build(namespace)
        return [e.target for e in graph.out_edges(_node_id(node_kind, namespace, name))]

    # ------------------------------------------------------------------
    # Narrative
    # ------------------------------------------------------------------

    def _narrate(self, radius: BlastRadius, graph: TopologyGraph) -> str:
        lines = [
            f"Target: {radius.target} ({radius.target_kind})",
            f"Direct dependents ({len(radius.direct)}):",
            *[f"  - {d}" for d in radius.direct[:15]],
            f"Transitive dependents ({len(radius.transitive)}):",
            *[f"  - {d}" for d in radius.transitive[:15]],
            f"Ingress paths reaching it: {radius.ingress_paths or 'none'}",
            "",
            "Edges into the target:",
        ]
        target_id = _node_id(
            TopologyNodeKind.SERVICE if radius.target_kind == "service"
            else TopologyNodeKind.WORKLOAD,
            radius.target.split("/")[0], radius.target.split("/")[-1],
        )
        for e in graph.in_edges(target_id)[:15]:
            lines.append(f"  {e.source} --{e.kind.value}--> {e.target}  {e.detail}")

        try:
            response = run_sync(llm.chat(
                messages=[context.user_message("\n".join(lines))],
                system=_SYSTEM, json_mode=True, max_tokens=700,
            ))
            parsed = parse_llm_json(response.content)
        except (LLMParseError, Exception) as exc:
            log.warning("topology.narrate.failed", error=str(exc)[:200])
            return self._fallback_explanation(radius)

        # The model may soften severity; the graph is the authority on whether
        # an ingress path exists, so that finding is not up for negotiation.
        if radius.user_facing:
            parsed["severity"] = "critical"
        precaution = parsed.get("precaution", "")
        text = parsed.get("explanation", "")
        return f"{text}\n\nCheck first: {precaution}".strip() if precaution else text

    @staticmethod
    def _fallback_explanation(radius: BlastRadius) -> str:
        total = len(radius.direct) + len(radius.transitive)
        if total == 0:
            return (f"{radius.target} has no discovered dependents. Nothing else in "
                    f"this namespace routes to or references it.")
        facing = ("It IS on a user-facing path via "
                  f"{', '.join(radius.ingress_paths[:3])}. " if radius.user_facing
                  else "No ingress path reaches it, so impact is internal only. ")
        return (f"{total} component(s) depend on {radius.target} "
                f"({len(radius.direct)} directly). {facing}"
                f"Severity {radius.severity}.")
