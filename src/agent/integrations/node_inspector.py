"""
Node and volume health inspection for the AI Agentic OS.

All operations are read-only unless explicitly called as a fix function.
Fix functions are named fix_* and should only be called after user confirmation.
"""
from __future__ import annotations

import json
import re

from agent.core.models import NodeMetrics, PvcInfo
from agent.integrations.kubectl import run_kubectl
from agent.observability.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Node metrics  (requires metrics-server)
# ---------------------------------------------------------------------------

def get_node_metrics() -> list[NodeMetrics]:
    """
    Parse `kubectl top nodes` output.
    Returns empty list if metrics-server is not installed.
    """
    result = run_kubectl(["top", "nodes", "--no-headers"])
    if not result.success:
        log.warning("node_metrics.unavailable", error=result.error[:100])
        return []

    metrics: list[NodeMetrics] = []
    for line in result.output.strip().splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        # NAME  CPU(cores)  CPU%  MEMORY(bytes)  MEMORY%
        name        = parts[0]
        cpu_cores   = parts[1]
        cpu_pct     = float(parts[2].strip("%") or 0)
        mem_bytes   = parts[3]
        mem_pct     = float(parts[4].strip("%") or 0)
        metrics.append(NodeMetrics(
            name           = name,
            cpu_cores      = cpu_cores,
            cpu_percent    = cpu_pct,
            memory_bytes   = mem_bytes,
            memory_percent = mem_pct,
        ))

    log.info("node_metrics.done", count=len(metrics))
    return metrics


# ---------------------------------------------------------------------------
# Node conditions  (from describe)
# ---------------------------------------------------------------------------

def get_node_conditions() -> dict[str, dict]:
    """
    Return {node_name: {condition: status}} for all nodes.
    Conditions: MemoryPressure, DiskPressure, PIDPressure, Ready.
    """
    result = run_kubectl(["get", "nodes", "-o", "json"])
    if not result.success:
        return {}

    try:
        data   = json.loads(result.output)
        output = {}
        for node in data.get("items", []):
            name = node["metadata"]["name"]
            conds = {}
            for c in node.get("status", {}).get("conditions", []):
                ctype  = c.get("type", "")
                status = c.get("status", "")
                reason = c.get("reason", "")
                message = c.get("message", "")
                conds[ctype] = {
                    "status":  status,
                    "reason":  reason,
                    "message": message,
                }
            output[name] = conds

            # Also grab allocatable for capacity context
            alloc = node.get("status", {}).get("allocatable", {})
            output[name]["_allocatable"] = alloc
            cap = node.get("status", {}).get("capacity", {})
            output[name]["_capacity"] = cap

        return output
    except Exception as e:
        log.error("node_conditions.parse_error", error=str(e))
        return {}


# ---------------------------------------------------------------------------
# Pod metrics  (requires metrics-server)
# ---------------------------------------------------------------------------

def get_top_pods(namespace: str = "all", limit: int = 20) -> list[dict]:
    """
    Return top pods sorted by memory usage.
    Returns empty list if metrics-server is not installed.
    """
    if namespace == "all":
        cmd = ["top", "pods", "-A", "--sort-by=memory", "--no-headers"]
    else:
        cmd = ["top", "pods", "-n", namespace, "--sort-by=memory", "--no-headers"]

    result = run_kubectl(cmd)
    if not result.success:
        log.warning("top_pods.unavailable", error=result.error[:100])
        return []

    pods: list[dict] = []
    for line in result.output.strip().splitlines()[:limit]:
        parts = line.split()
        if namespace == "all" and len(parts) >= 4:
            pods.append({
                "namespace": parts[0],
                "name":      parts[1],
                "cpu":       parts[2],
                "memory":    parts[3],
            })
        elif namespace != "all" and len(parts) >= 3:
            pods.append({
                "namespace": namespace,
                "name":      parts[0],
                "cpu":       parts[1],
                "memory":    parts[2],
            })

    return pods


# ---------------------------------------------------------------------------
# PVC status
# ---------------------------------------------------------------------------

def get_pvc_status() -> list[PvcInfo]:
    """Return all PVCs across all namespaces."""
    result = run_kubectl(["get", "pvc", "-A", "-o", "json"])
    if not result.success:
        log.warning("pvc_status.failed", error=result.error[:100])
        return []

    try:
        data  = json.loads(result.output)
        pvcs: list[PvcInfo] = []
        for item in data.get("items", []):
            meta = item.get("metadata", {})
            spec = item.get("spec", {})
            status = item.get("status", {})
            pvcs.append(PvcInfo(
                name          = meta.get("name", "?"),
                namespace     = meta.get("namespace", "?"),
                status        = status.get("phase", "?"),
                capacity      = status.get("capacity", {}).get("storage", spec.get("resources", {}).get("requests", {}).get("storage", "?")),
                storage_class = spec.get("storageClassName", "?"),
                volume_name   = spec.get("volumeName", "?"),
                access_modes  = spec.get("accessModes", []),
            ))
        log.info("pvc_status.done", count=len(pvcs))
        return pvcs
    except Exception as e:
        log.error("pvc_status.parse_error", error=str(e))
        return []


