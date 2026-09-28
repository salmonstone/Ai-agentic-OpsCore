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
import json
import os
import urllib.request
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Lets MCP_AUTH_TOKEN live in .env alongside the other secrets (it is already
# gitignored) instead of being retyped per shell. Does not override a variable
# that is already set, so an explicit `set VAR=...` still wins.
load_dotenv()

_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()

# Public hostname(s) this server is reached by, e.g. an ngrok domain. MCP's
# streamable-http transport validates the Host header to block DNS-rebinding
# attacks, and rejects anything not on this list with 421 Misdirected Request
# — so a tunnelled request fails until its hostname is named here. Hostname
# wildcards are NOT supported upstream (only ":*" port wildcards), so each
# public host must be listed exactly. Comma-separated.
def _ngrok_hostnames() -> list[str]:
    """Ask a locally-running ngrok agent what public hostnames it is serving.

    The free tier issues a new URL on every restart, so hardcoding it means
    MCP_ALLOWED_HOSTS silently goes stale and every tunnelled request 421s.
    ngrok exposes its current tunnels on a local API, so the hostname can just
    be discovered at startup instead of maintained by hand.

    Best-effort: a short timeout and a broad except, because ngrok not running
    is the normal case for stdio and must not delay or break startup.

    NGROK_API_URL overrides the address: inside a container 127.0.0.1 is the
    container itself, so ngrok on the host is reached via host.docker.internal.
    """
    api = os.getenv("NGROK_API_URL", "http://127.0.0.1:4040").rstrip("/")
    try:
        with urllib.request.urlopen(f"{api}/api/tunnels", timeout=1.5) as resp:
            tunnels = json.load(resp).get("tunnels", [])
    except Exception:
        return []

    hosts = []
    for t in tunnels:
        host = urlparse(t.get("public_url", "")).hostname
        if host and host not in hosts:
            hosts.append(host)
    return hosts


_ALLOWED_HOSTS = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
if not _ALLOWED_HOSTS and _TRANSPORT in ("http", "streamable-http"):
    _ALLOWED_HOSTS = _ngrok_hostnames()


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
async def jenkins_scan() -> dict:
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
                                 confirm_destructive_name: bool = False) -> dict:
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
async def jenkins_diagnose(job_name: str, build_number: int | None = None) -> dict:
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
async def jenkins_auth_status() -> dict:
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
async def k8s_list_nodes() -> list[dict]:
    """List every node in the cluster — status, instance type, kubelet
    version, capacity/allocatable CPU and memory. Read-only.

    Reuses the same collector agent k8s pods (the full cluster-pod view)
    already calls for its node summary; this exposes it standalone."""
    def _run():
        from agent.integrations.kubectl import get_nodes_detail
        return [n.model_dump() for n in get_nodes_detail()]
    return await asyncio.to_thread(_run)


@mcp.tool()
async def k8s_pod_metrics(namespace: str = "all") -> list[dict]:
    """Real per-pod CPU/memory usage from the metrics-server (kubectl top
    pods), each with its percentage against its resource limit. Read-only.

    Empty list means the metrics-server isn't installed/reachable, not that
    no pods exist — check k8s_list_pods for the pod list itself.

    namespace: Kubernetes namespace, or "all" for every namespace.
    """
    def _run():
        from agent.integrations.kubectl import get_pod_metrics
        return [m.model_dump() for m in get_pod_metrics(namespace)]
    return await asyncio.to_thread(_run)


@mcp.tool()
async def k8s_diagnose(pod_name: str, namespace: str) -> dict:
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
async def k8s_apply_fix(pod_name: str, namespace: str, confirm: bool = False) -> dict:
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


