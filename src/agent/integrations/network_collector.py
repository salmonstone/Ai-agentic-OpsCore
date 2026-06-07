"""
Deep network collectors for the agentic OS.

Rules: no Claude, never raises, every fn returns {"ok", "summary", "data"}.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, wait

from agent.integrations.kubectl import run_kubectl
from agent.observability.logging import get_logger

log = get_logger(__name__)
_TIMEOUT = 20


def _ok(summary: str, data: dict) -> dict:
    return {"ok": True, "summary": summary, "data": data}

def _err(area: str, error: str) -> dict:
    return {"ok": False, "summary": f"{area} failed: {error[:80]}", "data": {}}


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def collect_cni_health() -> dict:
    """Detect CNI plugin (Calico/Flannel/Cilium/Weave/other) and check pod health."""
    cni_map = {
        "calico":   [("kube-system", "k8s-app=calico-node"),
                     ("kube-system", "k8s-app=calico-kube-controllers")],
        "flannel":  [("kube-flannel", "app=flannel"),
                     ("kube-system", "app=flannel")],
        "cilium":   [("kube-system", "k8s-app=cilium"),
                     ("cilium", "k8s-app=cilium")],
        "weave":    [("kube-system", "name=weave-net")],
    }

    detected   = None
    all_pods   = []
    issues     = []

    for cni_name, targets in cni_map.items():
        for ns, selector in targets:
            r = run_kubectl(["get", "pods", "-n", ns, "-l", selector, "-o", "json"])
            if not r.success:
                continue
            items = json.loads(r.output).get("items", [])
            if not items:
                continue

            detected = cni_name
            for pod in items:
                meta  = pod["metadata"]
                phase = pod.get("status", {}).get("phase", "?")
                cs    = pod.get("status", {}).get("containerStatuses", [])
                ready = all(c.get("ready", False) for c in cs)
                rests = sum(c.get("restartCount", 0) for c in cs)
                name  = meta["name"]
                all_pods.append({"name": name, "namespace": ns,
                                  "phase": phase, "ready": ready, "restarts": rests})
                if not ready:
                    issues.append(f"{cni_name}/{name}: {phase} restarts={rests}")

    if not detected:
        return _ok("CNI plugin not detected — cluster may use host networking or unknown CNI.",
                   {"detected": None, "pods": [], "issues": ["unknown CNI"]})

    summary = (
        f"CNI={detected}: {len(all_pods)} pod(s). "
        + ("All healthy." if not issues else f"ISSUES: {'; '.join(issues)}")
    )
    return _ok(summary, {"detected": detected, "pods": all_pods, "issues": issues})


def collect_kube_proxy() -> dict:
    """Check kube-proxy DaemonSet health on all nodes."""
    r = run_kubectl(["get", "daemonset", "kube-proxy", "-n", "kube-system", "-o", "json"])
    if not r.success:
        return _ok("kube-proxy DaemonSet not found — may be using eBPF/Cilium replacement.",
                   {"found": False, "issues": []})

    ds        = json.loads(r.output)
    status    = ds.get("status", {})
    desired   = status.get("desiredNumberScheduled", 0)
    ready     = status.get("numberReady", 0)
    available = status.get("numberAvailable", 0)
    issues    = []

    if desired == 0:
        issues.append("kube-proxy DaemonSet has 0 desired — not scheduled to any node")
    elif ready < desired:
        issues.append(f"kube-proxy: {ready}/{desired} ready — {desired - ready} pods not running")

    summary = (
        f"kube-proxy: {ready}/{desired} nodes ready, {available} available"
        + (f" | ISSUES: {'; '.join(issues)}" if issues else " — OK")
    )
    return _ok(summary, {"desired": desired, "ready": ready,
                          "available": available, "issues": issues})


def collect_services_health() -> dict:
    """
    Find services with no ready endpoints or mismatched selectors.
    Skips headless services and ExternalName services.
    """
    svc_r = run_kubectl(["get", "services", "-A", "-o", "json"])
    if not svc_r.success:
        return _err("services_health", svc_r.error)

    ep_r  = run_kubectl(["get", "endpoints", "-A", "-o", "json"])
    if not ep_r.success:
        return _err("services_health", ep_r.error)

    # Build endpoint index: namespace/name → ready_count
    ep_index: dict[str, int] = {}
    for ep in json.loads(ep_r.output).get("items", []):
        meta = ep["metadata"]
        key  = f"{meta['namespace']}/{meta['name']}"
        count = 0
        for subset in ep.get("subsets", []):
            count += len(subset.get("addresses", []))
        ep_index[key] = count

    no_endpoints = []
    no_selector  = []
    total        = 0

    for svc in json.loads(svc_r.output).get("items", []):
        meta    = svc["metadata"]
        spec    = svc.get("spec", {})
        svc_type = spec.get("type", "ClusterIP")
        selector = spec.get("selector", {})
        cluster_ip = spec.get("clusterIP", "")
        key     = f"{meta['namespace']}/{meta['name']}"

        # Skip system services and headless/ExternalName
        if meta["namespace"] in ("kube-system", "kube-public", "kube-node-lease"):
            continue
        if svc_type == "ExternalName":
            continue
        if cluster_ip == "None":
            continue

        total += 1

        if not selector:
            no_selector.append(key)
            continue

        ready = ep_index.get(key, 0)
        if ready == 0:
            no_endpoints.append({"service": key, "selector": selector})

    issues_summary = []
    if no_endpoints:
        issues_summary.append(f"{len(no_endpoints)} svc(s) with no ready endpoints")
    if no_selector:
        issues_summary.append(f"{len(no_selector)} svc(s) with no selector")

    summary = (
        f"Services: {total} user-facing. "
        + ("; ".join(issues_summary) if issues_summary else "All have endpoints — OK")
    )
    return _ok(summary, {"total": total, "no_endpoints": no_endpoints,
                          "no_selector": no_selector})


def collect_network_policies() -> dict:
    """List NetworkPolicies and flag any that might block all ingress/egress."""
    r = run_kubectl(["get", "networkpolicies", "-A", "-o", "json"])
    if not r.success:
        return _ok("NetworkPolicy collection failed — may not be enabled.",
                   {"count": 0, "deny_all": [], "issues": []})

    items      = json.loads(r.output).get("items", [])
    deny_all   = []
    namespaces_with_policies: set[str] = set()

    for np in items:
        meta  = np["metadata"]
        spec  = np.get("spec", {})
        ns    = meta["namespace"]
        name  = meta["name"]
        namespaces_with_policies.add(ns)

        ingress = spec.get("ingress", None)
        egress  = spec.get("egress",  None)
        pod_sel = spec.get("podSelector", {})

        # Empty podSelector matches all pods; missing ingress/egress block all
        is_catch_all = (pod_sel == {} or pod_sel == {"matchLabels": {}})
        policy_types = spec.get("policyTypes", [])

        if is_catch_all:
            if "Ingress" in policy_types and ingress == []:
                deny_all.append(f"{ns}/{name}: DENY ALL ingress")
            if "Egress" in policy_types and egress == []:
                deny_all.append(f"{ns}/{name}: DENY ALL egress")

    issues = deny_all if deny_all else []
    summary = (
        f"{len(items)} NetworkPolicy(ies) across "
        f"{len(namespaces_with_policies)} namespace(s). "
        + (f"DENY-ALL found: {'; '.join(deny_all)}" if deny_all
           else "No blanket deny-all policies found.")
    )
    return _ok(summary, {"count": len(items), "deny_all": deny_all,
                          "namespaces": list(namespaces_with_policies), "issues": issues})


def collect_node_network() -> dict:
    """Node readiness and network-related node conditions."""
    r = run_kubectl(["get", "nodes", "-o", "json"])
    if not r.success:
        return _err("node_network", r.error)

    nodes    = []
    issues   = []

    for node in json.loads(r.output).get("items", []):
        meta       = node["metadata"]
        name       = meta["name"]
        conditions = node.get("status", {}).get("conditions", [])
        addresses  = [a["address"] for a in node.get("status", {}).get("addresses", [])
                      if a["type"] in ("InternalIP", "ExternalIP")]

        ready = False
        conds = {}
        for cond in conditions:
            cond_type = cond["type"]
            status    = cond["status"]
            conds[cond_type] = status
            if cond_type == "Ready":
                ready = (status == "True")

        # Network-specific conditions
        if conds.get("NetworkUnavailable") == "True":
            issues.append(f"{name}: NetworkUnavailable=True")
        if not ready:
            issues.append(f"{name}: NotReady")

        nodes.append({"name": name, "ready": ready,
                      "addresses": addresses, "conditions": conds})

    summary = (
        f"{len(nodes)} node(s). "
        + ("All network-ready." if not issues else f"ISSUES: {'; '.join(issues)}")
    )
    return _ok(summary, {"nodes": nodes, "issues": issues})


def collect_pod_connectivity() -> dict:
    """
    Quick pod-to-pod connectivity test across namespaces using a busybox pod.
    Tests connection to the kubernetes API service and kube-dns service.
    """
    import time

    tests   = {}
    issues  = []

    TARGETS = {
        "k8s_api":  ("kubernetes.default.svc.cluster.local", "443"),
        "kube_dns": ("kube-dns.kube-system.svc.cluster.local", "53"),
    }

    for test_name, (host, port) in TARGETS.items():
        pod_name = f"agent-net-test-{test_name.replace('_','-')}"
        run_kubectl(["delete", "pod", pod_name, "-n", "default", "--ignore-not-found"])

        r = run_kubectl([
            "run", pod_name, "-n", "default",
            "--image=busybox:1.28", "--restart=Never",
            "--", "sh", "-c", f"nc -z -w3 {host} {port} && echo OK || echo FAIL"
        ])
        if not r.success:
            tests[test_name] = {"ok": False, "host": host, "port": port,
                                  "reason": "pod create failed"}
            issues.append(f"{test_name}: pod create failed")
            continue

        time.sleep(5)
        logs_r = run_kubectl(["logs", pod_name, "-n", "default"])
        run_kubectl(["delete", "pod", pod_name, "-n", "default", "--ignore-not-found"])

        out = (logs_r.output or "").strip()
        ok  = "OK" in out
        tests[test_name] = {"ok": ok, "host": host, "port": port, "output": out[:80]}
        if not ok:
            issues.append(f"{test_name} ({host}:{port}): {out or 'no output'}")

    summary = (
        "Pod connectivity: "
        + ", ".join(f"{k}={'PASS' if v['ok'] else 'FAIL'}" for k, v in tests.items())
    )
    if issues:
        summary += " | ISSUES: " + "; ".join(issues)

    return _ok(summary, {"tests": tests, "issues": issues})


def collect_node_conditions() -> dict:
    """Collect all node pressure conditions (Memory/Disk/PID pressure)."""
    r = run_kubectl(["get", "nodes", "-o", "json"])
    if not r.success:
        return _err("node_conditions", r.error)

    pressure_issues = []
    nodes           = []

    for node in json.loads(r.output).get("items", []):
        name   = node["metadata"]["name"]
        conds  = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
        issues = []

        for pressure in ("MemoryPressure", "DiskPressure", "PIDPressure"):
            if conds.get(pressure) == "True":
                issues.append(pressure)

        nodes.append({"name": name, "conditions": conds, "pressure": issues})
        if issues:
            pressure_issues.append(f"{name}: {', '.join(issues)}")

    summary = (
        f"{len(nodes)} node(s) checked. "
        + ("No pressure conditions." if not pressure_issues
           else f"PRESSURE: {'; '.join(pressure_issues)}")
    )
    return _ok(summary, {"nodes": nodes, "pressure_issues": pressure_issues})


# ---------------------------------------------------------------------------
# Parallel orchestrator
# ---------------------------------------------------------------------------

NETWORK_COLLECTORS = {
    "cni_health":         collect_cni_health,
    "kube_proxy":         collect_kube_proxy,
    "services_health":    collect_services_health,
    "network_policies":   collect_network_policies,
    "node_network":       collect_node_network,
    "pod_connectivity":   collect_pod_connectivity,
    "node_conditions":    collect_node_conditions,
}


def collect_all_network() -> tuple[dict[str, dict], float]:
    import time
    from concurrent.futures import wait, ALL_COMPLETED

    results: dict[str, dict] = {}
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=len(NETWORK_COLLECTORS)) as pool:
        futures = {pool.submit(fn): area for area, fn in NETWORK_COLLECTORS.items()}
        done, pending = wait(futures, timeout=_TIMEOUT + 15)

        for future in done:
            area = futures[future]
            try:
                results[area] = future.result()
            except Exception as exc:
                results[area] = _err(area, str(exc))

        for future in pending:
            area = futures[future]
            results[area] = _err(area, "timed out")
            future.cancel()

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    log.info("network_collectors.done", areas=list(results), elapsed_ms=elapsed_ms)
    return results, elapsed_ms
