"""
AtlasOS MCP server.

Exposes a subset of the exact same skill methods `cli.py` already calls, as
MCP tools an MCP client (Claude Desktop, Claude Code, ...) can invoke. This
file adds zero business logic of its own — every tool is a thin wrapper that
instantiates a skill, calls its real method, and returns the typed result as
JSON (`.model_dump()`). `cli.py` and `skills/*.py` are completely untouched.

Every tool body runs via `asyncio.to_thread(...)`, not as a direct call on
the MCP server's own event loop. This matters: every skill's Claude call is
written as `asyncio.run(llm.chat(...))`, which is correct when called from
the plain-synchronous CLI, but raises "asyncio.run() cannot be called from a
running event loop" if invoked directly on the async loop the MCP stdio
server itself runs on. A worker thread has no event loop of its own, so the
skill's nested `asyncio.run()` works cleanly there, fully isolated from the
server's loop.

Only read-only operations are exposed for most tools. Anything that mutates
real infrastructure requires an explicit confirm=True argument and is scoped
to a single named resource — never a bulk/cluster-wide operation. See
ARCHITECTURE2.md section 6 for the full tiering rationale.

Run (stdio transport, for a local MCP client):
    uv run python -m agent.mcp_server

Run (HTTP transport, for a remote MCP client such as ChatGPT via a tunnel):
    MCP_TRANSPORT=http MCP_AUTH_TOKEN=<secret> uv run python -m agent.mcp_server

HTTP mode refuses to start without MCP_AUTH_TOKEN, and defaults to readonly
(MCP_READONLY=1) so that a remote model cannot reach a mutating tool at all —
those tools are never registered, so they do not appear in tools/list and
cannot be called regardless of what the client asks for. stdio mode keeps the
full tool set, since that is the local operator's own session.
"""
from __future__ import annotations

import asyncio
import hmac
import os
from typing import Any
from urllib.parse import parse_qs

from mcp.server.fastmcp import FastMCP

_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()

# Public hostname(s) this server is reached by, e.g. an ngrok domain. MCP's
# streamable-http transport validates the Host header to block DNS-rebinding
# attacks, and rejects anything not on this list with 421 Misdirected Request
# — so a tunnelled request fails until its hostname is named here. Hostname
# wildcards are NOT supported upstream (only ":*" port wildcards), so each
# public host must be listed exactly. Comma-separated.
_ALLOWED_HOSTS = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]


def _transport_security():
    """Extend Host-header validation to the configured public hostnames.

    Returns None when none are configured, which leaves the library default
    (localhost only) untouched — this must stay a widening of the allowlist,
    never a disabling of the protection.
    """
    if not _ALLOWED_HOSTS:
        return None
    from mcp.server.transport_security import TransportSecuritySettings

    local = ["localhost", "127.0.0.1", "localhost:*", "127.0.0.1:*"]
    return TransportSecuritySettings(
        allowed_hosts=local + _ALLOWED_HOSTS,
        allowed_origins=[f"https://{h}" for h in _ALLOWED_HOSTS]
                        + [f"http://{h}" for h in _ALLOWED_HOSTS]
                        + [f"http://{h}" for h in local],
    )


mcp = FastMCP("atlasos", transport_security=_transport_security())

# Readonly defaults to ON for any networked transport and OFF for stdio: a
# remote client is untrusted by default, the local operator is not. Either
# way an explicit MCP_READONLY=0/1 wins.
_READONLY = os.getenv("MCP_READONLY", "0" if _TRANSPORT == "stdio" else "1").strip() == "1"


def _mutating_tool():
    """Register a tool that has real side effects, EXCEPT in readonly mode.

    "Side effects" here is broader than the confirm=True tier: it also covers
    tools that look read-only but aren't. dns_scan creates and deletes a probe
    pod in the cluster, security_drift writes snapshot records into the memory
    store, and incident_correlate opens incidents and can fire Slack alerts.
    Exposing those to an untrusted remote model would let it create workloads
    and page a human, so they are withheld alongside the *_apply_fix tools.

    In readonly mode the function is returned unregistered — it never reaches
    the MCP tool registry, so it is absent from tools/list rather than merely
    refusing to run.
    """
    def decorator(fn):
        if _READONLY:
            return fn
        return mcp.tool()(fn)
    return decorator


@mcp.tool()
async def jenkins_scan() -> dict[str, Any]:
    """Scan all Jenkins jobs, agents, and the build queue; returns health score,
    failing jobs, offline agents, stuck queue items, and an AI diagnosis for
    each failure. Read-only — makes no changes to Jenkins."""
    def _run():
        from agent.skills.jenkins import JenkinsSkill
        return JenkinsSkill().scan().model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def jenkins_list_jobs(folder: str | None = None) -> list[dict]:
    """List every Jenkins job, regardless of pass/fail status — unlike
    jenkins_scan, which only surfaces failing ones. Read-only.

    folder: list only jobs inside this folder, or omit for everything.
    """
    def _run():
        # Calls integrations/jenkins.py directly, NOT JenkinsSkill — listing
        # jobs needs no AI/memory, and importing the skill class drags in
        # chromadb/embeddings (~4.2s cold) for zero benefit here (~0.9s
        # importing the raw client directly). Same fix applied to the CLI's
        # `agent jenkins jobs` command.
        from agent.integrations import jenkins as jk
        return [j.model_dump() for j in jk.get_all_jobs(folder)]
    return await asyncio.to_thread(_run)