@_mutating_tool()
async def k8s_crashloop_apply_fix(
    pod_name: str, namespace: str, deployment: str, container_name: str,
    fix_kind: str, fix_value: str, restart_count: int = 0, inc_id: str = "",
    confirm: bool = False,
) -> dict:
    """Apply a crashloop fix the autonomous daemon proposed — an AI-guessed
    container command patch or kubectl command — after a plain rolling
    restart didn't recover the pod on its own. Not something to call from
    scratch: the daemon (agent.skills.healer) proposes these via Slack with
    all the right parameters already filled in; this applies exactly that
    proposal once approved.

    fix_kind: "patch_command" (fix_value is a JSON array, the new container
        command) or "kubectl" (fix_value is the command to run).
    confirm: must be explicitly True, or nothing is applied.
    """
    def _run():
        from agent.skills.healer import apply_crashloop_fix
        if not confirm:
            return {"applied": False, "message": "Set confirm=True to actually apply this fix.",
                    "fix_kind": fix_kind, "fix_value": fix_value}
        return apply_crashloop_fix(pod_name, namespace, deployment, container_name,
                                   fix_kind, fix_value, restart_count, inc_id)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def tls_scan() -> dict:
    """Audit TLS certificates across every Ingress in the cluster — expiry,
    cert-manager status, ACME challenges, secret validity. Read-only."""
    def _run():
        from agent.skills.tls import TLSSkill
        return TLSSkill().scan().model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def ingress_scan() -> dict:
    """Diagnose nginx ingress, external-IP, and routing problems across the
    cluster. Read-only — does not apply any fix."""
    def _run():
        from agent.skills.ingress import IngressSkill
        return IngressSkill().scan().model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def security_audit(namespace: str = "all") -> dict:
    """Run a Kubernetes security audit (RBAC, pod security, network policies,
    exposed secrets) for the given namespace. Read-only.

    namespace: Kubernetes namespace to audit, or "all" for every namespace.
    """
    def _run():
        from agent.skills.security import SecurityAuditSkill
        return SecurityAuditSkill().run_audit(namespace).model_dump()
    return await asyncio.to_thread(_run)


@_mutating_tool()
async def security_drift(namespace: str = "all") -> dict:
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
async def cost_analyze(days: int = 30) -> dict:
    """Full AWS cost optimization analysis — spend by service, waste detected,
    savings opportunities, spend anomalies, over a trailing window. Read-only.

    days: how many trailing days of AWS Cost Explorer data to analyze.
    """
    def _run():
        from agent.skills.cost import CostAnalysisSkill
        return CostAnalysisSkill().full_aws_analysis(days).model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def aws_scan(region: str = "", services: str = "ec2,rds,alb") -> dict:
    """Find PROBLEMS in the AWS account — unhealthy EC2/RDS/ALB resources
    only, each with an AI root-cause diagnosis. Read-only.

    This does NOT report how many resources exist. unhealthy_count=0 means
    every resource that was checked is healthy, NOT that no resources exist —
    do not use this to answer "how many EC2 instances are running"; call
    aws_inventory for the full resource count and list.

    region: AWS region to scan; defaults to your configured AWS_REGION.
    services: comma-separated subset to check — ec2, rds, alb.
    """
    def _run():
        from agent.config import settings
        from agent.skills.aws import AwsSkill

        svc_list = [s.strip().lower() for s in services.split(",") if s.strip()]
        diagnoses = AwsSkill().scan_account(region or settings.aws_region, svc_list)
        return {
            "unhealthy_count": len(diagnoses),
            "diagnoses": [d.model_dump() for d in diagnoses],
            "note": "unhealthy_count is problems found, not total resources. "
                    "0 means healthy, not absent. Call aws_inventory for the "
                    "actual resource count and list.",
        }
    return await asyncio.to_thread(_run)


