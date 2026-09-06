"""
AtlasOS MCP server — Tier 1 (read-only tools only).

Exposes a subset of the exact same skill methods `cli.py` already calls, as
MCP tools an MCP client (Claude Desktop, Claude Code, ...) can invoke. This
file adds zero business logic of its own — every tool is a thin wrapper that
instantiates a skill, calls its real method, and returns the typed result as
JSON (`.model_dump()`). `cli.py` and `skills/*.py` are completely untouched.

Only read-only operations are exposed here on purpose. Anything that mutates
real infrastructure (apply_fix, heal, switch-auth, setup) is deliberately left
out of this server — see ARCHITECTURE2.md section 6 for the full tiering
rationale before adding write tools.

Run (stdio transport, for a local MCP client):
    uv run python -m agent.mcp_server
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("atlasos")


@mcp.tool()
def jenkins_scan() -> dict:
    """Scan all Jenkins jobs, agents, and the build queue; returns health score,
    failing jobs, offline agents, stuck queue items, and an AI diagnosis for
    each failure. Read-only — makes no changes to Jenkins."""
    from agent.skills.jenkins import JenkinsSkill

    return JenkinsSkill().scan().model_dump()


@mcp.tool()
def jenkins_diagnose(job_name: str, build_number: int | None = None) -> dict:
    """Deep-dive AI diagnosis of one Jenkins job's failure (root cause,
    confidence, suggested fix). Read-only — does not apply any fix.

    job_name: Jenkins job name, e.g. "backend-api/main".
    build_number: specific build to diagnose; defaults to the job's last build.
    """
    from agent.integrations import jenkins as jk
    from agent.memory.retrieval import retrieve_context
    from agent.skills.jenkins import JenkinsSkill

    jobs = {j.name: j for j in jk.get_all_jobs()}
    job = jobs.get(job_name)
    if build_number is None:
        if not job or job.last_build_number is None:
            raise ValueError(f"No build history found for '{job_name}'.")
        build_number = job.last_build_number

    build_info = jk.get_build_info(job_name, build_number)
    log_text = jk.get_console_log(job_name, build_number)
    history = jk.get_build_history(job_name, count=5)
    past = retrieve_context(f"jenkins {job_name} failure")

    diagnosis = JenkinsSkill().diagnose(job_name, build_info, log_text, history, past)
    return diagnosis.model_dump()


@mcp.tool()
def jenkins_auth_status() -> dict:
    """Verify the Jenkins connection and return version/executor/agent info."""
    from agent.integrations import jenkins as jk

    info = jk.get_connection_info()
    return info.model_dump()


@mcp.tool()
def k8s_scan(namespace: str = "all") -> list[dict]:
    """Full Kubernetes cluster health scan across pods, nodes, and resources
    for the given namespace ("all" scans every namespace). Read-only.

    namespace: Kubernetes namespace to scan, or "all" for every namespace.
    """
    from agent.skills.k8s import K8sSkill

    return K8sSkill().full_cluster_scan(namespace)


@mcp.tool()
def k8s_diagnose(pod_name: str, namespace: str) -> dict:
    """Deep AI diagnosis of one specific pod's problem (root cause, suggested
    fix). Read-only — does not apply any fix.

    pod_name: exact pod name.
    namespace: namespace the pod lives in.
    """
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


@mcp.tool()
def k8s_apply_fix(pod_name: str, namespace: str, confirm: bool = False) -> dict:
    """Diagnose one specific pod and apply the suggested kubectl fix — scoped
    to exactly this one pod, never a bulk/cluster-wide operation.

    pod_name: exact pod name.
    namespace: namespace the pod lives in.
    confirm: must be explicitly True, or nothing is applied — the diagnosis
        and proposed fix are returned instead so the caller can review first.
    """
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


@mcp.tool()
def tls_scan() -> dict:
    """Audit TLS certificates across every Ingress in the cluster — expiry,
    cert-manager status, ACME challenges, secret validity. Read-only."""
    from agent.skills.tls import TLSSkill

    return TLSSkill().scan().model_dump()


@mcp.tool()
def ingress_scan() -> dict:
    """Diagnose nginx ingress, external-IP, and routing problems across the
    cluster. Read-only — does not apply any fix."""
    from agent.skills.ingress import IngressSkill

    return IngressSkill().scan().model_dump()


@mcp.tool()
def security_audit(namespace: str = "all") -> dict:
    """Run a Kubernetes security audit (RBAC, pod security, network policies,
    exposed secrets) for the given namespace. Read-only.

    namespace: Kubernetes namespace to audit, or "all" for every namespace.
    """
    from agent.skills.security import SecurityAuditSkill

    return SecurityAuditSkill().run_audit(namespace).model_dump()


@mcp.tool()
def cost_analyze(days: int = 30) -> dict:
    """Full AWS cost optimization analysis — spend by service, waste detected,
    savings opportunities, spend anomalies, over a trailing window. Read-only.

    days: how many trailing days of AWS Cost Explorer data to analyze.
    """
    from agent.skills.cost import CostAnalysisSkill

    return CostAnalysisSkill().full_aws_analysis(days).model_dump()


@mcp.tool()
def aws_auth_status() -> dict:
    """Verify the configured AWS auth method (IAM role / access key / SSO
    profile) currently works, and return the resolved identity. Read-only."""
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


# ---------------------------------------------------------------------------
# Tier 2 — mutating tools. Every one requires confirm=True from the caller;
# without it, they return the diagnosis/fix that WOULD run and do nothing.
# ---------------------------------------------------------------------------

@mcp.tool()
def jenkins_apply_fix(job_name: str, build_number: int | None = None, confirm: bool = False) -> dict:
    """Diagnose a Jenkins job's failure and apply the suggested fix (retrigger,
    restart agent, clear workspace, or cancel+retrigger). Does NOT wait for the
    fix to be verified — call jenkins_scan again shortly after to check whether
    the job actually recovered.

    job_name: Jenkins job name, e.g. "backend-api/main".
    build_number: specific build to diagnose; defaults to the job's last build.
    confirm: must be explicitly True, or nothing is applied — the diagnosis
        and proposed fix are returned instead so the caller can review first.
    """
    from agent.integrations import jenkins as jk
    from agent.memory.retrieval import retrieve_context
    from agent.skills.jenkins import JenkinsSkill

    skill = JenkinsSkill()
    jobs = {j.name: j for j in jk.get_all_jobs()}
    job = jobs.get(job_name)
    if build_number is None:
        if not job or job.last_build_number is None:
            raise ValueError(f"No build history found for '{job_name}'.")
        build_number = job.last_build_number

    build_info = jk.get_build_info(job_name, build_number)
    log_text = jk.get_console_log(job_name, build_number)
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


@mcp.tool()
def ingress_apply_fix(name: str, namespace: str = "default", confirm: bool = False) -> dict:
    """Diagnose an Ingress resource's problem and apply the suggested kubectl
    fix.

    name: Ingress resource name.
    namespace: namespace the Ingress lives in.
    confirm: must be explicitly True, or nothing is applied.
    """
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


@mcp.tool()
def tls_apply_fix(name: str, namespace: str, kind: str = "certificate", confirm: bool = False) -> dict:
    """Diagnose a TLS certificate/issuer problem and apply the suggested
    kubectl fix.

    name: name of the Certificate (or other TLS-related) resource.
    namespace: namespace the resource lives in.
    kind: resource kind being diagnosed, defaults to "certificate".
    confirm: must be explicitly True, or nothing is applied.
    """
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


@mcp.tool()
def cost_apply_fix(fix_id: str, days: int = 30, confirm: bool = False) -> dict:
    """Apply one specific AWS cost-saving fix by ID (from a prior cost_analyze
    call's "savings_plan" list) — e.g. an EBS gp2->gp3 upgrade or a CloudWatch
    log retention policy. Only ever applies auto-fixable fixes.

    fix_id: the "id" field of the fix to apply, from cost_analyze's output.
    days: lookback window to re-run the analysis with, must match how the
        fix was originally found.
    confirm: must be explicitly True, or nothing is applied.
    """
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


# ---------------------------------------------------------------------------
# Tier 1 additions — memory, DNS/domain, deploy status, eval suites, project status
# ---------------------------------------------------------------------------

@mcp.tool()
def memory_search(query: str, limit: int = 5) -> list[dict]:
    """Semantic search over AtlasOS's stored incident memory (SQLite + vector
    store) — past scans, diagnoses, and fixes across every skill. Read-only.

    query: natural-language search, e.g. "jenkins flaky test" or "aws cost waste".
    limit: max results to return.
    """
    from agent.memory.retrieval import retrieve_context

    return [m.model_dump() for m in retrieve_context(query, limit=limit)]


@mcp.tool()
def dns_scan() -> dict:
    """Full DNS health audit — CoreDNS pods, config, resolution tests,
    external-dns, ndots. Read-only."""
    from agent.skills.dns import DNSSkill

    return {"report": DNSSkill().scan()}


@mcp.tool()
def domain_scan(domain: str) -> dict:
    """Check TLS/HTTPS status for a domain — certs, ingress, secrets. Read-only.

    domain: the domain name to check, e.g. "kibana.infragpt.online".
    """
    from agent.skills.domain import DomainSkill

    return DomainSkill().scan(domain)


@mcp.tool()
def deploy_status() -> list[dict]:
    """List all GitHub-webhook-triggered deploys currently waiting for human
    approval. Read-only."""
    from agent.integrations.deploy_db import list_pending

    return [d.model_dump() for d in list_pending(status="pending")]


@mcp.tool()
def run_eval(skill_name: str) -> dict:
    """Run one skill's eval suite (mocked external calls, real skill code,
    real Claude fallback where applicable) and return the pass rate, per-case
    results, and cost. Read-only — evals never write real memory rows.

    skill_name: one of the real eval suite directory names under evals/, e.g.
        "jenkins", "k8s", "cost", "security". An invalid name raises an error
        listing the valid ones.
    """
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


@mcp.tool()
def project_status() -> dict:
    """Live snapshot of the AtlasOS project itself — file/line counts, skill
    inventory, eval suite coverage, and current runtime state (cluster
    connection, memory record count, daemon status). Purely a filesystem/
    runtime scan, no AI involved — unlike `agent about`'s prose summary, which
    makes its own separate (and occasionally inaccurate) Claude call.
    """
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