# ---------------------------------------------------------------------------
# Node disk usage  (via privileged debug pod)
# ---------------------------------------------------------------------------

def get_node_disk_usage(node_name: str) -> str:
    """
    Run `df -h` on a node by launching a short-lived privileged pod.
    Returns raw df output or an error string.
    """
    pod_name = f"disk-inspector-{node_name.split('.')[0][-8:]}"
    pod_manifest = json.dumps({
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": "default"},
        "spec": {
            "nodeName": node_name,
            "restartPolicy": "Never",
            "hostPID": True,
            "containers": [{
                "name": "inspector",
                "image": "busybox",
                "command": ["df", "-h", "/host"],
                "securityContext": {"privileged": True},
                "volumeMounts": [{"mountPath": "/host", "name": "host-root"}],
            }],
            "volumes": [{"name": "host-root", "hostPath": {"path": "/"}}],
            "tolerations": [{"operator": "Exists"}],
        },
    })

    # Create pod
    create = run_kubectl(["apply", "-f", "-"], )
    # Use stdin via echo approach — run_kubectl doesn't support stdin directly,
    # so we use a temp approach: write manifest and apply
    import subprocess, tempfile, os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(pod_manifest)
        tmp = f.name

    try:
        create_result = run_kubectl(["apply", "-f", tmp])
        if not create_result.success:
            return f"(could not create inspector pod: {create_result.error[:100]})"

        # Wait for completion
        import time
        for _ in range(15):
            time.sleep(2)
            status = run_kubectl(["get", "pod", pod_name, "-n", "default",
                                  "--no-headers", "-o",
                                  "custom-columns=STATUS:.status.phase"])
            if status.success and "Succeeded" in status.output:
                break

        # Get logs
        logs = run_kubectl(["logs", pod_name, "-n", "default"])
        output = logs.output if logs.success else "(no output)"

        # Cleanup
        run_kubectl(["delete", "pod", pod_name, "-n", "default", "--ignore-not-found"])
        return output
    finally:
        os.unlink(tmp)


def get_large_files_in_pod(pod_name: str, namespace: str, path: str = "/") -> str:
    """
    Find large files inside a running pod by exec'ing find + du.
    """
    result = run_kubectl([
        "exec", pod_name, "-n", namespace, "--",
        "sh", "-c", f"find {path} -type f -size +50M 2>/dev/null | head -20 | xargs ls -lh 2>/dev/null || echo 'no large files found'"
    ])
    if result.success:
        return result.output or "(no large files found)"
    return f"(exec failed: {result.error[:100]})"


def get_pod_log_size_in_pod(pod_name: str, namespace: str) -> str:
    """Check /var/log size inside a pod."""
    result = run_kubectl([
        "exec", pod_name, "-n", namespace, "--",
        "sh", "-c", "du -sh /var/log 2>/dev/null || echo 'no /var/log'"
    ])
    return result.output.strip() if result.success else "(unavailable)"


# ---------------------------------------------------------------------------
# Fix functions  (only call after user confirmation)
# ---------------------------------------------------------------------------

def fix_expand_pvc(pvc_name: str, namespace: str, new_size: str):
    """Expand a PVC to a new storage size (e.g. '20Gi')."""
    from agent.integrations.kubectl import apply_patch
    log.info("fix.expand_pvc", pvc=pvc_name, namespace=namespace, new_size=new_size)
    return apply_patch(
        kind="pvc",
        name=pvc_name,
        namespace=namespace,
        patch_type="merge",
        patch={"spec": {"resources": {"requests": {"storage": new_size}}}},
    )


def fix_delete_pod(pod_name: str, namespace: str):
    """Delete (evict) a pod to free node memory — k8s will reschedule it."""
    log.info("fix.delete_pod", pod=pod_name, namespace=namespace)
    return run_kubectl(["delete", "pod", pod_name, "-n", namespace])


def fix_increase_memory_limit(
    deployment: str, namespace: str, container: str, new_limit: str
):
    """Patch a deployment's container memory limit."""
    from agent.integrations.kubectl import apply_patch
    log.info("fix.increase_memory", deployment=deployment, limit=new_limit)
    return apply_patch(
        kind="deployment",
        name=deployment,
        namespace=namespace,
        patch_type="json",
        patch=[{
            "op": "replace",
            "path": "/spec/template/spec/containers/0/resources/limits/memory",
            "value": new_limit,
        }],
    )


def fix_truncate_logs_in_pod(pod_name: str, namespace: str, log_path: str = "/var/log"):
    """Truncate large log files inside a pod."""
    log.info("fix.truncate_logs", pod=pod_name, path=log_path)
    return run_kubectl([
        "exec", pod_name, "-n", namespace, "--",
        "sh", "-c",
        f"find {log_path} -name '*.log' -type f -exec truncate -s 0 {{}} \\; 2>/dev/null; echo 'done'"
    ])