@mcp.tool()
async def aws_inventory(region: str = "", services: str = "ec2,rds,eip,lb,sg") -> dict:
    """Full AWS resource inventory — every EC2 instance, RDS database, Elastic
    IP, load balancer, and security group, not just the unhealthy ones (see
    aws_scan for that). Read-only, same EC2/EIP/LB/SG data as `agent aws
    list`, plus RDS which that command doesn't cover either.

    region: AWS region; defaults to your configured AWS_REGION.
    services: comma-separated subset — ec2, rds, eip, lb, sg.
    """
    def _run():
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from agent.config import settings
        from agent.integrations.aws import (
            get_all_ec2, get_all_load_balancers, get_all_rds, get_elastic_ips, get_security_groups,
        )

        r = region or settings.aws_region
        wanted = {s.strip().lower() for s in services.split(",") if s.strip()}
        fetchers = {
            "ec2": get_all_ec2, "rds": get_all_rds, "eip": get_elastic_ips,
            "lb": get_all_load_balancers, "sg": get_security_groups,
        }
        fetchers = {k: v for k, v in fetchers.items() if k in wanted}

        results: dict[str, list] = {}
        # max_workers=2, matching the fix applied to dns_collector.py and
        # security.py's run_audit(): each of these shells out its own boto3
        # call, and higher unthrottled concurrency has caused real, transient
        # connection failures elsewhere in this codebase under the same
        # pattern. The CLI's `agent aws list` still uses 4 workers directly —
        # left as-is since it has not exhibited the failure, but new call
        # sites default to the safer value.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(fn, r): name for name, fn in fetchers.items()}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    results[name] = fut.result()
                except Exception as exc:
                    results[name] = [{"error": str(exc)}]

        return {
            "region": r,
            "ec2_instances":     results.get("ec2", []),
            "rds_instances":     results.get("rds", []),
            "elastic_ips":       results.get("eip", []),
            "load_balancers":    results.get("lb", []),
            "security_groups":   results.get("sg", []),
        }
    return await asyncio.to_thread(_run)


@mcp.tool()
async def aws_diagnose(resource_id: str, resource_type: str, region: str = "") -> dict:
    """AI root-cause diagnosis of one specific AWS resource. Read-only — does
    not apply any fix (see aws_apply_fix for that).

    resource_id: instance ID, DB identifier, or target group ARN.
    resource_type: "ec2" | "rds" | "alb".
    region: AWS region; defaults to your configured AWS_REGION.
    """
    def _run():
        from agent.config import settings
        from agent.core.models import AwsResource, AwsResourceType
        from agent.skills.aws import AwsSkill

        type_map = {"ec2": AwsResourceType.EC2, "rds": AwsResourceType.RDS, "alb": AwsResourceType.ALB}
        rtype = type_map.get(resource_type.lower())
        if rtype is None:
            return {"error": f"Unknown resource type: {resource_type!r}. Use: ec2, rds, alb"}

        resource = AwsResource(
            id=resource_id, name=resource_id, resource_type=rtype,
            status="unknown", region=region or settings.aws_region,
        )
        return AwsSkill().diagnose_resource(resource).model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def aws_auth_status() -> dict:
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


@mcp.tool()
async def aws_capacity_forecast(region: str = "", days: int = 14) -> dict:
    """Project when each RDS instance's free storage runs out, from a plain
    linear trend over recent CloudWatch history — not just current usage.
    Read-only. Only ever extrapolates a SHRINKING trend; flat or growing
    free space reports days_until_full=null, never a backwards forecast.

    region: AWS region; defaults to your configured AWS_REGION.
    days: how many days of CloudWatch history to fit the trend to.
    """
    def _run():
        from agent.integrations.rds import forecast_storage_capacity
        return {"forecasts": forecast_storage_capacity(region, days)}
    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Propose-then-approve — available even in readonly mode. These never change
# infrastructure: propose_fix records a pending proposal and sends a Slack
# Approve/Reject message; only a human tapping Approve runs the fix, through
# the same *_apply_fix function with confirm=True. Limits (allowed kinds,
# param validation, dedupe, max pending) live in agent.core.fix_registry.
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_proposable_fixes() -> dict:
    """Which fixes can be proposed with propose_fix, and the params each needs.
    Read-only."""
    def _run():
        from agent.core import fix_registry
        return {k: fix_registry.param_spec(k) for k in sorted(fix_registry.REMOTE_PROPOSABLE)}
    return await asyncio.to_thread(_run)


