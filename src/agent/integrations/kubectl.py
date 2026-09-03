"""
Safe kubectl wrapper for the AI Agentic OS.

All kubectl calls go through run_kubectl() — no raw subprocess elsewhere.
Never raises exceptions; always returns a KubectlResult so callers can
decide what to do with errors.

Kubeconfig resolution order (same as kubectl itself):
  1. KUBECONFIG env var
  2. ~/.kube/config
Works with any cluster: EKS, GKE, AKS, minikube, kind, etc.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from agent.core.models import (
    CertInfo, ClusterOverview, IngressInfo, KubectlResult,
    NamespaceInfo, NodeInfo, PodInfo, ProblemType, TLSSecretInfo,
)
from agent.observability.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = 8  # seconds for every kubectl call (reduced from 30 to avoid blocking dashboard)

# Cluster availability cache — checked at most once per 30 seconds
_avail_cache: dict = {"ok": None, "ts": 0.0}


def is_cluster_available() -> bool:
    """
    Fast cluster reachability check using a TCP socket probe — no subprocess.

    Reads the kubeconfig to find the API server URL, then attempts a 2-second
    TCP connection. Returns within ~2 seconds even when the cluster is offline.
    Caches the result for 30 seconds so the dashboard pusher isn't thrashing.
    """
    import socket
    import re
    from pathlib import Path as _Path

    now = time.monotonic()
    if _avail_cache["ok"] is not None and (now - _avail_cache["ts"]) < 30:
        return _avail_cache["ok"]

    def _set(ok: bool) -> bool:
        _avail_cache.update({"ok": ok, "ts": now})
        return ok

    if not shutil.which("kubectl"):
        return _set(False)

    # Locate kubeconfig
    import os
    kube_config_path = os.environ.get("KUBECONFIG") or str(_Path.home() / ".kube" / "config")
    kube_file = _Path(kube_config_path)
    if not kube_file.exists():
        return _set(False)

    # Parse server URL — try yaml first, fall back to regex
    try:
        import yaml
        cfg = yaml.safe_load(kube_file.read_text(encoding="utf-8", errors="replace"))
        current_ctx = cfg.get("current-context", "")
        if not current_ctx:
            return _set(False)
        ctx_map = {c["name"]: c.get("context", {}) for c in (cfg.get("contexts") or [])}
        cl_map  = {c["name"]: c.get("cluster", {}) for c in (cfg.get("clusters") or [])}
        cluster_name = ctx_map.get(current_ctx, {}).get("cluster", "")
        server = cl_map.get(cluster_name, {}).get("server", "")
    except Exception:
        # Regex fallback — grab first `server:` line
        m = re.search(r'server:\s*(https?://[^\s]+)', kube_file.read_text(errors="replace"))
        server = m.group(1) if m else ""

    if not server:
        return _set(False)

    # TCP probe — instant on Windows, no subprocess required
    try:
        from urllib.parse import urlparse
        parsed = urlparse(server)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host:
            return _set(False)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        try:
            ok = sock.connect_ex((host, port)) == 0
        finally:
            sock.close()
    except Exception:
        ok = False

    return _set(ok)


# Error strings kubectl prints when the API server doesn't recognize the
# caller's identity at all (as opposed to recognizing it and denying a
# specific verb, which is a normal RBAC "no").
_AUTH_UNRECOGNIZED_MARKERS = (
    "must be logged in to the server",
    "the server has asked for the client to provide credentials",
)

_auth_cache: dict = {"ok": None, "ts": 0.0, "ctx": "", "detail": ""}


def check_cluster_auth() -> tuple[bool, str]:
    """
    Verify the current kubeconfig identity is actually authorized inside the
    cluster — not just that the API server is reachable.

    is_cluster_available() only proves the server is up (TCP probe); a
    perfectly valid AWS/GCP/Azure session can still be rejected by the
    cluster's own RBAC (e.g. missing from an EKS aws-auth ConfigMap). This
    makes one cheap authenticated call and caches the result for 30s per
    context so repeated commands don't pay for it each time.

    Returns (ok, error_detail). error_detail is "" when ok is True.
    """
    now = time.monotonic()
    ctx = get_current_context()
    if (_auth_cache["ok"] is not None and _auth_cache["ctx"] == ctx
            and (now - _auth_cache["ts"]) < 30):
        return _auth_cache["ok"], _auth_cache["detail"]

    result = run_kubectl(["auth", "can-i", "get", "pods"], timeout=6)
    # "yes"/"no" on stdout both mean the server recognized the identity —
    # only the specific unrecognized-identity errors mean "not authorized at all".
    unrecognized = any(m in (result.error or "") for m in _AUTH_UNRECOGNIZED_MARKERS)
    ok = not unrecognized
    detail = "" if ok else result.error.strip()

    _auth_cache.update({"ok": ok, "ts": now, "ctx": ctx, "detail": detail})
    return ok, detail


def build_eks_access_fix_hint(context_name: str) -> str:
    """
    Build actionable remediation text for the EKS aws-auth ConfigMap lockout —
    where AWS credentials are valid but the cluster's own RBAC has never
    heard of that IAM identity. Falls back to generic guidance for non-EKS
    contexts or when details can't be parsed.
    """
    import re as _re

    m = _re.search(r"arn:aws:eks:([\w-]+):(\d+):cluster/([\w-]+)", context_name)
    if not m:
        return (
            "Your credentials are valid but the cluster's RBAC doesn't "
            "recognize this identity. Ask a cluster admin to grant it "
            "access (e.g. add it to the cluster's RBAC / identity mapping)."
        )

    region, account, cluster = m.groups()
    identity = ""
    try:
        r = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            identity = r.stdout.strip()
    except Exception:
        pass
    identity = identity or "<your IAM user/role ARN>"

    return (
        f"Your AWS credentials are valid, but cluster '{cluster}' doesn't "
        f"recognize this identity in its RBAC (aws-auth ConfigMap).\n\n"
        f"Fix — run as an account admin / root (e.g. via AWS CloudShell), "
        f"no kubectl access needed:\n\n"
        f"  aws eks update-cluster-config --name {cluster} --region {region} \\\n"
        f"    --access-config authenticationMode=API_AND_CONFIG_MAP\n\n"
        f"  # wait until: aws eks describe-cluster --name {cluster} --region {region} "
        f"--query cluster.status --output text   (should print ACTIVE)\n\n"
        f"  aws eks create-access-entry --cluster-name {cluster} --region {region} \\\n"
        f"    --principal-arn {identity}\n\n"
        f"  aws eks associate-access-policy --cluster-name {cluster} --region {region} \\\n"
        f"    --principal-arn {identity} \\\n"
        f"    --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \\\n"
        f"    --access-scope type=cluster"
    )


# Pod statuses that we consider unhealthy
_PROBLEM_STATUSES = {
    "CrashLoopBackOff",
    "OOMKilled",
    "Error",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "CreateContainerError",
    "InvalidImageName",
    "Terminating",
    "Pending",
    "Unknown",
    "ContainerStatusUnknown",
    "OOMKilling",
}


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------

def run_kubectl(command: list[str], timeout: int = _TIMEOUT) -> KubectlResult:
    """
    Run a kubectl command safely.

    Prepends 'kubectl' if not already the first token.
    Returns KubectlResult — never raises.
    """
    if not command or command[0] != "kubectl":
        command = ["kubectl"] + command

    # Verify kubectl is installed
    if not shutil.which("kubectl"):
        msg = "kubectl not found on PATH. Install it: https://kubernetes.io/docs/tasks/tools/"
        log.error("kubectl.not_found")
        return KubectlResult(
            command=command, output="", error=msg,
            success=False, duration_ms=0.0,
        )

    log.info("kubectl.run", cmd=" ".join(command))
    t0 = time.perf_counter()

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        success     = proc.returncode == 0

        if success:
            log.info("kubectl.ok", cmd=command[1], duration_ms=duration_ms)
        else:
            log.warning("kubectl.failed",
                        cmd=command[1],
                        returncode=proc.returncode,
                        stderr=proc.stderr[:200])

        return KubectlResult(
            command=command,
            output=proc.stdout,
            error=proc.stderr,
            success=success,
            duration_ms=duration_ms,
        )

    except subprocess.TimeoutExpired:
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        log.error("kubectl.timeout", cmd=command[1], timeout=_TIMEOUT)
        return KubectlResult(
            command=command,
            output="",
            error=f"kubectl timed out after {_TIMEOUT}s",
            success=False,
            duration_ms=duration_ms,
        )
    except Exception as exc:
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        log.error("kubectl.exception", cmd=command[1], error=str(exc))
        return KubectlResult(
            command=command,
            output="",
            error=str(exc),
            success=False,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Pod helpers
# ---------------------------------------------------------------------------

def _parse_pod_status(pod: dict) -> str:
    """
    Derive the pod's effective status string — mirrors what `kubectl get pods` shows.

    Checks container statuses for waiting/terminated reasons first, then
    falls back to pod phase.
    """
    status_obj = pod.get("status", {})
    phase      = status_obj.get("phase", "Unknown")

    # Check init containers
    for cs in status_obj.get("initContainerStatuses", []):
        waiting = cs.get("state", {}).get("waiting", {})
        if waiting.get("reason"):
            return waiting["reason"]
        terminated = cs.get("state", {}).get("terminated", {})
        if terminated.get("reason") and terminated["reason"] != "Completed":
            return terminated["reason"]

    # Check regular containers
    for cs in status_obj.get("containerStatuses", []):
        waiting = cs.get("state", {}).get("waiting", {})
        if waiting.get("reason"):
            return waiting["reason"]
        terminated = cs.get("state", {}).get("terminated", {})
        if terminated.get("reason") and terminated["reason"] not in ("Completed", ""):
            return terminated["reason"]

    # Check deletion timestamp → Terminating
    if pod.get("metadata", {}).get("deletionTimestamp"):
        return "Terminating"

    return phase


def _parse_ready(pod: dict) -> str:
    """Return 'X/Y' ready string."""
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if not statuses:
        return "0/0"
    total = len(statuses)
    ready = sum(1 for cs in statuses if cs.get("ready", False))
    return f"{ready}/{total}"


def _parse_restarts(pod: dict) -> int:
    """Total restart count across all containers."""
    statuses = pod.get("status", {}).get("containerStatuses", [])
    return sum(cs.get("restartCount", 0) for cs in statuses)


def _parse_age(pod: dict) -> str:
    """Human-readable age from creationTimestamp."""
    ts_str = pod.get("metadata", {}).get("creationTimestamp", "")
    if not ts_str:
        return "?"
    try:
        created = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        delta   = datetime.now(timezone.utc) - created
        secs    = int(delta.total_seconds())
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h"
        return f"{secs // 86400}d"
    except Exception:
        return "?"


def _is_pending_too_long(pod: dict) -> bool:
    """True if pod has been Pending for more than 5 minutes."""
    if _parse_pod_status(pod) != "Pending":
        return False
    ts_str = pod.get("metadata", {}).get("creationTimestamp", "")
    if not ts_str:
        return True
    try:
        created = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        delta   = datetime.now(timezone.utc) - created
        return delta.total_seconds() > 300
    except Exception:
        return True


def _parse_conditions(pod: dict) -> list[str]:
    """Return human-readable condition strings from pod status conditions."""
    out = []
    for c in pod.get("status", {}).get("conditions", []):
        ctype  = c.get("type", "?")
        status = c.get("status", "?")
        reason = c.get("reason", "")
        out.append(f"{ctype}={status}" + (f" ({reason})" if reason else ""))
    return out


def _pod_to_info(pod: dict) -> PodInfo:
    spec = pod.get("spec", {})
    return PodInfo(
        name       = pod["metadata"]["name"],
        namespace  = pod["metadata"].get("namespace", "default"),
        status     = _parse_pod_status(pod),
        ready      = _parse_ready(pod),
        restarts   = _parse_restarts(pod),
        age        = _parse_age(pod),
        node       = spec.get("nodeName") or "<none>",
        ip         = pod.get("status", {}).get("podIP", ""),
        containers = [c["name"] for c in spec.get("containers", [])],
        images     = [c.get("image", "") for c in spec.get("containers", [])],
        conditions = _parse_conditions(pod),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_pods(namespace: str = "all") -> list[PodInfo]:
    """
    Return all pods across the cluster (or a single namespace).

    Args:
        namespace: "all" for -A, otherwise the namespace name.
    """
    if namespace == "all":
        cmd = ["get", "pods", "-A", "-o", "json"]
    else:
        cmd = ["get", "pods", "-n", namespace, "-o", "json"]

    result = run_kubectl(cmd)
    if not result.success or not result.output.strip():
        log.warning("get_pods.failed", error=result.error[:200])
        return []

    try:
        data = json.loads(result.output)
        return [_pod_to_info(p) for p in data.get("items", [])]
    except json.JSONDecodeError as e:
        log.error("get_pods.parse_error", error=str(e))
        return []


def get_problematic_pods() -> list[PodInfo]:
    """
    Return unhealthy pods using --no-headers table output.
    Avoids loading large JSON — much lighter on Windows page file.
    Full JSON (get_pods) is reserved for diagnose_pod which needs containers/images.
    """
    result = run_kubectl(["get", "pods", "-A", "--no-headers"])
    if not result.success or not result.output.strip():
        log.warning("get_problematic_pods.failed", error=result.error[:200])
        return []

    bad:   list[PodInfo] = []
    total: int           = 0

    for line in result.output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split()
        # NAMESPACE  NAME  READY  STATUS  RESTARTS  AGE
        if len(parts) < 5:
            continue
        total += 1
        ns          = parts[0]
        name        = parts[1]
        ready       = parts[2]
        status      = parts[3]
        restart_raw = parts[4].split("(")[0].strip()   # strip "(Xh ago)" suffix
        try:
            restarts = int(restart_raw)
        except ValueError:
            restarts = 0
        age = parts[5] if len(parts) > 5 else ""

        if status in _PROBLEM_STATUSES or (status == "Running" and restarts >= 5):
            bad.append(PodInfo(
                name=name, namespace=ns, status=status,
                ready=ready, restarts=restarts, age=age, node="<none>",
            ))

    log.info("get_problematic_pods.result", total=total, problematic=len(bad))
    return bad


def get_node_issues() -> list[dict]:
    """Nodes that are not in Ready state."""
    result = run_kubectl(["get", "nodes", "--no-headers"])
    if not result.success:
        return []
    issues = []
    for line in result.output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name, status = parts[0], parts[1]
        if status != "Ready":
            issues.append({
                "category": "node",
                "severity": "critical",
                "resource": name,
                "namespace": "",
                "description": f"Node '{name}' is {status} — pods cannot be scheduled here",
                "fix_command": f"kubectl describe node {name}",
            })
    return issues


def get_quota_issues() -> list[dict]:
    """Namespaces where a FailedCreate event signals resource quota exceeded."""
    result = run_kubectl([
        "get", "events", "-A", "--no-headers",
        "--field-selector", "reason=FailedCreate",
    ])
    if not result.success:
        return []
    seen: set[str] = set()
    issues = []
    for line in result.output.strip().splitlines():
        if not line.strip() or "quota" not in line.lower():
            continue
        parts = line.split()
        ns = parts[0] if parts else "unknown"
        if ns in seen:
            continue
        seen.add(ns)
        issues.append({
            "category": "quota",
            "severity": "critical",
            "resource": "ResourceQuota",
            "namespace": ns,
            "description": f"Resource quota exceeded in '{ns}' — new pods blocked",
            "fix_command": f"kubectl describe resourcequota -n {ns}",
        })
    return issues


def get_probe_failures() -> list[dict]:
    """Pods with recent liveness or readiness probe failures (Unhealthy events)."""
    result = run_kubectl([
        "get", "events", "-A", "--no-headers",
        "--field-selector", "reason=Unhealthy",
    ])
    if not result.success:
        return []
    seen: set[str] = set()
    issues = []
    for line in result.output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        ns = parts[0]
        # OBJECT column is like "Pod/pod-name"
        obj = parts[4] if len(parts) > 4 else ""
        pod_name = obj.split("/")[-1] if "/" in obj else obj
        key = f"{ns}/{pod_name}"
        if key in seen:
            continue
        seen.add(key)
        probe_type = "liveness" if "liveness" in line.lower() else "readiness"
        issues.append({
            "category": "probe",
            "severity": "warning",
            "resource": pod_name,
            "namespace": ns,
            "description": f"Pod '{pod_name}' in '{ns}' — {probe_type} probe failing",
            "fix_command": f"kubectl delete pod {pod_name} -n {ns}",
        })
    return issues


def get_tls_issues() -> list[dict]:
    """Expired or soon-expiring TLS secrets across all namespaces."""
    try:
        from agent.integrations.tls_collector import collect_tls_secrets
    except ImportError:
        return []
    result = collect_tls_secrets()
    secrets = result.get("data", {}).get("secrets", [])
    issues = []
    for sec in secrets:
        name = sec.get("name", "")
        ns   = sec.get("namespace", "")
        if sec.get("expired"):
            days = abs(sec.get("days_left", 0))
            issues.append({
                "category": "tls",
                "severity": "critical",
                "resource": name,
                "namespace": ns,
                "description": f"TLS secret '{name}' in '{ns}' EXPIRED {days} day(s) ago",
                "fix_command": f"kubectl delete secret {name} -n {ns}",
            })
        elif 0 < sec.get("days_left", 9999) <= 30:
            issues.append({
                "category": "tls",
                "severity": "warning",
                "resource": name,
                "namespace": ns,
                "description": f"TLS secret '{name}' in '{ns}' expires in {sec['days_left']} day(s)",
                "fix_command": f"kubectl delete secret {name} -n {ns}",
            })
    return issues


def get_pod_logs(pod: str, namespace: str, lines: int = 100) -> str:
    """
    Fetch current and (if crashing) previous logs for a pod.
    Returns combined string.
    """
    parts: list[str] = []

    current = run_kubectl(["logs", pod, "-n", namespace, f"--tail={lines}"])
    if current.success and current.output.strip():
        parts.append("=== CURRENT LOGS ===\n" + current.output)
    elif current.error.strip():
        parts.append(f"=== CURRENT LOGS (error) ===\n{current.error}")

    previous = run_kubectl(["logs", pod, "-n", namespace, "--previous", "--tail=50"])
    if previous.success and previous.output.strip():
        parts.append("=== PREVIOUS LOGS (before last crash) ===\n" + previous.output)

    return "\n\n".join(parts) if parts else "(no logs available)"


def get_pod_events(pod: str, namespace: str) -> str:
    """Return Kubernetes events for a specific pod."""
    result = run_kubectl([
        "get", "events",
        "-n", namespace,
        f"--field-selector=involvedObject.name={pod}",
        "--sort-by=.lastTimestamp",
    ])
    if not result.success or not result.output.strip():
        return f"(no events — {result.error[:100]})"
    return result.output


def describe_pod(pod: str, namespace: str) -> str:
    """Return full `kubectl describe pod` output."""
    result = run_kubectl(["describe", "pod", pod, "-n", namespace])
    if not result.success:
        return f"(describe failed — {result.error[:200]})"
    return result.output


def get_all_namespaces() -> list[str]:
    """Return all namespace names in the cluster."""
    result = run_kubectl(["get", "namespaces", "-o", "json"])
    if not result.success or not result.output.strip():
        log.warning("get_all_namespaces.failed", error=result.error[:200])
        return []
    try:
        data = json.loads(result.output)
        return [item["metadata"]["name"] for item in data.get("items", [])]
    except (json.JSONDecodeError, KeyError) as e:
        log.error("get_all_namespaces.parse_error", error=str(e))
        return []


def get_pods_in_namespace(namespace: str) -> list[PodInfo]:
    """Return full PodInfo for every pod in a specific namespace."""
    return get_pods(namespace)


def _get_cluster_name() -> str:
    """Return current kubectl context name."""
    result = run_kubectl(["config", "current-context"])
    if result.success and result.output.strip():
        return result.output.strip()
    return "unknown-cluster"


def _get_node_status() -> tuple[int, int]:
    """Return (total_nodes, healthy_nodes)."""
    result = run_kubectl(["get", "nodes", "-o", "json"])
    if not result.success or not result.output.strip():
        return 0, 0
    try:
        data  = json.loads(result.output)
        nodes = data.get("items", [])
        total = len(nodes)
        healthy = 0
        for node in nodes:
            for cond in node.get("status", {}).get("conditions", []):
                if cond.get("type") == "Ready" and cond.get("status") == "True":
                    healthy += 1
                    break
        return total, healthy
    except (json.JSONDecodeError, KeyError):
        return 0, 0


def _parse_memory_human(raw: str) -> str:
    """Convert Ki/Mi/Gi suffixes to a readable string."""
    try:
        if raw.endswith("Ki"):
            mb = int(raw[:-2]) // 1024
            return f"{mb} Mi" if mb < 1024 else f"{mb // 1024} Gi"
        return raw
    except Exception:
        return raw


def get_nodes_detail() -> list[NodeInfo]:
    """Return full NodeInfo for every node in the cluster."""
    result = run_kubectl(["get", "nodes", "-o", "json"])
    if not result.success or not result.output.strip():
        log.warning("get_nodes_detail.failed", error=result.error[:200])
        return []

    try:
        data  = json.loads(result.output)
        nodes: list[NodeInfo] = []

        for node in data.get("items", []):
            meta   = node.get("metadata", {})
            spec   = node.get("spec", {})
            status = node.get("status", {})
            labels = meta.get("labels", {})

            # Node ready status
            node_status = "NotReady"
            cond_strs: list[str] = []
            for cond in status.get("conditions", []):
                ctype  = cond.get("type", "?")
                cstatus = cond.get("status", "?")
                reason = cond.get("reason", "")
                cond_strs.append(f"{ctype}={cstatus}" + (f"({reason})" if reason else ""))
                if ctype == "Ready" and cstatus == "True":
                    node_status = "Ready"

            # Roles from labels
            roles = [
                key.split("/")[-1]
                for key in labels
                if key.startswith("node-role.kubernetes.io/")
            ] or ["worker"]

            node_info_obj = status.get("nodeInfo", {})
            capacity      = status.get("capacity", {})
            allocatable   = status.get("allocatable", {})

            nodes.append(NodeInfo(
                name               = meta.get("name", "?"),
                status             = node_status,
                roles              = roles,
                age                = _parse_age(node),
                kubelet_version    = node_info_obj.get("kubeletVersion", "?"),
                instance_type      = labels.get("node.kubernetes.io/instance-type",
                                                 labels.get("beta.kubernetes.io/instance-type", "?")),
                os_image           = node_info_obj.get("osImage", "?"),
                capacity_cpu       = capacity.get("cpu", "?"),
                capacity_memory    = _parse_memory_human(capacity.get("memory", "?")),
                allocatable_cpu    = allocatable.get("cpu", "?"),
                allocatable_memory = _parse_memory_human(allocatable.get("memory", "?")),
                conditions         = cond_strs,
            ))

        log.info("get_nodes_detail.done", count=len(nodes))
        return nodes

    except (json.JSONDecodeError, KeyError) as e:
        log.error("get_nodes_detail.parse_error", error=str(e))
        return []


def get_deployment_for_pod(pod_name: str, namespace: str) -> tuple[str, str] | tuple[None, None]:
    """
    Find the owner deployment/statefulset/daemonset for a pod.
    Returns (kind, name) or (None, None) if not found.
    """
    result = run_kubectl(["get", "pod", pod_name, "-n", namespace, "-o", "json"])
    if not result.success or not result.output.strip():
        return None, None
    try:
        pod   = json.loads(result.output)
        owners = pod.get("metadata", {}).get("ownerReferences", [])
        for owner in owners:
            kind = owner.get("kind", "")
            name = owner.get("name", "")
            # Pod → ReplicaSet → Deployment
            if kind == "ReplicaSet":
                rs = run_kubectl(["get", "replicaset", name, "-n", namespace, "-o", "json"])
                if rs.success:
                    rs_data  = json.loads(rs.output)
                    rs_owners = rs_data.get("metadata", {}).get("ownerReferences", [])
                    for rs_owner in rs_owners:
                        if rs_owner.get("kind") == "Deployment":
                            return "Deployment", rs_owner["name"]
            if kind in ("StatefulSet", "DaemonSet", "Deployment"):
                return kind, name
    except Exception:
        pass
    return None, None


def get_resource_yaml(kind: str, name: str, namespace: str) -> str:
    """Return the full YAML of a k8s resource."""
    result = run_kubectl(["get", kind.lower(), name, "-n", namespace, "-o", "yaml"])
    if result.success:
        return result.output
    return f"(failed to get {kind}/{name}: {result.error[:100]})"


def _is_pod_healthy(pod: PodInfo) -> bool:
    """A pod is healthy if Running and all containers are ready."""
    if pod.status != "Running":
        return False
    parts = pod.ready.split("/")
    if len(parts) == 2:
        try:
            return int(parts[0]) == int(parts[1]) and int(parts[1]) > 0
        except ValueError:
            pass
    return False


def get_cluster_overview() -> ClusterOverview:
    """
    Build a complete cluster snapshot in ONE kubectl call.

    Previously made N+2 calls (nodes + namespaces + one per namespace for pods).
    Now: kubectl get nodes,namespaces,pods -A -o json → single auth round-trip.
    """
    cluster_name = _get_cluster_name()
    generated_at = datetime.now(timezone.utc).isoformat()

    result = run_kubectl(["get", "nodes,namespaces,pods", "-A", "-o", "json"])
    if not result.success:
        log.warning("get_cluster_overview.failed", error=result.error[:200])
        return ClusterOverview(
            cluster_name=cluster_name, total_namespaces=0, namespaces=[],
            total_pods=0, healthy_pods=0, unhealthy_pods=0,
            total_nodes=0, healthy_nodes=0, generated_at=generated_at,
        )

    try:
        items = json.loads(result.output).get("items", [])
    except Exception as exc:
        log.warning("get_cluster_overview.parse_error", error=str(exc))
        items = []

    # Partition by kind
    raw_nodes:  list[dict] = []
    raw_ns:     list[dict] = []
    raw_pods:   list[dict] = []
    for obj in items:
        kind = obj.get("kind", "")
        if kind == "Node":
            raw_nodes.append(obj)
        elif kind == "Namespace":
            raw_ns.append(obj)
        elif kind == "Pod":
            raw_pods.append(obj)

    # Node health
    total_nodes = len(raw_nodes)
    healthy_n   = 0
    for node in raw_nodes:
        for cond in node.get("status", {}).get("conditions", []):
            if cond.get("type") == "Ready" and cond.get("status") == "True":
                healthy_n += 1
                break

    # Namespace names (if none returned, infer from pods)
    ns_names: list[str] = [
        obj["metadata"]["name"] for obj in raw_ns
        if obj.get("metadata", {}).get("name")
    ]
    if not ns_names:
        ns_names = sorted({p["metadata"].get("namespace", "default") for p in raw_pods})

    # Group pods by namespace
    pods_by_ns: dict[str, list[PodInfo]] = {ns: [] for ns in ns_names}
    for raw_pod in raw_pods:
        ns = raw_pod["metadata"].get("namespace", "default")
        if ns not in pods_by_ns:
            pods_by_ns[ns] = []
        pods_by_ns[ns].append(_pod_to_info(raw_pod))

    # Build NamespaceInfo list
    ns_infos: list[NamespaceInfo] = []
    total_pods = healthy_total = unhealthy_total = 0
    for ns_name in ns_names:
        pods      = pods_by_ns.get(ns_name, [])
        healthy   = sum(1 for p in pods if _is_pod_healthy(p))
        unhealthy = len(pods) - healthy
        ns_infos.append(NamespaceInfo(
            name=ns_name, pods=pods,
            total_pods=len(pods), healthy_pods=healthy, unhealthy_pods=unhealthy,
        ))
        total_pods      += len(pods)
        healthy_total   += healthy
        unhealthy_total += unhealthy

    log.info(
        "get_cluster_overview.done",
        cluster=cluster_name, namespaces=len(ns_infos),
        total_pods=total_pods, unhealthy=unhealthy_total,
    )
    return ClusterOverview(
        cluster_name     = cluster_name,
        total_namespaces = len(ns_infos),
        namespaces       = ns_infos,
        total_pods       = total_pods,
        healthy_pods     = healthy_total,
        unhealthy_pods   = unhealthy_total,
        total_nodes      = total_nodes,
        healthy_nodes    = healthy_n,
        generated_at     = generated_at,
    )


def apply_fix(command: str) -> KubectlResult:
    """
    Run a kubectl fix command suggested by the AI.
    Uses shlex.split so quoted JSON arguments (e.g. -p '[...]') are kept intact.
    """
    import shlex
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        parts = command.strip().split()

    if parts and parts[0] == "kubectl":
        parts = parts[1:]

    log.info("kubectl.apply_fix", command=command)
    return run_kubectl(parts)


# ---------------------------------------------------------------------------
# Resource metrics helpers
# ---------------------------------------------------------------------------

def _parse_cpu_millicores(raw: str) -> int:
    raw = raw.strip()
    if raw.endswith("m"):
        try:
            return int(raw[:-1])
        except ValueError:
            return 0
    try:
        return int(float(raw) * 1000)
    except ValueError:
        return 0


def _parse_memory_mebibytes(raw: str) -> int:
    raw = raw.strip()
    try:
        if raw.endswith("Ki"):
            return max(int(raw[:-2]) // 1024, 1)
        if raw.endswith("Mi"):
            return int(raw[:-2])
        if raw.endswith("Gi"):
            return int(raw[:-2]) * 1024
        if raw.endswith("Ti"):
            return int(raw[:-2]) * 1024 * 1024
        if raw.endswith("K") or raw.endswith("k"):
            return max(int(raw[:-1]) // 1024, 1)
        if raw.endswith("M"):
            return int(raw[:-1])
        if raw.endswith("G"):
            return int(raw[:-1]) * 1024
        return max(int(raw) // (1024 * 1024), 1)
    except ValueError:
        return 0


def get_node_metrics_top() -> list:
    """
    kubectl top nodes --no-headers
    Output columns: NAME  CPU(cores)  CPU%  MEMORY(bytes)  MEMORY%
    Returns list[NodeResourceMetrics].
    """
    from agent.core.models import NodeResourceMetrics

    result = run_kubectl(["top", "nodes", "--no-headers"])
    if not result.success:
        err = result.error.lower()
        if "metrics api not available" in err or "no metrics" in err or "servicenotfound" in err.lower():
            log.warning("get_node_metrics.metrics_server_missing")
        else:
            log.warning("get_node_metrics.failed", error=result.error[:200])
        return []

    metrics = []
    for line in result.output.strip().splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        name       = parts[0]
        cpu_usage  = parts[1]
        cpu_pct    = int(parts[2].rstrip("%")) if parts[2].rstrip("%").isdigit() else 0
        mem_usage  = parts[3]
        mem_pct    = int(parts[4].rstrip("%")) if parts[4].rstrip("%").isdigit() else 0

        if cpu_pct >= 85 or mem_pct >= 90:
            status = "critical"
        elif cpu_pct >= 70 or mem_pct >= 75:
            status = "warning"
        else:
            status = "healthy"

        metrics.append(NodeResourceMetrics(
            name           = name,
            cpu_usage      = cpu_usage,
            cpu_percent    = cpu_pct,
            memory_usage   = mem_usage,
            memory_percent = mem_pct,
            status         = status,
            pressure       = cpu_pct >= 85 or mem_pct >= 90,
        ))

    log.info("get_node_metrics.done", count=len(metrics))
    return metrics


def get_pod_metrics(namespace: str = "all") -> list:
    """
    Combines kubectl top pods (usage) + kubectl get pods -o json (limits).
    Returns list[PodResourceMetrics] with percentages relative to limits.
    """
    from agent.core.models import PodResourceMetrics

    # --- usage ---
    if namespace == "all":
        top_r = run_kubectl(["top", "pods", "-A", "--no-headers"])
    else:
        top_r = run_kubectl(["top", "pods", "-n", namespace, "--no-headers"])

    if not top_r.success:
        err = top_r.error.lower()
        if "metrics api not available" in err or "no metrics" in err:
            log.warning("get_pod_metrics.metrics_server_missing")
        else:
            log.warning("get_pod_metrics.top_failed", error=top_r.error[:200])
        return []

    # Parse: NAMESPACE  NAME  CPU(cores)  MEMORY(bytes)  (ns="all")
    #  or:   NAME  CPU(cores)  MEMORY(bytes)              (ns=specific)
    top_data: dict[tuple, dict] = {}
    for line in top_r.output.strip().splitlines():
        parts = line.split()
        if namespace == "all":
            if len(parts) < 4:
                continue
            ns_key, name, cpu_raw, mem_raw = parts[0], parts[1], parts[2], parts[3]
        else:
            if len(parts) < 3:
                continue
            ns_key, name, cpu_raw, mem_raw = namespace, parts[0], parts[1], parts[2]
        top_data[(ns_key, name)] = {
            "cpu_usage": cpu_raw,
            "mem_usage": mem_raw,
            "cpu_mc":    _parse_cpu_millicores(cpu_raw),
            "mem_mb":    _parse_memory_mebibytes(mem_raw),
        }

    if not top_data:
        return []

    # --- limits ---
    if namespace == "all":
        lim_r = run_kubectl(["get", "pods", "-A", "-o", "json"])
    else:
        lim_r = run_kubectl(["get", "pods", "-n", namespace, "-o", "json"])

    limits_map: dict[tuple, dict] = {}
    if lim_r.success and lim_r.output.strip():
        try:
            for pod in json.loads(lim_r.output).get("items", []):
                pns  = pod["metadata"].get("namespace", "default")
                pnam = pod["metadata"]["name"]
                cpu_lim_mc = mem_lim_mb = 0
                cpu_req_mc = mem_req_mb = 0
                cpu_lim_str = mem_lim_str = "none"
                cpu_req_str = mem_req_str = "none"
                has_limits = has_requests = False
                for c in pod.get("spec", {}).get("containers", []):
                    res  = c.get("resources", {})
                    lims = res.get("limits", {})
                    reqs = res.get("requests", {})
                    if lims:
                        has_limits = True
                        if lims.get("cpu"):
                            cpu_lim_mc += _parse_cpu_millicores(lims["cpu"])
                            cpu_lim_str  = lims["cpu"]
                        if lims.get("memory"):
                            mem_lim_mb  += _parse_memory_mebibytes(lims["memory"])
                            mem_lim_str  = lims["memory"]
                    if reqs:
                        has_requests = True
                        if reqs.get("cpu"):
                            cpu_req_mc += _parse_cpu_millicores(reqs["cpu"])
                            cpu_req_str  = reqs["cpu"]
                        if reqs.get("memory"):
                            mem_req_mb  += _parse_memory_mebibytes(reqs["memory"])
                            mem_req_str  = reqs["memory"]
                limits_map[(pns, pnam)] = {
                    "cpu_lim_mc": cpu_lim_mc, "mem_lim_mb": mem_lim_mb,
                    "cpu_req_mc": cpu_req_mc, "mem_req_mb": mem_req_mb,
                    "has_limits": has_limits, "has_requests": has_requests,
                    "cpu_limit":  cpu_lim_str, "mem_limit": mem_lim_str,
                    "cpu_request": cpu_req_str, "mem_request": mem_req_str,
                }
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning("get_pod_metrics.limits_parse_error", error=str(exc))

    # --- build PodResourceMetrics ---
    result_list = []
    for (ns_key, name), usage in top_data.items():
        lim  = limits_map.get((ns_key, name), {})
        cpu_mc   = usage["cpu_mc"]
        mem_mb   = usage["mem_mb"]
        cpu_lim  = lim.get("cpu_lim_mc", 0)
        mem_lim  = lim.get("mem_lim_mb", 0)
        has_lim  = lim.get("has_limits", False)

        cpu_pct = int(cpu_mc * 100 / cpu_lim) if cpu_lim > 0 else 0
        mem_pct = int(mem_mb * 100 / mem_lim) if mem_lim > 0 else 0

        cpu_at_risk = cpu_pct >= 70
        mem_at_risk = mem_pct >= 75
        no_lims     = not has_lim

        if no_lims:
            risk_type = "no_limits"
        elif cpu_at_risk and mem_at_risk:
            risk_type = "both"
        elif cpu_at_risk:
            risk_type = "cpu"
        elif mem_at_risk:
            risk_type = "memory"
        else:
            risk_type = "none"

        result_list.append(PodResourceMetrics(
            name           = name,
            namespace      = ns_key,
            cpu_usage      = usage["cpu_usage"],
            cpu_percent    = min(cpu_pct, 999),
            memory_usage   = usage["mem_usage"],
            memory_percent = min(mem_pct, 999),
            cpu_limit      = lim.get("cpu_limit", "none"),
            memory_limit   = lim.get("mem_limit", "none"),
            at_risk        = cpu_at_risk or mem_at_risk or no_lims,
            risk_type      = risk_type,
        ))

    log.info("get_pod_metrics.done", count=len(result_list))
    return result_list


def get_pod_limits(pod: str, namespace: str):
    """
    kubectl get pod <pod> -n <namespace> -o json
    Returns ResourceLimits with aggregated limits/requests across all containers.
    """
    from agent.core.models import ResourceLimits

    result = run_kubectl(["get", "pod", pod, "-n", namespace, "-o", "json"])
    if not result.success:
        return ResourceLimits(
            cpu_limit="none", memory_limit="none",
            cpu_request="none", memory_request="none",
            has_limits=False, has_requests=False,
        )
    try:
        data = json.loads(result.output)
        cpu_lims, mem_lims, cpu_reqs, mem_reqs = [], [], [], []
        for c in data.get("spec", {}).get("containers", []):
            res  = c.get("resources", {})
            lims = res.get("limits", {})
            reqs = res.get("requests", {})
            if lims.get("cpu"):
                cpu_lims.append(lims["cpu"])
            if lims.get("memory"):
                mem_lims.append(lims["memory"])
            if reqs.get("cpu"):
                cpu_reqs.append(reqs["cpu"])
            if reqs.get("memory"):
                mem_reqs.append(reqs["memory"])
        return ResourceLimits(
            cpu_limit    = cpu_lims[0]  if cpu_lims  else "none",
            memory_limit = mem_lims[0]  if mem_lims  else "none",
            cpu_request  = cpu_reqs[0]  if cpu_reqs  else "none",
            memory_request = mem_reqs[0] if mem_reqs else "none",
            has_limits   = bool(cpu_lims or mem_lims),
            has_requests = bool(cpu_reqs or mem_reqs),
        )
    except (json.JSONDecodeError, KeyError) as exc:
        log.warning("get_pod_limits.parse_error", pod=pod, error=str(exc))
        return ResourceLimits(
            cpu_limit="none", memory_limit="none",
            cpu_request="none", memory_request="none",
            has_limits=False, has_requests=False,
        )


def get_hpa_status(namespace: str = "all") -> list:
    """
    kubectl get hpa -A --no-headers
    Columns: NAMESPACE NAME REFERENCE TARGETS MINPODS MAXPODS REPLICAS AGE
    Returns list[HPAInfo].
    """
    from agent.core.models import HPAInfo

    if namespace == "all":
        result = run_kubectl(["get", "hpa", "-A", "--no-headers"])
    else:
        result = run_kubectl(["get", "hpa", "-n", namespace, "--no-headers"])

    if not result.success:
        return []

    hpas = []
    for line in result.output.strip().splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        ns        = parts[0]
        name      = parts[1]
        reference = parts[2]
        targets   = parts[3]  # e.g. "50%/80%" or "<unknown>/80%"
        min_r     = int(parts[4]) if parts[4].isdigit() else 0
        max_r     = int(parts[5]) if parts[5].isdigit() else 0
        cur_r     = int(parts[6]) if parts[6].isdigit() else 0

        cpu_cur = cpu_tgt = 0
        if "/" in targets:
            cur_s, tgt_s = targets.split("/", 1)
            try:
                cpu_cur = int(cur_s.rstrip("%"))
            except ValueError:
                pass
            try:
                cpu_tgt = int(tgt_s.rstrip("%"))
            except ValueError:
                pass

        hpas.append(HPAInfo(
            name=name, namespace=ns, target=reference,
            min_replicas=min_r, max_replicas=max_r, current_replicas=cur_r,
            cpu_target=cpu_tgt, cpu_current=cpu_cur,
        ))

    return hpas


def patch_resource_limits(
    deployment: str,
    namespace: str,
    cpu_limit: str,
    memory_limit: str,
) -> KubectlResult:
    """
    Patch the first container's resource limits on a deployment.
    Only call after explicit user approval.
    """
    patch = json.dumps([
        {"op": "replace",
         "path": "/spec/template/spec/containers/0/resources/limits/memory",
         "value": memory_limit},
        {"op": "replace",
         "path": "/spec/template/spec/containers/0/resources/limits/cpu",
         "value": cpu_limit},
    ])
    return run_kubectl([
        "patch", "deployment", deployment,
        "-n", namespace,
        "--type=json",
        f"-p={patch}",
    ])


def scale_deployment(deployment: str, namespace: str, replicas: int) -> KubectlResult:
    """
    kubectl scale deployment — only call after explicit user approval.
    """
    return run_kubectl([
        "scale", "deployment", deployment,
        "-n", namespace,
        f"--replicas={replicas}",
    ])


# ---------------------------------------------------------------------------
# TLS / Ingress discovery
# ---------------------------------------------------------------------------

def get_all_ingresses() -> list[IngressInfo]:
    """Return all Ingress resources across all namespaces."""
    r = run_kubectl(["get", "ingress", "-A", "-o", "json"])
    if not r.success or not r.output.strip():
        log.warning("get_all_ingresses.failed", error=r.error[:200])
        return []

    results: list[IngressInfo] = []
    try:
        for ing in json.loads(r.output).get("items", []):
            meta = ing["metadata"]
            spec = ing.get("spec", {})

            # Collect all TLS entries
            tls_entries = spec.get("tls", [])
            tls_enabled = bool(tls_entries)
            tls_secret  = tls_entries[0].get("secretName", "") if tls_entries else ""

            # External address
            lb_ingress = ing.get("status", {}).get("loadBalancer", {}).get("ingress", [])
            address = ""
            if lb_ingress:
                address = lb_ingress[0].get("ip") or lb_ingress[0].get("hostname") or ""

            # One IngressInfo per rule/host
            rules = spec.get("rules", [])
            if not rules:
                results.append(IngressInfo(
                    name=meta["name"], namespace=meta["namespace"],
                    domain="", tls_enabled=tls_enabled, tls_secret=tls_secret,
                    backend_service="", address=address, age=_parse_age(ing),
                ))
                continue

            for rule in rules:
                host = rule.get("host", "")
                # Pick first backend service from paths
                backend = ""
                for path in rule.get("http", {}).get("paths", []):
                    svc = (path.get("backend", {}).get("service", {})
                           or path.get("backend", {}))
                    backend = svc.get("name", "") or svc.get("serviceName", "")
                    if backend:
                        break

                # Match TLS secret for this host
                host_secret = tls_secret
                for tls in tls_entries:
                    if host in tls.get("hosts", []):
                        host_secret = tls.get("secretName", "")
                        break

                results.append(IngressInfo(
                    name            = meta["name"],
                    namespace       = meta["namespace"],
                    domain          = host,
                    tls_enabled     = tls_enabled,
                    tls_secret      = host_secret,
                    backend_service = backend,
                    address         = address,
                    age             = _parse_age(ing),
                ))
    except (json.JSONDecodeError, KeyError) as e:
        log.error("get_all_ingresses.parse_error", error=str(e))

    log.info("get_all_ingresses.done", count=len(results))
    return results


def get_tls_secret(secret_name: str, namespace: str) -> TLSSecretInfo | None:
    """
    Fetch a TLS secret and decode the certificate to extract issuer,
    expiry, cert type, and domain.
    """
    r = run_kubectl(["get", "secret", secret_name, "-n", namespace, "-o", "json"])
    if not r.success or not r.output.strip():
        log.warning("get_tls_secret.failed", name=secret_name, ns=namespace)
        return None

    try:
        secret = json.loads(r.output)
        data   = secret.get("data", {})
        tls_crt_b64 = data.get("tls.crt", "")
        if not tls_crt_b64:
            return TLSSecretInfo(
                name=secret_name, namespace=namespace,
                cert_type="missing", days_until_expiry=-1,
                is_expired=True,
            )

        import base64
        cert_pem = base64.b64decode(tls_crt_b64).decode("utf-8", errors="replace")
        return _parse_cert_pem(secret_name, namespace, cert_pem)

    except Exception as e:
        log.error("get_tls_secret.error", name=secret_name, error=str(e))
        return None


def _parse_cert_pem(name: str, namespace: str, cert_pem: str) -> TLSSecretInfo:
    """Parse a PEM certificate string into TLSSecretInfo."""
    from datetime import datetime, timezone, timedelta

    issuer    = "unknown"
    expiry_dt: datetime | None = None
    domain    = ""
    cert_type = "unknown"

    # Try cryptography library first
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())

        # Issuer
        issuer_obj = cert.issuer
        org = issuer_obj.get_attributes_for_oid(
            x509.NameOID.ORGANIZATION_NAME
        )
        cn = issuer_obj.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        issuer_str = (org[0].value if org else cn[0].value if cn else "unknown")
        issuer = issuer_str

        # Expiry
        expiry_dt = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") \
                    else cert.not_valid_after.replace(tzinfo=timezone.utc)

        # Domain (CN or first SAN)
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            dns_names = san.value.get_values_for_type(x509.DNSName)
            domain = dns_names[0] if dns_names else ""
        except Exception:
            subj_cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
            domain = subj_cn[0].value if subj_cn else ""

    except ImportError:
        # Fall back to openssl subprocess
        import subprocess
        try:
            r = subprocess.run(
                ["openssl", "x509", "-noout", "-subject", "-issuer", "-enddate"],
                input=cert_pem, capture_output=True, text=True, timeout=8,
            )
            for line in r.stdout.splitlines():
                if line.startswith("issuer="):
                    issuer = line.split("=", 1)[1].strip()
                if line.startswith("notAfter="):
                    raw = line.split("=", 1)[1].strip()
                    from email.utils import parsedate_to_datetime
                    try:
                        expiry_dt = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
                if "CN=" in line and not domain:
                    for part in line.split("/"):
                        if part.startswith("CN="):
                            domain = part[3:]
        except Exception:
            pass

    # Cert type classification
    issuer_lower = issuer.lower()
    if "let's encrypt" in issuer_lower or "letsencrypt" in issuer_lower or "r3" in issuer_lower:
        cert_type = "Let's Encrypt"
    elif "self" in issuer_lower or issuer == domain:
        cert_type = "self-signed"
    elif "digicert" in issuer_lower or "comodo" in issuer_lower or "sectigo" in issuer_lower:
        cert_type = "commercial CA"
    elif "amazon" in issuer_lower:
        cert_type = "AWS ACM"
    elif "google" in issuer_lower:
        cert_type = "Google CA"
    else:
        cert_type = "other CA"

    now = datetime.now(timezone.utc)
    if expiry_dt:
        days = (expiry_dt - now).days
        expiry_str = expiry_dt.strftime("%Y-%m-%d")
    else:
        days = -1
        expiry_str = "unknown"

    return TLSSecretInfo(
        name              = name,
        namespace         = namespace,
        domain            = domain,
        issuer            = issuer,
        expiry_date       = expiry_str,
        days_until_expiry = days,
        is_expired        = days < 0 if days != -1 else False,
        is_expiring_soon  = 0 <= days <= 30 if days != -1 else False,
        cert_type         = cert_type,
    )


def get_cert_manager_certificates() -> list[CertInfo]:
    """Return all cert-manager Certificate resources across all namespaces."""
    r = run_kubectl(["get", "certificates", "-A", "-o", "json"])
    if not r.success:
        log.info("get_cert_manager_certificates.unavailable")
        return []

    results: list[CertInfo] = []
    try:
        for cert in json.loads(r.output).get("items", []):
            meta     = cert["metadata"]
            spec     = cert.get("spec", {})
            status   = cert.get("status", {})
            cond_map = {c["type"]: c for c in status.get("conditions", [])}
            ready_c  = cond_map.get("Ready", {})
            ready    = ready_c.get("status") == "True"
            message  = ready_c.get("message", "")
            dns_names = spec.get("dnsNames", [])
            domain   = dns_names[0] if dns_names else spec.get("commonName", "")
            expiry   = (status.get("notAfter") or "")[:10]
            issuer   = spec.get("issuerRef", {}).get("name", "")

            results.append(CertInfo(
                name      = meta["name"],
                namespace = meta["namespace"],
                domain    = domain,
                ready     = ready,
                status    = "Ready" if ready else "NotReady",
                message   = message,
                expiry    = expiry,
                issuer    = issuer,
            ))
    except (json.JSONDecodeError, KeyError) as e:
        log.error("get_cert_manager_certificates.parse_error", error=str(e))

    log.info("get_cert_manager_certificates.done", count=len(results))
    return results


def get_cert_manager_issuers() -> list[str]:
    """Return names of all ClusterIssuers."""
    r = run_kubectl(["get", "clusterissuers", "-o", "json"])
    if not r.success:
        return []
    try:
        return [
            item["metadata"]["name"]
            for item in json.loads(r.output).get("items", [])
        ]
    except (json.JSONDecodeError, KeyError):
        return []


def check_ingress_controller() -> dict:
    """
    Detect the running ingress controller type, namespace, and status.
    Returns: {type, namespace, status, pods}
    """
    SELECTORS = [
        ("nginx",   "kube-system",   "app.kubernetes.io/name=ingress-nginx"),
        ("nginx",   "ingress-nginx", "app.kubernetes.io/name=ingress-nginx"),
        ("nginx",   "kube-system",   "app=ingress-nginx"),
        ("nginx",   "default",       "app=nginx-ingress"),
        ("traefik", "kube-system",   "app.kubernetes.io/name=traefik"),
        ("traefik", "traefik",       "app.kubernetes.io/name=traefik"),
        ("haproxy", "haproxy-controller", "run=haproxy-ingress"),
    ]

    for ctrl_type, ns, selector in SELECTORS:
        r = run_kubectl(["get", "pods", "-n", ns, "-l", selector,
                         "-o", "json"])
        if not r.success:
            continue
        items = json.loads(r.output).get("items", [])
        if not items:
            continue

        pods_ok   = []
        pods_bad  = []
        for pod in items:
            phase = pod.get("status", {}).get("phase", "Unknown")
            cs    = pod.get("status", {}).get("containerStatuses", [])
            ready = all(c.get("ready", False) for c in cs)
            name  = pod["metadata"]["name"]
            (pods_ok if ready else pods_bad).append(name)

        status = "running" if pods_ok and not pods_bad else (
                 "degraded" if pods_ok else "down")
        log.info("check_ingress_controller.found", type=ctrl_type, ns=ns, status=status)
        return {
            "type":      ctrl_type,
            "namespace": ns,
            "status":    status,
            "pods_ok":   pods_ok,
            "pods_bad":  pods_bad,
            "found":     True,
        }

    log.info("check_ingress_controller.not_found")
    return {"type": "none", "namespace": "", "status": "not_found",
            "pods_ok": [], "pods_bad": [], "found": False}


def run_tls_fix(fix_type: str, params: dict) -> KubectlResult:
    """
    Execute a TLS fix by fix_type.

    fix_type options:
      restart_cert      — delete the TLS secret so cert-manager re-issues it
      annotate_ingress  — add cert-manager annotation to an ingress
      apply_manifest    — kubectl apply -f <file>
      delete_challenge  — delete stuck ACME challenge
      restart_issuer    — annotate clusterissuer to trigger re-check
    """
    log.info("run_tls_fix", fix_type=fix_type, params=params)

    if fix_type == "restart_cert":
        name = params["name"]
        ns   = params["namespace"]
        log.info("kubectl.tls_fix.restart_cert", name=name, ns=ns)
        return run_kubectl(["delete", "secret", name, "-n", ns, "--ignore-not-found"])

    if fix_type == "annotate_ingress":
        ing  = params["ingress"]
        ns   = params["namespace"]
        ann  = params.get("annotation", "cert-manager.io/cluster-issuer=letsencrypt-prod")
        log.info("kubectl.tls_fix.annotate_ingress", ingress=ing, ns=ns)
        return run_kubectl(["annotate", "ingress", ing, "-n", ns, ann, "--overwrite"])

    if fix_type == "apply_manifest":
        path = params["path"]
        log.info("kubectl.tls_fix.apply_manifest", path=path)
        return run_kubectl(["apply", "-f", path])

    if fix_type == "delete_challenge":
        name = params.get("name", "--all")
        ns   = params["namespace"]
        cmd  = ["delete", "challenge", name, "-n", ns] if name != "--all" \
               else ["delete", "challenges", "--all", "-n", ns]
        log.info("kubectl.tls_fix.delete_challenge", ns=ns)
        return run_kubectl(cmd)

    if fix_type == "restart_issuer":
        name = params["name"]
        log.info("kubectl.tls_fix.restart_issuer", name=name)
        return run_kubectl([
            "annotate", "clusterissuer", name,
            f"cert-manager.io/force-renew={__import__('time').time()}",
            "--overwrite",
        ])

    return KubectlResult(
        command=[], output="", success=False,
        error=f"unknown fix_type: {fix_type}", duration_ms=0.0,
    )


def apply_patch(
    kind: str,
    name: str,
    namespace: str,
    patch_type: str,
    patch: list | dict,
) -> KubectlResult:
    """
    Apply a kubectl patch by passing the JSON directly — no shell serialisation.
    patch_type: 'json' | 'merge' | 'strategic'
    patch: the patch object (list for json-patch, dict for merge/strategic)
    """
    patch_str = json.dumps(patch)
    cmd = [
        "patch", kind.lower(), name,
        "-n", namespace,
        f"--type={patch_type}",
        "-p", patch_str,
    ]
    log.info("kubectl.apply_patch", kind=kind, name=name, namespace=namespace, patch_type=patch_type)
    return run_kubectl(cmd)


# ---------------------------------------------------------------------------
# Log analysis
# ---------------------------------------------------------------------------

_ERROR_RE = re.compile(
    r"(\bERROR\b|\bFATAL\b|\bPANIC\b|\bpanic\b|\bException\b"
    r"|connection refused|connection reset|timed out|timeout"
    r"|out of memory|cannot allocate|OOMKilled"
    r"|failed to|Failed to|Error:|error:|WARN\b|\bWARNING\b"
    r"|CrashLoopBackOff|BackOff|segfault|killed)",
    re.IGNORECASE,
)


def _fetch_pod_errors(pod: PodInfo, tail: int) -> dict | None:
    """Fetch logs for one pod and return error lines. Returns None if no errors."""
    raw = get_pod_logs(pod.name, pod.namespace, lines=tail)
    if not raw or raw.startswith("(no logs"):
        return None
    error_lines = [
        line.strip()
        for line in raw.splitlines()
        if _ERROR_RE.search(line) and line.strip()
    ]
    if not error_lines:
        return None
    return {
        "pod":         pod.name,
        "namespace":   pod.namespace,
        "status":      pod.status,
        "error_lines": error_lines[:10],
        "raw_sample":  "\n".join(error_lines[:20]),
    }


def get_all_pod_logs_errors(namespace: str = "all", tail: int = 50) -> list[dict]:
    """
    Fetch logs for all running pods in parallel and return only pods with errors.
    Uses up to 6 parallel workers to avoid being slow on large clusters.
    """
    pods = get_pods(namespace)
    running = [p for p in pods if p.status in ("Running", "CrashLoopBackOff", "Error", "OOMKilled")]

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_pod_errors, pod, tail): pod for pod in running}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception:
                pass

    results.sort(key=lambda x: len(x["error_lines"]), reverse=True)
    return results


def get_cluster_events(namespace: str = "all") -> list[dict]:
    """
    Return Warning-type cluster events sorted by last timestamp.
    Covers both node and pod events.
    """
    args = [
        "get", "events",
        "--sort-by=.lastTimestamp",
        "--no-headers",
        "-o", (
            "custom-columns="
            "TYPE:.type,"
            "REASON:.reason,"
            "NS:.involvedObject.namespace,"
            "KIND:.involvedObject.kind,"
            "NAME:.involvedObject.name,"
            "MSG:.message"
        ),
    ]
    if namespace == "all":
        args.append("-A")
    else:
        args += ["-n", namespace]

    result = run_kubectl(args)
    if not result.success or not result.output.strip():
        return []

    events: list[dict] = []
    for line in result.output.strip().splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        ev_type, reason, ns, kind, name, msg = parts
        if ev_type.lower() != "warning":
            continue
        events.append({
            "type":      ev_type,
            "reason":    reason,
            "namespace": ns,
            "kind":      kind,
            "name":      name,
            "message":   msg.strip(),
        })

    return events[-50:]


# ---------------------------------------------------------------------------
# Helm
# ---------------------------------------------------------------------------

def get_helm_releases() -> list[dict]:
    """
    List all Helm releases across every namespace.
    Returns empty list if helm is not installed.
    """
    if not shutil.which("helm"):
        log.debug("helm.not_found")
        return []

    result = subprocess.run(
        ["helm", "list", "-A", "--no-headers", "--output", "json"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []

    try:
        raw = json.loads(result.stdout)
    except Exception:
        return []

    releases: list[dict] = []
    for r in raw:
        status = r.get("status", "unknown").lower()
        is_ok  = status == "deployed"
        fix    = None
        if status in ("failed", "pending-upgrade"):
            fix = f"helm rollback {r.get('name')} -n {r.get('namespace')}"
        elif status == "pending-install":
            fix = f"helm uninstall {r.get('name')} -n {r.get('namespace')}"

        releases.append({
            "name":        r.get("name", ""),
            "namespace":   r.get("namespace", ""),
            "chart":       r.get("chart", ""),
            "app_version": r.get("app_version", ""),
            "status":      status,
            "updated":     r.get("updated", ""),
            "healthy":     is_ok,
            "fix_command": fix,
        })

    log.info("helm.list_done", total=len(releases),
             unhealthy=sum(1 for r in releases if not r["healthy"]))
    return releases


# ---------------------------------------------------------------------------
# Security audit helpers
# ---------------------------------------------------------------------------

_SENSITIVE_ENV_RE = re.compile(
    r"(PASSWORD|SECRET|KEY|TOKEN|API_KEY|APIKEY|PASSWD|CREDENTIAL|AUTH|PRIVATE)",
    re.IGNORECASE,
)


def _get_pods_json_all(namespace: str = "all") -> list[dict]:
    """Fetch full pod specs as JSON — shared by all security checks."""
    args = ["get", "pods", "-o", "json"]
    if namespace == "all":
        args.append("-A")
    else:
        args.extend(["-n", namespace])
    result = run_kubectl(args)
    if not result.success:
        return []
    try:
        return json.loads(result.output).get("items", [])
    except Exception:
        return []


def get_privileged_pods(namespace: str = "all") -> list[dict]:
    """Find containers with privileged / allowPrivilegeEscalation / runAsRoot."""
    findings: list[dict] = []
    for pod in _get_pods_json_all(namespace):
        pod_name = pod["metadata"]["name"]
        pod_ns   = pod["metadata"]["namespace"]
        pod_sc   = pod.get("spec", {}).get("securityContext", {})
        for ctr in pod.get("spec", {}).get("containers", []):
            sc     = ctr.get("securityContext", {})
            issues = []
            if sc.get("privileged"):
                issues.append("privileged=true")
            if sc.get("allowPrivilegeEscalation"):
                issues.append("allowPrivilegeEscalation=true")
            ru = sc.get("runAsUser", pod_sc.get("runAsUser"))
            if ru == 0:
                issues.append("runAsUser=0 (root)")
            if issues:
                findings.append({
                    "pod": pod_name, "namespace": pod_ns,
                    "container": ctr["name"], "issues": issues,
                })
    log.info("security.privileged_pods", count=len(findings))
    return findings


def get_secrets_in_env(namespace: str = "all") -> list[dict]:
    """Find pods with sensitive values hardcoded as plain env vars (not secretRef)."""
    findings: list[dict] = []
    for pod in _get_pods_json_all(namespace):
        pod_name = pod["metadata"]["name"]
        pod_ns   = pod["metadata"]["namespace"]
        for ctr in pod.get("spec", {}).get("containers", []):
            for env in ctr.get("env", []):
                name = env.get("name", "")
                if _SENSITIVE_ENV_RE.search(name) and "value" in env and "valueFrom" not in env:
                    findings.append({
                        "pod": pod_name, "namespace": pod_ns,
                        "container": ctr["name"], "env_var": name,
                    })
    log.info("security.secrets_in_env", count=len(findings))
    return findings


def get_rbac_issues() -> list[dict]:
    """Detect dangerous RBAC bindings: anonymous access, cluster-admin misuse, wildcards."""
    findings: list[dict] = []

    crb = run_kubectl(["get", "clusterrolebindings", "-o", "json"])
    if crb.success:
        try:
            for b in json.loads(crb.output).get("items", []):
                bname    = b["metadata"]["name"]
                role_ref = b.get("roleRef", {}).get("name", "")
                for subj in b.get("subjects", []) or []:
                    sname = subj.get("name", "")
                    if sname in ("system:anonymous", "system:unauthenticated"):
                        findings.append({
                            "binding": bname, "kind": "ClusterRoleBinding",
                            "role": role_ref, "subject": sname,
                            "namespace": "cluster-wide",
                            "issue": f"Bound to {sname} — anyone can access cluster",
                        })
                    elif role_ref == "cluster-admin" and not sname.startswith("system:"):
                        findings.append({
                            "binding": bname, "kind": "ClusterRoleBinding",
                            "role": "cluster-admin", "subject": sname,
                            "namespace": "cluster-wide",
                            "issue": f"cluster-admin granted to non-system account: {sname}",
                        })
        except Exception:
            pass

    cr = run_kubectl(["get", "clusterroles", "-o", "json"])
    if cr.success:
        try:
            for role in json.loads(cr.output).get("items", []):
                rname = role["metadata"]["name"]
                if rname.startswith("system:"):
                    continue
                for rule in role.get("rules", []):
                    if "*" in rule.get("verbs", []) and "*" in rule.get("resources", []):
                        findings.append({
                            "binding": rname, "kind": "ClusterRole",
                            "role": rname, "subject": "N/A",
                            "namespace": "cluster-wide",
                            "issue": "Wildcard permissions (verbs=[*] resources=[*])",
                        })
                        break
        except Exception:
            pass

    log.info("security.rbac_issues", count=len(findings))
    return findings


def get_network_policy_gaps(namespace: str = "all") -> list[dict]:
    """Find namespaces that have no NetworkPolicy — pods can communicate freely."""
    ns_res = run_kubectl(["get", "namespaces", "-o", "json"])
    if not ns_res.success:
        return []
    try:
        skip = {"kube-system", "kube-public", "kube-node-lease"}
        all_ns = [
            n["metadata"]["name"]
            for n in json.loads(ns_res.output).get("items", [])
            if n["metadata"]["name"] not in skip
        ]
    except Exception:
        return []

    np_res = run_kubectl(["get", "networkpolicies", "-A", "-o", "json"])
    protected: set[str] = set()
    if np_res.success:
        try:
            for np in json.loads(np_res.output).get("items", []):
                protected.add(np["metadata"]["namespace"])
        except Exception:
            pass

    findings = []
    for ns_name in all_ns:
        if namespace != "all" and ns_name != namespace:
            continue
        if ns_name not in protected:
            findings.append({
                "namespace": ns_name,
                "issue":     "No NetworkPolicy — unrestricted pod-to-pod traffic",
                "risk":      "high" if ns_name in {"production", "prod", "default"} else "medium",
            })

    log.info("security.network_policy_gaps", count=len(findings))
    return findings


def get_pods_running_as_root(namespace: str = "all") -> list[dict]:
    """Find containers explicitly running as root or with no securityContext."""
    findings: list[dict] = []
    for pod in _get_pods_json_all(namespace):
        pod_name = pod["metadata"]["name"]
        pod_ns   = pod["metadata"]["namespace"]
        pod_sc   = pod.get("spec", {}).get("securityContext", {})
        for ctr in pod.get("spec", {}).get("containers", []):
            sc  = ctr.get("securityContext", {})
            ru  = sc.get("runAsUser", pod_sc.get("runAsUser"))
            rnr = sc.get("runAsNonRoot", pod_sc.get("runAsNonRoot"))
            if ru == 0:
                findings.append({
                    "pod": pod_name, "namespace": pod_ns,
                    "container": ctr["name"],
                    "issue": "Explicitly running as root (runAsUser=0)",
                    "explicit": True,
                })
            elif ru is None and rnr is None:
                findings.append({
                    "pod": pod_name, "namespace": pod_ns,
                    "container": ctr["name"],
                    "issue": "No securityContext — may run as root by default",
                    "explicit": False,
                })

    log.info("security.pods_as_root", count=len(findings))
    return findings


_INGRESS_CONTROLLER_NAMES = {
    "ingress-nginx", "ingress-nginx-controller",
    "istio-ingressgateway", "istio-ingress",
    "traefik", "traefik-web", "traefik-websecure",
    "ambassador", "ambassador-admin",
    "kong", "kong-proxy",
    "haproxy-ingress", "nginx-ingress-controller",
    "envoy", "contour",
    "aws-load-balancer-controller",
}
_STANDARD_PORTS = {80, 443, 8080, 8443}


def _detect_ingress_controller(sname: str, labels: dict) -> tuple[bool, str]:
    """Return (is_ingress, controller_type) by matching service name and labels."""
    name_lower = sname.lower()
    for known in _INGRESS_CONTROLLER_NAMES:
        if known in name_lower:
            return True, known
    # Check common labels used by ingress controllers
    for label_key in ("app.kubernetes.io/name", "app", "app.kubernetes.io/component"):
        label_val = labels.get(label_key, "").lower()
        for known in ("ingress-nginx", "istio-ingressgateway", "traefik",
                      "ambassador", "kong", "contour"):
            if known in label_val:
                return True, label_val
    return False, ""


def get_exposed_services(namespace: str = "all") -> list[dict]:
    """
    Find LoadBalancer and NodePort services.

    Returns enriched data including:
    - is_ingress: whether this is a known ingress controller
    - ingress_type: name of the ingress controller
    - port_numbers: list of int port numbers
    - non_standard_ports: ports outside {80, 443, 8080, 8443}
    """
    args = ["get", "services", "-o", "json"]
    if namespace == "all":
        args.append("-A")
    else:
        args.extend(["-n", namespace])
    result = run_kubectl(args)
    if not result.success:
        return []

    findings: list[dict] = []
    try:
        for svc in json.loads(result.output).get("items", []):
            sname  = svc["metadata"]["name"]
            sns    = svc["metadata"]["namespace"]
            stype  = svc["spec"].get("type", "ClusterIP")
            if stype not in ("LoadBalancer", "NodePort"):
                continue
            labels = svc["metadata"].get("labels", {})
            ext_ip = ""
            if stype == "LoadBalancer":
                ing = svc.get("status", {}).get("loadBalancer", {}).get("ingress", [])
                if ing:
                    ext_ip = ing[0].get("hostname") or ing[0].get("ip", "")

            port_specs   = svc["spec"].get("ports", [])
            port_numbers = [p.get("port", 0) for p in port_specs]
            ports_str    = ", ".join(
                f"{p.get('port')}/{p.get('protocol','TCP')}" for p in port_specs
            )
            non_standard = [p for p in port_numbers if p not in _STANDARD_PORTS]
            is_ingress, ingress_type = _detect_ingress_controller(sname, labels)

            sensitive = {"kube-system", "default", "production", "prod"}
            findings.append({
                "service":           sname,
                "namespace":         sns,
                "type":              stype,
                "ports":             ports_str,
                "port_numbers":      port_numbers,
                "non_standard_ports": non_standard,
                "external_ip":       ext_ip,
                "is_ingress":        is_ingress,
                "ingress_type":      ingress_type,
                "risk":              "high" if sns in sensitive else "medium",
            })
    except Exception:
        pass

    log.info("security.exposed_services", count=len(findings))
    return findings


# ---------------------------------------------------------------------------
# Cost analysis helpers
# ---------------------------------------------------------------------------

def parse_cpu(value: str) -> float:
    """Convert K8s CPU string to float cores. '500m'->0.5, '2'->2.0, '1500m'->1.5"""
    if not value or value in ("0", ""):
        return 0.0
    value = value.strip()
    if value.endswith("m"):
        return round(int(value[:-1]) / 1000, 4)
    try:
        return float(value)
    except ValueError:
        return 0.0


def parse_memory(value: str) -> float:
    """Convert K8s memory string to float GB. '512Mi'->0.5, '2Gi'->2.0"""
    if not value or value in ("0", ""):
        return 0.0
    value = value.strip()
    if value.endswith("Ki"):
        return round(int(value[:-2]) / 1_048_576, 4)
    if value.endswith("Mi"):
        return round(int(value[:-2]) / 1024, 4)
    if value.endswith("Gi"):
        return round(float(value[:-2]), 4)
    if value.endswith("Ti"):
        return round(float(value[:-2]) * 1024, 4)
    if value.endswith("K"):
        return round(int(value[:-1]) / 1_000_000, 4)
    if value.endswith("M"):
        return round(int(value[:-1]) / 1000, 4)
    if value.endswith("G"):
        return round(float(value[:-1]), 4)
    # raw bytes
    try:
        return round(int(value) / (1024 ** 3), 4)
    except ValueError:
        return 0.0


def get_nodes_and_pods(namespace: str = "all") -> tuple[list[dict], list[dict]]:
    """
    Fetch nodes AND pods in a single kubectl call to avoid double auth round-trips.

    Returns (nodes_list, pods_list) — same shapes as the old separate functions.
    """
    args = ["get", "nodes,pods", "-o", "json"]
    if namespace == "all":
        args.append("-A")
    else:
        args.extend(["-n", namespace])

    result = run_kubectl(args)
    if not result.success:
        log.warning("cost.fetch_failed", error=result.error[:120])
        return [], []

    nodes: list[dict] = []
    pods:  list[dict] = []

    try:
        data  = json.loads(result.output)
        items = data.get("items", [])
        for obj in items:
            kind = obj.get("kind", "")

            if kind == "Node":
                labels = obj["metadata"].get("labels", {})
                inst   = (
                    labels.get("node.kubernetes.io/instance-type")
                    or labels.get("beta.kubernetes.io/instance-type")
                    or "unknown"
                )
                region = (
                    labels.get("topology.kubernetes.io/region")
                    or labels.get("failure-domain.beta.kubernetes.io/region")
                    or "us-east-1"
                )
                alloc  = obj.get("status", {}).get("allocatable", {})
                nodes.append({
                    "name":          obj["metadata"]["name"],
                    "instance_type": inst,
                    "cpu":           parse_cpu(alloc.get("cpu", "0")),
                    "memory_gb":     parse_memory(alloc.get("memory", "0")),
                    "region":        region,
                })

            elif kind == "Pod":
                phase = obj.get("status", {}).get("phase", "")
                if phase not in ("Running", "Pending", ""):
                    continue
                name      = obj["metadata"]["name"]
                ns        = obj["metadata"].get("namespace", "default")
                node_name = obj["spec"].get("nodeName", "")
                owners    = obj["metadata"].get("ownerReferences", [])
                deployment = ""
                for owner in owners:
                    okind = owner.get("kind", "")
                    if okind == "ReplicaSet":
                        rs = owner["name"]
                        deployment = "-".join(rs.split("-")[:-1]) or rs
                        break
                    if okind in ("Deployment", "StatefulSet", "DaemonSet"):
                        deployment = owner["name"]
                        break
                if not deployment:
                    deployment = name
                total_cpu = total_mem = 0.0
                for container in obj["spec"].get("containers", []):
                    req = container.get("resources", {}).get("requests", {})
                    total_cpu += parse_cpu(req.get("cpu", "0"))
                    total_mem += parse_memory(req.get("memory", "0"))
                pods.append({
                    "pod":            name,
                    "namespace":      ns,
                    "deployment":     deployment,
                    "cpu_request":    total_cpu,
                    "memory_request": total_mem,
                    "node_name":      node_name,
                })

    except Exception as exc:
        log.warning("cost.parse_error", error=str(exc))

    log.info("cost.fetched", nodes=len(nodes), pods=len(pods))
    return nodes, pods


def get_node_instance_types() -> list[dict]:
    """Kept for backwards-compat. Use get_nodes_and_pods() for cost scans."""
    nodes, _ = get_nodes_and_pods()
    return nodes


def get_pod_resource_requests(namespace: str = "all") -> list[dict]:
    """Kept for backwards-compat. Use get_nodes_and_pods() for cost scans."""
    _, pods = get_nodes_and_pods(namespace)
    return pods


_TOP_TIMEOUT = 15  # kubectl top polls metrics-server; give it a real chance

def get_pod_actual_usage(namespace: str = "all") -> list[dict]:
    """Run kubectl top pods and return actual CPU/memory usage per pod."""
    args = ["top", "pods", "--no-headers"]
    if namespace == "all":
        args.append("-A")
    else:
        args.extend(["-n", namespace])
    result = run_kubectl(args, timeout=_TOP_TIMEOUT)
    if not result.success:
        return []
    usage: list[dict] = []
    try:
        for line in result.output.strip().splitlines():
            parts = line.split()
            if namespace == "all" and len(parts) >= 4:
                ns, pod_name, cpu_str, mem_str = parts[0], parts[1], parts[2], parts[3]
            elif namespace != "all" and len(parts) >= 3:
                ns, pod_name, cpu_str, mem_str = namespace, parts[0], parts[1], parts[2]
            else:
                continue
            usage.append({
                "pod":           pod_name,
                "namespace":     ns,
                "cpu_actual":    parse_cpu(cpu_str),
                "memory_actual": parse_memory(mem_str),
            })
    except Exception:
        pass
    log.info("cost.pod_usage", count=len(usage))
    return usage


# ---------------------------------------------------------------------------
# Multi-cluster context management
# ---------------------------------------------------------------------------

def get_all_contexts() -> list:
    """Parse ~/.kube/config and return all configured contexts as ClusterContext objects."""
    from agent.core.models import ClusterContext

    result = run_kubectl(["config", "view", "-o", "json"])
    if not result.success:
        return []

    try:
        cfg         = json.loads(result.output)
        current_ctx = cfg.get("current-context", "")

        clusters_map: dict[str, str] = {
            c["name"]: c.get("cluster", {}).get("server", "")
            for c in cfg.get("clusters", [])
        }
        context_map: dict[str, dict] = {
            c["name"]: c.get("context", {})
            for c in cfg.get("contexts", [])
        }

        contexts = []
        for ctx_name, ctx_data in context_map.items():
            provider = _detect_provider(ctx_name, clusters_map.get(ctx_data.get("cluster", ""), ""))
            region   = _detect_region(ctx_name)
            env      = _detect_environment(ctx_name)
            contexts.append(ClusterContext(
                name          = ctx_name,
                cluster       = ctx_data.get("cluster", ""),
                user          = ctx_data.get("user", ""),
                namespace     = ctx_data.get("namespace", "default") or "default",
                is_current    = (ctx_name == current_ctx),
                cloud_provider = provider,
                region        = region,
                environment   = env,
            ))

        log.info("multicluster.contexts_found", count=len(contexts))
        return contexts
    except Exception as exc:
        log.warning("multicluster.contexts_parse_failed", error=str(exc))
        return []


def _detect_provider(ctx_name: str, server_url: str) -> str:
    name = ctx_name.lower()
    url  = server_url.lower()
    if "eks" in name or "amazonaws" in url or "aws" in name:
        return "aws"
    if "gke" in name or "googleapis" in url or "gcp" in name:
        return "gcp"
    if "aks" in name or "azure" in url:
        return "azure"
    if "minikube" in name or "kind" in name or "docker-desktop" in name or "rancher" in name:
        return "local"
    return "unknown"


def _detect_region(ctx_name: str) -> str:
    import re as _re
    m = _re.search(
        r"(us-east-[12]|us-west-[12]|eu-west-[123]|eu-central-1"
        r"|ap-south-1|ap-southeast-[12]|ap-northeast-[123]"
        r"|ca-central-1|sa-east-1)",
        ctx_name, _re.IGNORECASE,
    )
    return m.group(1).lower() if m else ""


def _detect_environment(ctx_name: str) -> str:
    name = ctx_name.lower()
    if any(x in name for x in ("prod", "production", "prd")):
        return "production"
    if any(x in name for x in ("stag", "staging", "stage")):
        return "staging"
    if any(x in name for x in ("dev", "develop", "development", "local")):
        return "development"
    if any(x in name for x in ("test", "testing", "qa", "uat")):
        return "testing"
    return "unknown"


def get_current_context() -> str:
    result = run_kubectl(["config", "current-context"])
    return result.output.strip() if result.success else ""


def switch_context(context_name: str) -> bool:
    result = run_kubectl(["config", "use-context", context_name])
    if result.success:
        log.info("multicluster.switched", context=context_name)
    else:
        log.warning("multicluster.switch_failed", context=context_name, error=result.error)
    return result.success


def get_context_node_count(context_name: str) -> int:
    """Get node count for a context without permanently switching to it."""
    result = subprocess.run(
        ["kubectl", "get", "nodes", "--no-headers",
         f"--context={context_name}"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return -1
    return len([l for l in result.stdout.strip().splitlines() if l.strip()])


def add_eks_cluster(cluster_name: str, region: str, profile: str = "default") -> bool:
    """Run aws eks update-kubeconfig to add a cluster to kubeconfig."""
    if not shutil.which("aws"):
        log.warning("multicluster.aws_cli_missing")
        return False
    result = subprocess.run(
        ["aws", "eks", "update-kubeconfig",
         "--name", cluster_name,
         "--region", region,
         "--profile", profile],
        capture_output=True, text=True, timeout=30,
    )
    success = result.returncode == 0
    if success:
        log.info("multicluster.eks_added", cluster=cluster_name, region=region)
    else:
        log.warning("multicluster.eks_add_failed", error=result.stderr[:200])
    return success


def rename_context(old_name: str, new_name: str) -> bool:
    result = run_kubectl(["config", "rename-context", old_name, new_name])
    return result.success


def get_cluster_summary(context_name: str) -> dict:
    """Get pod + namespace count for a context."""
    pods_res = subprocess.run(
        ["kubectl", "get", "pods", "-A", "--no-headers",
         f"--context={context_name}"],
        capture_output=True, text=True, timeout=15,
    )
    ns_res = subprocess.run(
        ["kubectl", "get", "namespaces", "--no-headers",
         f"--context={context_name}"],
        capture_output=True, text=True, timeout=15,
    )
    ver_res = subprocess.run(
        ["kubectl", "version", "--short", f"--context={context_name}"],
        capture_output=True, text=True, timeout=15,
    )
    pod_lines = [l for l in pods_res.stdout.strip().splitlines() if l.strip()]
    ns_lines  = [l for l in ns_res.stdout.strip().splitlines() if l.strip()]
    version   = ""
    for line in ver_res.stdout.splitlines():
        if "Server" in line:
            version = line.split(":", 1)[-1].strip()
            break
    return {
        "pod_count":       len(pod_lines),
        "namespace_count": len(ns_lines),
        "k8s_version":     version,
        "reachable":       pods_res.returncode == 0,
    }


# ---------------------------------------------------------------------------
# Pod Log Analysis helpers
# ---------------------------------------------------------------------------

def get_pod_logs_smart(pod: str, namespace: str, lines: int = 200, container: str | None = None) -> "PodLogs":
    """
    Production-grade log fetching. Gets current + previous (crash) logs for all containers.
    Handles: pod not found, pending pods, multi-container, permission errors.
    """
    from agent.core.models import PodLogs

    result = PodLogs(pod=pod, namespace=namespace)

    # Discover containers via pod JSON
    pod_result = run_kubectl(["get", "pod", pod, "-n", namespace, "-o", "json"])
    if not pod_result.success:
        err = pod_result.error.lower()
        if "not found" in err:
            result.fetch_errors.append(f"Pod '{pod}' not found in namespace '{namespace}'")
        else:
            result.fetch_errors.append(f"Cannot access pod: {pod_result.error[:200]}")
        return result

    try:
        pod_spec = json.loads(pod_result.output)
        containers_raw = pod_spec.get("spec", {}).get("containers", [])
        init_containers = pod_spec.get("spec", {}).get("initContainers", [])
        all_containers = [c["name"] for c in containers_raw] + [c["name"] for c in init_containers]
        phase = pod_spec.get("status", {}).get("phase", "")
    except Exception:
        all_containers = []
        phase = ""

    if not all_containers:
        result.fetch_errors.append("No containers found in pod spec")
        return result

    # If specific container requested, filter to just that one
    target_containers = [container] if container and container in all_containers else all_containers
    result.containers = all_containers

    # Handle pending pods
    if phase == "Pending":
        result.fetch_errors.append(
            "Pod is Pending — container has not started yet. Check events with: "
            "kubectl describe pod " + pod + " -n " + namespace
        )
        return result

    total_lines = 0
    for cname in target_containers:
        # Current logs
        cur = run_kubectl(["logs", pod, "-n", namespace, "-c", cname, f"--tail={lines}"])
        if cur.success and cur.output.strip():
            result.current_logs[cname] = cur.output
            total_lines += len(cur.output.splitlines())
        elif cur.error:
            err = cur.error.lower()
            if "permission denied" in err or "forbidden" in err:
                result.fetch_errors.append(f"Permission denied reading logs for container '{cname}'")
            elif "container" in err and ("not found" in err or "invalid" in err):
                result.fetch_errors.append(f"Container '{cname}' not found or not ready")
            # else: silently skip (e.g. init container already completed)

        # Previous logs (the crash — most important for CrashLoopBackOff)
        prev = run_kubectl(["logs", pod, "-n", namespace, "-c", cname, "--previous", f"--tail={lines}"])
        if prev.success and prev.output.strip():
            result.previous_logs[cname] = prev.output
            result.has_previous = True
            total_lines += len(prev.output.splitlines())
        # previous logs not existing is expected — skip silently

    result.log_lines_count = total_lines
    log.info("logs.fetched", pod=pod, namespace=namespace,
             containers=len(target_containers), lines=total_lines, has_previous=result.has_previous)
    return result


def get_container_states(pod: str, namespace: str) -> list["ContainerState"]:
    """Extract per-container state, restart count, exit code, and reason from pod JSON."""
    from agent.core.models import ContainerState

    result = run_kubectl(["get", "pod", pod, "-n", namespace, "-o", "json"])
    if not result.success:
        return []

    states: list[ContainerState] = []
    try:
        data = json.loads(result.output)
        status = data.get("status", {})

        for cs in status.get("containerStatuses", []) + status.get("initContainerStatuses", []):
            name          = cs.get("name", "")
            ready         = cs.get("ready", False)
            restart_count = cs.get("restartCount", 0)

            state_dict = cs.get("state", {})
            last_dict  = cs.get("lastState", {})

            def _parse_state(d: dict) -> tuple[str, int | None, str | None]:
                if "running" in d:
                    return "running", None, None
                if "waiting" in d:
                    w = d["waiting"]
                    return "waiting", None, w.get("reason")
                if "terminated" in d:
                    t = d["terminated"]
                    return "terminated", t.get("exitCode"), t.get("reason")
                return "unknown", None, None

            state_str, exit_code, reason = _parse_state(state_dict)
            last_state_str, last_exit, last_reason = _parse_state(last_dict)

            # For CrashLoopBackOff the real reason is in lastState
            if reason == "CrashLoopBackOff" and last_reason:
                reason = f"CrashLoopBackOff (last: {last_reason})"
            if exit_code is None and last_exit is not None:
                exit_code = last_exit

            states.append(ContainerState(
                name=name, ready=ready, restart_count=restart_count,
                state=state_str, last_state=last_state_str,
                exit_code=exit_code, reason=reason,
            ))
    except Exception as exc:
        log.warning("container_states.parse_error", error=str(exc))

    return states


_ERROR_PATTERNS: list[tuple[str, list[str]]] = [
    ("OOM",        ["out of memory", "oomkilled", "heap limit", "heap oom", "cannot allocate memory",
                    "allocation failed", "java.lang.outofmemoryerror", "exit code 137"]),
    ("NETWORK",    ["connection refused", "connection timed out", "no route to host",
                    "dial tcp", "i/o timeout", "network unreachable", "econnrefused",
                    "econnreset", "dns resolution failed", "name or service not known"]),
    ("CONFIG",     ["no such file or directory", "file not found", "config not found",
                    "missing required", "invalid configuration", "cannot parse",
                    "environment variable", "no value for"]),
    ("PERMISSION", ["permission denied", "access denied", "operation not permitted",
                    "cannot open", "forbidden", "unauthorized"]),
    ("CRASH",      ["panic:", "fatal error", "segmentation fault", "core dumped",
                    "killed", "signal: killed", "traceback", "exception in thread",
                    "unhandled exception", "stack overflow"]),
]

def extract_error_patterns(logs: str) -> list[dict]:
    """
    Scan log text for known error signatures.
    Returns list of {line_no, line, pattern_type} for the most important lines.
    Caps at 20 matches to avoid flooding Claude's context.
    """
    matches: list[dict] = []
    seen_lines: set[str] = set()
    for line_no, line in enumerate(logs.splitlines(), 1):
        lower = line.lower().strip()
        if not lower or lower in seen_lines:
            continue
        for pattern_type, keywords in _ERROR_PATTERNS:
            if any(kw in lower for kw in keywords):
                seen_lines.add(lower)
                matches.append({
                    "line_no":      line_no,
                    "line":         line.strip()[:300],
                    "pattern_type": pattern_type,
                })
                break
        if len(matches) >= 20:
            break
    return matches


# ---------------------------------------------------------------------------
# Deployment Management
# ---------------------------------------------------------------------------

def get_deployment_info(deployment: str, namespace: str) -> "DeploymentInfo | None":
    from agent.core.models import DeploymentInfo
    r = run_kubectl(["get", "deployment", deployment, "-n", namespace, "-o", "json"])
    if not r.success:
        return None
    try:
        d = json.loads(r.output)
        spec        = d.get("spec", {})
        status      = d.get("status", {})
        containers  = spec.get("template", {}).get("spec", {}).get("containers", [])
        images      = [c.get("image", "") for c in containers]
        names       = [c.get("name", "") for c in containers]
        strategy    = spec.get("strategy", {})
        rolling     = strategy.get("rollingUpdate", {})
        desired     = spec.get("replicas", 0)
        ready       = status.get("readyReplicas", 0) or 0
        annotation  = d.get("metadata", {}).get("annotations", {})
        revision    = int(annotation.get("deployment.kubernetes.io/revision", 0))
        labels      = d.get("metadata", {}).get("labels", {})
        return DeploymentInfo(
            name             = d["metadata"]["name"],
            namespace        = d["metadata"]["namespace"],
            replicas_desired = desired,
            replicas_ready   = ready,
            current_image    = images[0] if images else "",
            containers       = names,
            strategy         = strategy.get("type", "RollingUpdate"),
            max_surge        = str(rolling.get("maxSurge", "25%")),
            max_unavailable  = str(rolling.get("maxUnavailable", "25%")),
            revision         = revision,
            healthy          = ready >= desired > 0,
            labels           = labels,
        )
    except Exception as exc:
        log.warning("get_deployment_info.parse_error", error=str(exc))
        return None


def get_deployment_history(deployment: str, namespace: str) -> list["Revision"]:
    from agent.core.models import Revision
    import json as _json

    r = run_kubectl(["rollout", "history", f"deployment/{deployment}", "-n", namespace])
    if not r.success:
        return []

    # Build revision → creationTimestamp map from ReplicaSets.
    # Each RS carries the annotation deployment.kubernetes.io/revision.
    rs_r = run_kubectl(["get", "rs", "-n", namespace, "-o", "json"], timeout=20)
    rev_ts: dict[int, str] = {}
    if rs_r.success:
        try:
            items = _json.loads(rs_r.output).get("items", [])
            for rs in items:
                ann = rs.get("metadata", {}).get("annotations", {})
                rev_ann = ann.get("deployment.kubernetes.io/revision", "")
                owner_refs = rs.get("metadata", {}).get("ownerReferences", [])
                owned_by_dep = any(
                    o.get("kind") == "Deployment" and o.get("name") == deployment
                    for o in owner_refs
                )
                if rev_ann.isdigit() and owned_by_dep:
                    rev_ts[int(rev_ann)] = rs["metadata"].get("creationTimestamp", "")
        except Exception:
            pass

    revisions: list[Revision] = []
    for line in r.output.splitlines():
        line = line.strip()
        if not line or line.startswith("REVISION") or line.startswith("deployment"):
            continue
        parts = line.split(None, 1)
        if not parts or not parts[0].isdigit():
            continue
        rev_num = int(parts[0])
        cause   = parts[1].strip() if len(parts) > 1 else "<none>"

        # Get image for this revision
        detail = run_kubectl([
            "rollout", "history", f"deployment/{deployment}",
            "-n", namespace, f"--revision={rev_num}",
        ], timeout=15)
        image = ""
        if detail.success:
            for dl in detail.output.splitlines():
                if "Image:" in dl:
                    image = dl.split("Image:")[-1].strip()
                    break

        revisions.append(Revision(
            revision_number = rev_num,
            image           = image,
            change_cause    = cause,
            created_at      = rev_ts.get(rev_num, ""),
        ))

    return sorted(revisions, key=lambda x: x.revision_number, reverse=True)


def get_previous_revision(deployment: str, namespace: str) -> "Revision | None":
    history = get_deployment_history(deployment, namespace)
    if len(history) < 2:
        return None
    return history[1]  # [0] is current (highest revision)


def rollout_undo(deployment: str, namespace: str, to_revision: int | None = None) -> KubectlResult:
    cmd = ["rollout", "undo", f"deployment/{deployment}", "-n", namespace]
    if to_revision:
        cmd += [f"--to-revision={to_revision}"]
    return run_kubectl(cmd)


def rollout_restart(deployment: str, namespace: str) -> KubectlResult:
    return run_kubectl(["rollout", "restart", f"deployment/{deployment}", "-n", namespace])


def set_image(deployment: str, namespace: str, container: str, image: str) -> KubectlResult:
    return run_kubectl([
        "set", "image", f"deployment/{deployment}",
        f"{container}={image}", "-n", namespace,
    ])


def restart_deployment(deployment: str, namespace: str) -> KubectlResult:
    return run_kubectl(["rollout", "restart", f"deployment/{deployment}", "-n", namespace])


def wait_for_rollout(deployment: str, namespace: str, timeout: int = 120) -> bool:
    r = run_kubectl([
        "rollout", "status", f"deployment/{deployment}",
        "-n", namespace, f"--timeout={timeout}s",
    ], timeout=timeout + 10)
    return r.success and "successfully rolled out" in r.output.lower()


def check_node_capacity(cpu_needed_m: int = 0, memory_needed_mb: int = 0) -> dict:
    """Estimate if cluster has room. Returns has_capacity + available figures."""
    nodes_r = run_kubectl(["get", "nodes", "-o", "json"])
    top_r   = run_kubectl(["top", "nodes", "--no-headers"], timeout=20)

    allocatable_cpu_m   = 0
    allocatable_mem_mb  = 0

    if nodes_r.success:
        try:
            for node in json.loads(nodes_r.output).get("items", []):
                alloc = node.get("status", {}).get("allocatable", {})
                allocatable_cpu_m  += parse_cpu(alloc.get("cpu", "0"))
                allocatable_mem_mb += parse_memory(alloc.get("memory", "0"))
        except Exception:
            pass

    used_cpu_m  = 0
    used_mem_mb = 0
    if top_r.success:
        for line in top_r.output.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                used_cpu_m  += parse_cpu(parts[1])
                used_mem_mb += parse_memory(parts[2])

    free_cpu_m  = max(allocatable_cpu_m  - used_cpu_m,  0)
    free_mem_mb = max(allocatable_mem_mb - used_mem_mb, 0)

    has_capacity = (
        (cpu_needed_m    == 0 or free_cpu_m  >= cpu_needed_m) and
        (memory_needed_mb == 0 or free_mem_mb >= memory_needed_mb)
    )
    return {
        "has_capacity":     has_capacity,
        "free_cpu_m":       free_cpu_m,
        "free_mem_mb":      free_mem_mb,
        "allocatable_cpu":  f"{allocatable_cpu_m}m",
        "allocatable_mem":  f"{allocatable_mem_mb}Mi",
        "used_cpu":         f"{used_cpu_m}m",
        "used_mem":         f"{used_mem_mb}Mi",
    }


def check_image_exists(image: str) -> bool:
    """Best-effort: check if image is already running in cluster (fast path)."""
    r = run_kubectl(["get", "pods", "-A", "-o", "json"])
    if not r.success:
        return True  # assume OK if we can't check
    try:
        for item in json.loads(r.output).get("items", []):
            for c in item.get("spec", {}).get("containers", []):
                if c.get("image", "") == image:
                    return True
    except Exception:
        pass
    # Basic format validation — if it has a colon it's likely real
    return ":" in image or "/" in image