@_mutating_tool()
async def jenkins_trigger_build(job_name: str, confirm: bool = False,
                                 confirm_destructive_name: bool = False) -> dict[str, Any]:
    """Directly trigger a build for a named Jenkins job — no diagnosis, just
    runs it. This is a deliberate action the caller explicitly asked for by
    naming the job, not an autonomous fix, so it isn't gated by the
    problem/confidence-based safety policy the way jenkins_apply_fix is —
    but it still requires confirm=True before anything actually runs.

    If the job NAME itself suggests destructive intent (contains "destroy",
    "teardown", "nuke", "purge", "wipe", "decommission"), confirm=True is
    NOT sufficient on its own — confirm_destructive_name must ALSO be
    explicitly True. This is deliberate: a single "yes, trigger it" from a
    natural-language conversation must never be enough to run something
    named like this. Surface the destructive-name warning to the human and
    get a distinct, explicit second acknowledgment before ever setting
    confirm_destructive_name=True.

    job_name: exact Jenkins job name, e.g. "backend-api/main".
    confirm: must be explicitly True, or nothing is triggered.
    confirm_destructive_name: required IN ADDITION to confirm when the job
        name matches a destructive-sounding pattern.
    """
    def _run():
        from agent.core.safety import name_suggests_destructive
        from agent.integrations import jenkins as jk

        token = name_suggests_destructive(job_name)
        if token and not confirm_destructive_name:
            return {
                "triggered": False,
                "message": (
                    f"'{job_name}' looks destructive (matched '{token}'). confirm=True alone "
                    "is not enough for a job whose name suggests destructive intent — "
                    "confirm_destructive_name=True is also required. Get an explicit, distinct "
                    "acknowledgment from the human before setting it, not just their original 'yes'."
                ),
            }
        if not confirm:
            return {"triggered": False, "message": "Set confirm=True to actually trigger this build."}

        jobs = {j.name: j for j in jk.get_all_jobs()}
        if job_name not in jobs:
            raise ValueError(f"No such Jenkins job: '{job_name}'.")
        queue_id = jk.retrigger_build(job_name)
        return {"triggered": True, "queue_id": queue_id}
    return await asyncio.to_thread(_run)


@mcp.tool()
async def jenkins_diagnose(job_name: str, build_number: int | None = None) -> dict[str, Any]:
    """Deep-dive AI diagnosis of one Jenkins job's failure (root cause,
    confidence, suggested fix). Read-only — does not apply any fix.

    job_name: Jenkins job name, e.g. "backend-api/main".
    build_number: specific build to diagnose; defaults to the job's last build.
    """
    def _run():
        from agent.integrations import jenkins as jk
        from agent.memory.retrieval import retrieve_context
        from agent.skills.jenkins import JenkinsSkill

        jobs = {j.name: j for j in jk.get_all_jobs()}
        job = jobs.get(job_name)
        bn = build_number
        if bn is None:
            if not job or job.last_build_number is None:
                raise ValueError(f"No build history found for '{job_name}'.")
            bn = job.last_build_number

        build_info = jk.get_build_info(job_name, bn)
        log_text = jk.get_console_log(job_name, bn)
        history = jk.get_build_history(job_name, count=5)
        past = retrieve_context(f"jenkins {job_name} failure")

        diagnosis = JenkinsSkill().diagnose(job_name, build_info, log_text, history, past)
        return diagnosis.model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def jenkins_auth_status() -> dict[str, Any]:
    """Verify the Jenkins connection and return version/executor/agent info."""
    def _run():
        from agent.integrations import jenkins as jk
        return jk.get_connection_info().model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def k8s_scan(namespace: str = "all") -> list[dict]:
    """Full Kubernetes cluster health scan across pods, nodes, and resources
    for the given namespace ("all" scans every namespace). Read-only.

    namespace: Kubernetes namespace to scan, or "all" for every namespace.
    """
    def _run():
        from agent.skills.k8s import K8sSkill
        return K8sSkill().full_cluster_scan(namespace)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def k8s_list_pods(namespace: str = "all", status_filter: str | None = None) -> list[dict]:
    """List every pod in the cluster (or one namespace), regardless of health —
    unlike k8s_scan, which only surfaces problem pods. Read-only.

    namespace: Kubernetes namespace to list, or "all" for every namespace.
    status_filter: optional exact status to filter by (e.g. "Running", "Pending",
        "CrashLoopBackOff"). Omit to return every pod.
    """
    def _run():
        from agent.integrations.kubectl import get_pods

        pods = get_pods(namespace)
        if status_filter:
            pods = [p for p in pods if p.status.lower() == status_filter.lower()]
        return [p.model_dump() for p in pods]
    return await asyncio.to_thread(_run)


