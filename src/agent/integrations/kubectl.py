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
import shutil
import subprocess
import time
from datetime import datetime, timezone

from agent.core.models import (
    CertInfo, ClusterOverview, IngressInfo, KubectlResult,
    NamespaceInfo, NodeInfo, PodInfo, ProblemType, TLSSecretInfo,
)
from agent.observability.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = 30  # seconds for every kubectl call

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

def run_kubectl(command: list[str]) -> KubectlResult:
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
            timeout=_TIMEOUT,
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
    Build a complete snapshot of the cluster:
    all namespaces → all pods → node status → ClusterOverview.
    """
    cluster_name           = _get_cluster_name()
    total_nodes, healthy_n = _get_node_status()
    namespace_names        = get_all_namespaces()

    generated_at = datetime.now(timezone.utc).isoformat()

    ns_infos: list[NamespaceInfo] = []
    total_pods = healthy_total = unhealthy_total = 0

    for ns_name in namespace_names:
        pods       = get_pods_in_namespace(ns_name)
        healthy    = sum(1 for p in pods if _is_pod_healthy(p))
        unhealthy  = len(pods) - healthy

        ns_infos.append(NamespaceInfo(
            name           = ns_name,
            pods           = pods,
            total_pods     = len(pods),
            healthy_pods   = healthy,
            unhealthy_pods = unhealthy,
        ))
        total_pods     += len(pods)
        healthy_total  += healthy
        unhealthy_total += unhealthy

    log.info(
        "get_cluster_overview.done",
        cluster=cluster_name,
        namespaces=len(ns_infos),
        total_pods=total_pods,
        unhealthy=unhealthy_total,
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