@mcp.tool()
async def propose_fix(kind: str, summary: str, params: dict | None = None) -> dict:
    """Propose a fix for a HUMAN to approve in Slack. Changes nothing itself.

    Use this after diagnosing a problem (k8s_diagnose, jenkins_diagnose,
    aws_diagnose, cost_analyze, ...) when a fix would help. It sends the
    operator a Slack message with Approve/Reject buttons; the fix only runs
    if they tap Approve, and then re-diagnoses fresh before acting. Tell the
    user it's waiting for their approval — never say it was applied.

    kind: one of list_proposable_fixes(), e.g. "k8s_apply_fix".
    summary: one plain sentence for the approver — what's wrong and what the
        fix does, e.g. "Pod api-7d8 is CrashLoopBackOff; restart it".
    params: that kind's arguments, e.g. {"pod_name": "api-7d8", "namespace": "prod"}.
        Never include `confirm`.
    """
    def _run():
        from agent.core import fix_registry
        try:
            r = fix_registry.propose(kind, params or {}, summary, source="MCP (ChatGPT)", remote=True)
        except fix_registry.ProposalError as e:
            return {"proposed": False, "error": str(e)}
        msg = ("An identical proposal is already waiting for approval — not sent again."
               if r["duplicate"] else
               "Sent to Slack for human approval. Nothing has been changed yet.")
        return {"proposed": True, "approval_id": r["id"], "status": r["status"],
                "expires_at": r["expires_at"], "message": msg}
    return await asyncio.to_thread(_run)


@mcp.tool()
async def approval_status(approval_id: str) -> dict:
    """Check whether a proposed fix was approved, rejected, applied, failed,
    or expired — and its result. Read-only."""
    def _run():
        from agent.core import approvals
        approvals.expire_stale()
        a = approvals.get(approval_id)
        if a is None:
            return {"found": False, "error": f"No approval with id {approval_id!r}"}
        return {"found": True, "id": a.id, "kind": a.kind, "status": a.status,
                "summary": a.summary, "decided_by": a.decided_by, "result": a.result,
                "expires_at": a.expires_at}
    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Tier 2 — mutating tools. Every one requires confirm=True from the caller;
# without it, they return the diagnosis/fix that WOULD run and do nothing.
# ---------------------------------------------------------------------------

@_mutating_tool()
async def jenkins_apply_fix(job_name: str, build_number: int | None = None, confirm: bool = False) -> dict:
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
async def ingress_apply_fix(name: str, namespace: str = "default", confirm: bool = False) -> dict:
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
async def tls_apply_fix(name: str, namespace: str, kind: str = "certificate", confirm: bool = False) -> dict:
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
async def aws_apply_fix(resource_id: str, resource_type: str, region: str = "", confirm: bool = False) -> dict:
    """Diagnose one specific AWS resource and apply the suggested fix —
    scoped to exactly this one resource, never account-wide. For the
    diagnosis alone, without applying anything, use aws_diagnose.

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
async def cost_apply_fix(fix_id: str, days: int = 30, confirm: bool = False) -> dict:
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
async def dns_scan() -> dict:
    """Full DNS health audit — CoreDNS pods, config, resolution tests,
    external-dns, ndots. Read-only."""
    def _run():
        from agent.skills.dns import DNSSkill
        return DNSSkill().scan().model_dump()
    return await asyncio.to_thread(_run)


@mcp.tool()
async def domain_scan(domain: str) -> dict:
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
async def run_eval(skill_name: str) -> dict:
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
async def project_status() -> dict:
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
async def incident_correlate(minutes: int = 30) -> dict:
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


class _PathDispatch:
    """Route /webhook/* and /slack/* to the webhook receiver, everything else
    to the bearer-gated MCP app.

    Those two prefixes carry their own HMAC signature checks (GitHub's and
    Slack's) — a second, different proof of identity than the MCP bearer
    token — so they are exempted from that token here rather than doubly
    protected by it. This lets Slack's button clicks and GitHub's push
    events reach this server through the same ngrok tunnel as /mcp, instead
    of needing a second exposed port.
    """

    def __init__(self, mcp_app, webhook_app) -> None:
        self._mcp = mcp_app
        self._webhook = webhook_app

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (path.startswith("/webhook/") or path.startswith("/slack/")):
            await self._webhook(scope, receive, send)
            return
        await self._mcp(scope, receive, send)


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
    try:
        from agent.integrations.webhook import app as webhook_app
        app = _PathDispatch(app, webhook_app)
        webhook_mounted = True
    except Exception as exc:
        webhook_mounted = False
        print(f"  (GitHub/Slack webhook receiver not mounted: {exc})")

    print(f"atlasos MCP — http://{host}:{port}/mcp  "
          f"(readonly={'on' if _READONLY else 'OFF — full tool set exposed'})")
    if webhook_mounted:
        print(f"  webhooks: http://{host}:{port}/webhook/github  and  /slack/actions "
              "(own signature checks, not the MCP token)")
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