@mcp.tool()
async def k8s_diagnose(pod_name: str, namespace: str) -> dict[str, Any]:
    """Deep AI diagnosis of one specific pod's problem (root cause, suggested
    fix). Read-only — does not apply any fix.

    pod_name: exact pod name.
    namespace: namespace the pod lives in.
    """
    def _run():
        from agent.core.models import PodInfo
        from agent.integrations.kubectl import get_pods
        from agent.skills.k8s import K8sSkill

        all_pods = get_pods(namespace)
        pod_info = next((p for p in all_pods if p.name == pod_name), None)
        if pod_info is None:
            pod_info = PodInfo(
                name=pod_name, namespace=namespace, status="Unknown",
                ready="0/1", restarts=0, age="?", node="<none>",
            )

        diagnosis = K8sSkill().diagnose_pod(pod_info)
        return diagnosis.model_dump()
    return await asyncio.to_thread(_run)


@_mutating_tool()
async def k8s_apply_fix(pod_name: str, namespace: str, confirm: bool = False) -> dict[str, Any]:
    """Diagnose one specific pod and apply the suggested kubectl fix — scoped
    to exactly this one pod, never a bulk/cluster-wide operation.

    pod_name: exact pod name.
    namespace: namespace the pod lives in.
    confirm: must be explicitly True, or nothing is applied — the diagnosis
        and proposed fix are returned instead so the caller can review first.
    """
    def _run():
        from agent.core.models import PodInfo
        from agent.integrations.kubectl import get_pods
        from agent.skills.k8s import K8sSkill

        skill = K8sSkill()
        all_pods = get_pods(namespace)
        pod_info = next((p for p in all_pods if p.name == pod_name), None)
        if pod_info is None:
            pod_info = PodInfo(
                name=pod_name, namespace=namespace, status="Unknown",
                ready="0/1", restarts=0, age="?", node="<none>",
            )

        diagnosis = skill.diagnose_pod(pod_info)

        if not diagnosis.fix_command:
            return {"applied": False, "message": "No automated fix command available for this issue.",
                    "diagnosis": diagnosis.model_dump()}

        if not confirm:
            return {"applied": False, "message": "Set confirm=True to actually apply this fix.",
                    "diagnosis": diagnosis.model_dump()}

        success = skill.apply_fix(diagnosis)
        return {"applied": success, "fix_command": diagnosis.fix_command, "diagnosis": diagnosis.model_dump()}
    return await asyncio.to_thread(_run)


@mcp.tool()
async def tls_scan() -> dict[str, Any]:
    """Audit TLS certificates across every Ingress in the cluster — expiry,
    cert-manager status, ACME challenges, secret validity. Read-only."""
    def _run():
        from agent.skills.tls import TLSSkill
        return TLSSkill().scan().model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def ingress_scan() -> dict[str, Any]:
    """Diagnose nginx ingress, external-IP, and routing problems across the
    cluster. Read-only — does not apply any fix."""
    def _run():
        from agent.skills.ingress import IngressSkill
        return IngressSkill().scan().model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def security_audit(namespace: str = "all") -> dict[str, Any]:
    """Run a Kubernetes security audit (RBAC, pod security, network policies,
    exposed secrets) for the given namespace. Read-only.

    namespace: Kubernetes namespace to audit, or "all" for every namespace.
    """
    def _run():
        from agent.skills.security import SecurityAuditSkill
        return SecurityAuditSkill().run_audit(namespace).model_dump()
    return await asyncio.to_thread(_run)


@_mutating_tool()
async def security_drift(namespace: str = "all") -> dict[str, Any]:
    """Security audit that only reports what's NEW since the last scan of
    this cluster (new findings, resolved count, unchanged count) — instead
    of repeating every finding every time. Read-only. First call on a
    cluster has no baseline yet; every call becomes the baseline for next.

    namespace: Kubernetes namespace to audit, or "all" for every namespace.
    """
    def _run():
        from agent.skills.security import SecurityAuditSkill
        return SecurityAuditSkill().detect_drift(namespace).model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def cost_analyze(days: int = 30) -> dict[str, Any]:
    """Full AWS cost optimization analysis — spend by service, waste detected,
    savings opportunities, spend anomalies, over a trailing window. Read-only.

    days: how many trailing days of AWS Cost Explorer data to analyze.
    """
    def _run():
        from agent.skills.cost import CostAnalysisSkill
        return CostAnalysisSkill().full_aws_analysis(days).model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def aws_scan(region: str = "", services: str = "ec2,rds,alb") -> dict[str, Any]:
    """Scan the AWS account for unhealthy EC2, RDS, and ALB resources —
    each finding includes an AI root-cause diagnosis. Read-only.

    region: AWS region to scan; defaults to your configured AWS_REGION.
    services: comma-separated subset to check — ec2, rds, alb.
    """
    def _run():
        from agent.config import settings
        from agent.skills.aws import AwsSkill

        svc_list = [s.strip().lower() for s in services.split(",") if s.strip()]
        diagnoses = AwsSkill().scan_account(region or settings.aws_region, svc_list)
        return {"count": len(diagnoses), "diagnoses": [d.model_dump() for d in diagnoses]}
    return await asyncio.to_thread(_run)


@mcp.tool()
async def aws_auth_status() -> dict[str, Any]:
    """Verify the configured AWS auth method (IAM role / access key / SSO
    profile) currently works, and return the resolved identity. Read-only."""
    def _run():
        from agent.config import settings
        from agent.integrations.aws import get_aws_client

        method = settings.aws_auth_method
        try:
            identity = get_aws_client("sts").get_caller_identity()
            return {
                "method": method,
                "connected": True,
                "arn": identity.get("Arn", ""),
                "account": identity.get("Account", ""),
            }
        except Exception as exc:
            return {"method": method, "connected": False, "error": str(exc)}
    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Tier 2 — mutating tools. Every one requires confirm=True from the caller;
# without it, they return the diagnosis/fix that WOULD run and do nothing.
# ---------------------------------------------------------------------------

@_mutating_tool()
async def jenkins_apply_fix(job_name: str, build_number: int | None = None, confirm: bool = False) -> dict[str, Any]:
    """Diagnose a Jenkins job's failure and apply the suggested fix (retrigger,
    restart agent, clear workspace, or cancel+retrigger). Does NOT wait for the
    fix to be verified — call jenkins_scan again shortly after to check whether
    the job actually recovered.

    job_name: Jenkins job name, e.g. "backend-api/main".
    build_number: specific build to diagnose; defaults to the job's last build.
    confirm: must be explicitly True, or nothing is applied — the diagnosis
        and proposed fix are returned instead so the caller can review first.
    """
    def _run():
        from agent.integrations import jenkins as jk
        from agent.memory.retrieval import retrieve_context
        from agent.skills.jenkins import JenkinsSkill

        skill = JenkinsSkill()
        jobs = {j.name: j for j in jk.get_all_jobs()}
        job = jobs.get(job_name)
        bn = build_number
        if bn is None:
            if not job or job.last_build_number is None:
                raise ValueError(f"No build history found for '{job_name}'.")
            bn = job.last_build_number

        build_info = jk.get_build_info(job_name, bn)
        log_text = jk.get_console_log(job_name, bn)
        history = jk.get_build_history(job_name, count=5)
        past = retrieve_context(f"jenkins {job_name} failure")
        diagnosis = skill.diagnose(job_name, build_info, log_text, history, past)

        if diagnosis.fix_action.value == "MANUAL_ONLY":
            return {"applied": False, "message": "This issue requires manual intervention.",
                    "diagnosis": diagnosis.model_dump()}

        if not confirm:
            return {"applied": False, "message": "Set confirm=True to actually apply this fix.",
                    "diagnosis": diagnosis.model_dump()}

        fix_result = skill.apply_fix(diagnosis, confirmed=True)
        return {"applied": fix_result.success, "fix_result": fix_result.model_dump(),
                "diagnosis": diagnosis.model_dump()}
    return await asyncio.to_thread(_run)


@_mutating_tool()
async def ingress_apply_fix(name: str, namespace: str = "default", confirm: bool = False) -> dict[str, Any]:
    """Diagnose an Ingress resource's problem and apply the suggested kubectl
    fix.

    name: Ingress resource name.
    namespace: namespace the Ingress lives in.
    confirm: must be explicitly True, or nothing is applied.
    """
    def _run():
        from agent.skills.ingress import IngressSkill

        skill = IngressSkill()
        diagnosis = skill.diagnose(name, namespace)

        if not diagnosis.fix_command:
            return {"applied": False, "message": "No automated fix available for this problem.",
                    "diagnosis": diagnosis.model_dump()}

        if not confirm:
            return {"applied": False, "message": "Set confirm=True to actually apply this fix.",
                    "diagnosis": diagnosis.model_dump()}

        ok = skill.apply_fix(diagnosis.fix_command, name, namespace)
        return {"applied": ok, "fix_command": diagnosis.fix_command, "diagnosis": diagnosis.model_dump()}
    return await asyncio.to_thread(_run)


@_mutating_tool()
async def tls_apply_fix(name: str, namespace: str, kind: str = "certificate", confirm: bool = False) -> dict[str, Any]:
    """Diagnose a TLS certificate/issuer problem and apply the suggested
    kubectl fix.

    name: name of the Certificate (or other TLS-related) resource.
    namespace: namespace the resource lives in.
    kind: resource kind being diagnosed, defaults to "certificate".
    confirm: must be explicitly True, or nothing is applied.
    """
    def _run():
        from agent.skills.tls import TLSSkill

        skill = TLSSkill()
        diagnosis = skill.diagnose(name, namespace, kind)

        if not diagnosis.fix_command:
            return {"applied": False, "message": "No automated fix available for this problem.",
                    "diagnosis": diagnosis.model_dump()}

        if not confirm:
            return {"applied": False, "message": "Set confirm=True to actually apply this fix.",
                    "diagnosis": diagnosis.model_dump()}

        ok = skill.apply_fix(diagnosis.fix_command, name, namespace)
        return {"applied": ok, "fix_command": diagnosis.fix_command, "diagnosis": diagnosis.model_dump()}
    return await asyncio.to_thread(_run)


@_mutating_tool()
async def aws_apply_fix(resource_id: str, resource_type: str, region: str = "", confirm: bool = False) -> dict[str, Any]:
    """Diagnose one specific AWS resource and apply the suggested fix —
    scoped to exactly this one resource, never account-wide.

    resource_id: instance ID, DB identifier, or target group ARN.
    resource_type: "ec2" | "rds" | "alb".
    region: AWS region; defaults to your configured AWS_REGION.
    confirm: must be explicitly True, or nothing is applied — the diagnosis
        and proposed fix are returned instead so the caller can review first.
    """
    def _run():
        from agent.config import settings
        from agent.core.models import AwsResource, AwsResourceType
        from agent.skills.aws import AwsSkill

        type_map = {"ec2": AwsResourceType.EC2, "rds": AwsResourceType.RDS, "alb": AwsResourceType.ALB}
        rtype = type_map.get(resource_type.lower())
        if rtype is None:
            return {"applied": False, "message": f"Unknown resource type: {resource_type!r}. Use: ec2, rds, alb"}

        skill = AwsSkill()
        resource = AwsResource(
            id=resource_id, name=resource_id, resource_type=rtype,
            status="unknown", region=region or settings.aws_region,
        )
        diagnosis = skill.diagnose_resource(resource)

        if not diagnosis.fix_command:
            return {"applied": False, "message": "No automated fix command available for this issue.",
                    "diagnosis": diagnosis.model_dump()}

        if not confirm:
            return {"applied": False, "message": "Set confirm=True to actually apply this fix.",
                    "diagnosis": diagnosis.model_dump()}

        success = skill.apply_fix(diagnosis)
        return {"applied": success, "diagnosis": diagnosis.model_dump()}
    return await asyncio.to_thread(_run)


@_mutating_tool()
async def cost_apply_fix(fix_id: str, days: int = 30, confirm: bool = False) -> dict[str, Any]:
    """Apply one specific AWS cost-saving fix by ID (from a prior cost_analyze
    call's "savings_plan" list) — e.g. an EBS gp2->gp3 upgrade or a CloudWatch
    log retention policy. Only ever applies auto-fixable fixes.

    fix_id: the "id" field of the fix to apply, from cost_analyze's output.
    days: lookback window to re-run the analysis with, must match how the
        fix was originally found.
    confirm: must be explicitly True, or nothing is applied.
    """
    def _run():
        from agent.skills.cost import CostAnalysisSkill

        skill = CostAnalysisSkill()
        analysis = skill.full_aws_analysis(days)
        matches = [f for f in analysis.savings_plan if f.id == fix_id]
        if not matches:
            return {"applied": False, "message": f"No fix found with id '{fix_id}'."}
        fix = matches[0]

        if not fix.auto_fixable:
            return {"applied": False, "message": "This fix requires manual action.", "fix": fix.model_dump()}

        if not confirm:
            return {"applied": False, "message": "Set confirm=True to actually apply this fix.",
                    "fix": fix.model_dump()}

        ok = skill.apply_cost_fix(fix)
        return {"applied": ok, "fix": fix.model_dump()}
    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Tier 1 additions — memory, DNS/domain, deploy status, eval suites, project status
# ---------------------------------------------------------------------------

@mcp.tool()
async def memory_search(query: str, limit: int = 5) -> list[dict]:
    """Semantic search over AtlasOS's stored incident memory (SQLite + vector
    store) — past scans, diagnoses, and fixes across every skill. Read-only.

    query: natural-language search, e.g. "jenkins flaky test" or "aws cost waste".
    limit: max results to return.
    """
    def _run():
        from agent.memory.retrieval import retrieve_context
        return [m.model_dump() for m in retrieve_context(query, limit=limit)]
    return await asyncio.to_thread(_run)


@_mutating_tool()
async def dns_scan() -> dict[str, Any]:
    """Full DNS health audit — CoreDNS pods, config, resolution tests,
    external-dns, ndots. Read-only."""
    def _run():
        from agent.skills.dns import DNSSkill
        return {"report": DNSSkill().scan()}
    return await asyncio.to_thread(_run)


@mcp.tool()
async def domain_scan(domain: str) -> dict[str, Any]:
    """Check TLS/HTTPS status for a domain — certs, ingress, secrets. Read-only.

    domain: the domain name to check, e.g. "kibana.infragpt.online".
    """
    def _run():
        from agent.skills.domain import DomainSkill
        return DomainSkill().scan(domain)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def deploy_status() -> list[dict]:
    """List all GitHub-webhook-triggered deploys currently waiting for human
    approval. Read-only."""
    def _run():
        from agent.integrations.deploy_db import list_pending
        return [d.model_dump() for d in list_pending(status="pending")]
    return await asyncio.to_thread(_run)


@mcp.tool()
async def run_eval(skill_name: str) -> dict[str, Any]:
    """Run one skill's eval suite (mocked external calls, real skill code,
    real Claude fallback where applicable) and return the pass rate, per-case
    results, and cost. Read-only — evals never write real memory rows.

    skill_name: one of the real eval suite directory names under evals/, e.g.
        "jenkins", "k8s", "cost", "security". An invalid name raises an error
        listing the valid ones.
    """
    def _run():
        import importlib
        from pathlib import Path

        valid = sorted(
            d.name for d in Path(__file__).parent.parent.parent.joinpath("evals").iterdir()
            if d.is_dir() and (d / "runner.py").exists()
        )
        if skill_name not in valid:
            raise ValueError(f"'{skill_name}' is not a real eval suite. Valid options: {valid}")

        runner = importlib.import_module(f"evals.{skill_name}.runner")
        data = runner.run_evals()
        return {"skill": skill_name, "totals": data["totals"], "results": data["results"]}
    return await asyncio.to_thread(_run)


@mcp.tool()
async def project_status() -> dict[str, Any]:
    """Live snapshot of the AtlasOS project itself — file/line counts, skill
    inventory, eval suite coverage, and current runtime state (cluster
    connection, memory record count, daemon status). Purely a filesystem/
    runtime scan, no AI involved — unlike `agent about`'s prose summary, which
    makes its own separate (and occasionally inaccurate) Claude call.
    """
    def _run():
        import dataclasses
        from agent.skills.about import AboutSkill

        skill = AboutSkill()
        structure = skill.scan_project_structure()
        runtime = skill.scan_runtime_state()
        skills_summary = skill.get_skills_summary(structure)

        return {
            "structure": dataclasses.asdict(structure),
            "runtime": dataclasses.asdict(runtime),
            "skills": [dataclasses.asdict(s) for s in skills_summary],
        }
    return await asyncio.to_thread(_run)


@_mutating_tool()
async def incident_correlate(minutes: int = 30) -> dict[str, Any]:
    """Correlate deploys, autonomous daemon actions, and every skill's memory
    within a time window into ONE incident with a causal timeline, instead of
    treating each symptom as its own disconnected alert. Only opens/updates a
    real incident in incident_db when confidence is high or medium — a
    low-confidence guess is returned but never recorded as noise.

    minutes: how far back to look for correlated signals.
    """
    def _run():
        from agent.skills.incident_correlation import IncidentCorrelationSkill
        return IncidentCorrelationSkill().correlate(minutes).model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def metrics_status() -> dict[str, Any]:
    """Which time-series backends are reachable (Prometheus, CloudWatch) and
    whether metric analysis is running degraded. Read-only.

    Run this first when any metrics answer looks thin — "no Prometheus
    configured" is a one-minute config fix, not something to diagnose around.
    """
    def _run():
        from agent.skills.metrics import MetricsSkill
        return MetricsSkill().status()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def metrics_analyze(target: str = ".*", namespace: str = "default",
                          minutes: int = 60) -> dict[str, Any]:
    """Time-series analysis of one target: golden signals (rate/errors/latency),
    plus saturation, spike, drop, leak-shaped-trend and dead-scrape-target
    detection. Read-only.

    Detection is arithmetic and runs with or without an LLM; Claude only
    narrates. Falls back to a single kubectl-top reading when Prometheus is
    not configured, and says so via `degraded`.

    target: pod-name regex, e.g. "checkout-api.*". Defaults to everything.
    namespace: Kubernetes namespace.
    minutes: window to analyse, default 60.
    """
    def _run():
        from agent.skills.metrics import MetricsSkill
        return MetricsSkill().analyze(target, namespace, minutes).model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def metrics_verify_recovery(target: str, namespace: str = "default",
                                  minutes: int = 15) -> dict[str, Any]:
    """Did a remediation actually work? Re-reads the metric window after a fix
    and returns recovered / partial / not_recovered / unknown. Read-only.

    This is the verification half of the remediation loop: every other fix
    path in this system can confirm a fix RAN, not that the symptom stopped.

    target: pod-name regex for the thing that was fixed.
    namespace: Kubernetes namespace.
    minutes: how far back to look, default 15.
    """
    def _run():
        from agent.skills.metrics import MetricsSkill
        return MetricsSkill().verify_recovery(target, namespace, minutes)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def change_timeline(minutes: int = 60, namespace: str = "all") -> dict[str, Any]:
    """Every infrastructure change in the window, as one ordered timeline —
    k8s rollouts, scale events, our own deploy records, autonomous healer
    actions, opened incidents, Jenkins builds and git commits. Read-only.

    minutes: how far back to look, default 60.
    namespace: Kubernetes namespace, or "all".
    """
    def _run():
        from agent.skills.change import ChangeSkill
        events = ChangeSkill().collect(minutes, namespace)
        return {"window_minutes": minutes, "namespace": namespace,
                "count": len(events), "events": [e.model_dump() for e in events]}
    return await asyncio.to_thread(_run)


@mcp.tool()
async def change_correlate(symptom: str, symptom_at: str | None = None,
                           minutes: int = 60, namespace: str = "all") -> dict[str, Any]:
    """Answer "what changed just before this broke?" — ranks every change in
    the window against a symptom and names a prime suspect with a confidence
    level. Read-only.

    Scoring is deterministic: proximity in time, resource and namespace
    overlap, and how risky that kind of change is. A change that happened
    AFTER the symptom always scores zero and is excluded.

    symptom: what is wrong, e.g. "checkout-api returning 502 in prod".
    symptom_at: ISO8601 time the symptom started; defaults to now.
    minutes: window to search, default 60.
    namespace: Kubernetes namespace of the symptom, or "all".
    """
    def _run():
        from agent.skills.change import ChangeSkill
        return ChangeSkill().correlate(symptom, symptom_at, minutes, namespace).model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def topology_graph(namespace: str = "all") -> dict[str, Any]:
    """Build the service dependency graph from live cluster state: ingress ->
    service -> workload edges from real label selectors, plus inferred
    workload -> service calls. Read-only.

    Edges marked "(inferred)" come from container env values and are a
    heuristic — do not treat them as authoritative.

    namespace: Kubernetes namespace, or "all".
    """
    def _run():
        from agent.skills.topology import TopologySkill
        return TopologySkill().build(namespace).model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def blast_radius(name: str, kind: str = "workload",
                       namespace: str = "default") -> dict[str, Any]:
    """What else breaks if this component degrades — direct and transitive
    dependents, whether a user-facing ingress path reaches it, and a severity.
    Read-only.

    Ask this BEFORE draining a node, restarting a shared service or approving
    a risky fix.

    name: exact resource name.
    kind: workload | service | ingress. Default workload.
    namespace: Kubernetes namespace.
    """
    def _run():
        from agent.skills.topology import TopologySkill
        return TopologySkill().blast_radius(kind, name, namespace).model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def workloads_scan(namespace: str = "all") -> dict[str, Any]:
    """Controller-level Kubernetes scan: Deployments/StatefulSets short on
    replicas or stuck mid-rollout, DaemonSets missing from nodes, Jobs past
    their backoff limit, CronJobs suspended or no longer firing, unschedulable
    pods with the scheduler's own reason, and PodDisruptionBudgets blocking
    disruption. Read-only.

    Complements k8s_scan, which is pod-centric: these failures are invisible
    from a pod list.

    namespace: Kubernetes namespace, or "all".
    """
    def _run():
        from agent.skills.workloads import WorkloadsSkill
        return WorkloadsSkill().scan(namespace).model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def workloads_diagnose(kind: str, name: str,
                             namespace: str = "default") -> dict[str, Any]:
    """Deep dive on one controller, including its status conditions — where
    Kubernetes records WHY a rollout is stuck rather than merely that it is.
    Read-only.

    kind: deployment | statefulset | daemonset | job | cronjob.
    name: exact workload name.
    namespace: Kubernetes namespace.
    """
    def _run():
        from agent.skills.workloads import WorkloadsSkill
        return WorkloadsSkill().diagnose(kind, name, namespace)
    return await asyncio.to_thread(_run)


@_mutating_tool()
async def workloads_apply_fix(kind: str, name: str, namespace: str,
                              problem_type: str, confirm: bool = False) -> dict[str, Any]:
    """Apply the suggested fix for ONE named workload issue — scoped to that
    single resource, never a bulk operation.

    The issue is re-detected live before anything runs, so a problem that has
    already resolved itself cannot be "fixed" against stale findings. Purely
    diagnostic commands are refused rather than counted as a remediation, and
    the command still passes through the destructive-command guard in
    kubectl.apply_fix.

    kind: deployment | statefulset | daemonset | job | cronjob.
    name: exact workload name.
    namespace: Kubernetes namespace.
    problem_type: which detected issue to act on, e.g. "ScaledZero".
    confirm: must be explicitly True, or nothing is applied — the proposed fix
        is returned instead so the caller can review it first.
    """
    def _run():
        from agent.core.models import WorkloadIssue
        from agent.skills.workloads import WorkloadsSkill

        skill = WorkloadsSkill()
        detail = skill.diagnose(kind, name, namespace)
        if not detail.get("found"):
            return {"applied": False, "message": detail.get("message", "Workload not found.")}

        issues = [WorkloadIssue(**i) for i in detail.get("issues", [])]
        match = next((i for i in issues if i.problem_type.value == problem_type), None)
        if match is None:
            available = [i.problem_type.value for i in issues]
            return {"applied": False,
                    "message": (f"No current issue of type {problem_type!r} on "
                                f"{namespace}/{name}. Detected now: {available or 'none'}."),
                    "detected": available}

        result = skill.apply_fix(match, confirmed=confirm)
        result["issue"] = match.model_dump()
        return result
    return await asyncio.to_thread(_run)


@mcp.tool()
async def path_trace(url: str, namespace: str = "") -> dict[str, Any]:
    """Trace one URL hop by hop from the public internet to the pod, and name
    the weakest hop. Read-only.

    Walks nine hops in order: DNS, TLS, HTTP, AWS load balancer, Ingress rule,
    ingress controller, Service, Endpoints, workload. Hops 1-3 are REAL probes
    from the machine running this server (a DNS lookup, a TLS handshake and an
    HTTP GET) — so unlike a config check, this distinguishes "configured
    correctly" from "actually working".

    The weakest hop is the FIRST failure along the path, never the worst
    -sounding one, because an upstream failure explains every symptom
    downstream of it.

    Use this instead of guessing which subsystem to check when someone reports
    a site or API is down.

    url: full URL or bare hostname, e.g. "www.example.com" or "https://api.example.com/health".
    namespace: optional Kubernetes namespace hint; inferred from the matching
        Ingress when omitted.
    """
    def _run():
        from agent.skills.request_path import RequestPathSkill
        return RequestPathSkill().trace(url, namespace).model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def incident_postmortem(incident_id: str) -> dict[str, Any]:
    """Compile a written postmortem for an incident: a chronological timeline
    merged from the incident tracker and whatever other skills (change
    correlation, workload scans, metrics, request-path traces, pod/Jenkins
    diagnoses) recorded during its window, plus a narrative summary, root
    cause, impact and ranked action items. Read-only — writes nothing back to
    the incident.

    Adds no new evidence of its own; it only compiles what other tools already
    diagnosed. If nothing else diagnosed this incident, `degraded=true` says
    so rather than inventing a root cause. The response includes a rendered
    `markdown` field ready to paste into a wiki.

    incident_id: full or short (8-char) incident id from incident_correlate
        or the incident tracker.
    """
    def _run():
        from agent.integrations import incident_db
        from agent.skills.postmortem import PostmortemSkill

        resolved_id = incident_id
        if incident_db.get_incident(incident_id) is None and len(incident_id) <= 8:
            for cand in incident_db.list_incidents(limit=200):
                if cand["id"].startswith(incident_id):
                    resolved_id = cand["id"]
                    break
        return PostmortemSkill().generate(resolved_id).model_dump()
    return await asyncio.to_thread(_run)


class _BearerAuthMiddleware:
    """Pure-ASGI bearer-token gate in front of the MCP app.

    Accepts the token either as `Authorization: Bearer <token>` or as a
    `?token=<token>` query parameter. The query-string form exists because
    some MCP clients (ChatGPT's custom connectors among them) only offer
    "no authentication" or full OAuth — with no way to attach a static
    header — so without it the only options would be implementing an OAuth
    server or exposing the tools unauthenticated.

    The header form is preferable where a client supports it: a URL is more
    likely to be written to a proxy/tunnel access log or shell history than
    a header is. Over HTTPS both are encrypted in transit.

    Both comparisons use hmac.compare_digest so a wrong token can't be
    recovered by timing the rejection.
    """

    def __init__(self, app, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode()
        self._token = token.encode()

    def _authorized(self, scope) -> bool:
        for key, value in scope.get("headers") or []:
            if key.lower() == b"authorization" and hmac.compare_digest(value, self._expected):
                return True

        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        return any(hmac.compare_digest(t.encode(), self._token) for t in query.get("token", []))

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        if not self._authorized(scope):
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer")],
            })
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return

        await self._app(scope, receive, send)


def _serve_http() -> None:
    import uvicorn

    token = os.getenv("MCP_AUTH_TOKEN", "").strip()
    if not token:
        # Fail closed. This server reaches real Jenkins/EKS/AWS credentials on
        # this machine, so it must never bind a network port unauthenticated —
        # not even on localhost, since the whole point of HTTP mode is that a
        # tunnel will be pointed at it.
        raise SystemExit(
            "MCP_AUTH_TOKEN is required for HTTP transport and is not set.\n"
            "Generate one, e.g.:  python -c \"import secrets;print(secrets.token_urlsafe(32))\""
        )

    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8000"))

    app = _BearerAuthMiddleware(mcp.streamable_http_app(), token)
    print(f"atlasos MCP — http://{host}:{port}/mcp  "
          f"(readonly={'on' if _READONLY else 'OFF — full tool set exposed'})")
    hosts_desc = ", ".join(_ALLOWED_HOSTS) if _ALLOWED_HOSTS else "localhost only"
    print(f"  allowed hosts: {hosts_desc}")
    if not _ALLOWED_HOSTS:
        print("  (tunnelled requests will 421 until MCP_ALLOWED_HOSTS=<tunnel-domain> is set)")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    if _TRANSPORT in ("http", "streamable-http"):
        _serve_http()
    else:
        mcp.run(transport="stdio")
