"""
Deep DNS collectors for the agentic OS.

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

def collect_coredns_health() -> dict:
    """CoreDNS pod status, replica count, and ready state."""
    r = run_kubectl(["get", "pods", "-n", "kube-system",
                     "-l", "k8s-app=kube-dns", "-o", "json"])
    if not r.success:
        return _err("coredns_health", r.error)

    items    = json.loads(r.output).get("items", [])
    pods     = []
    issues   = []

    for pod in items:
        meta     = pod["metadata"]
        status   = pod.get("status", {})
        phase    = status.get("phase", "Unknown")
        cs       = status.get("containerStatuses", [])
        ready    = all(c.get("ready", False) for c in cs)
        restarts = sum(c.get("restartCount", 0) for c in cs)
        name     = meta["name"]

        effective = phase
        for c in cs:
            w = c.get("state", {}).get("waiting", {})
            if w.get("reason"):
                effective = w["reason"]
                break

        pods.append({"name": name, "phase": effective, "ready": ready, "restarts": restarts})
        if not ready or effective not in ("Running",):
            issues.append(f"{name}: {effective} (restarts={restarts})")

    # Check deployment desired vs available
    dep_r = run_kubectl(["get", "deployment", "coredns", "-n", "kube-system", "-o", "json"])
    desired   = 0
    available = 0
    if dep_r.success:
        dep_spec   = json.loads(dep_r.output)
        desired    = dep_spec.get("spec", {}).get("replicas", 0)
        available  = dep_spec.get("status", {}).get("availableReplicas", 0)

    all_ok  = not issues and len(pods) > 0
    summary = (
        f"CoreDNS: {len(pods)} pod(s)"
        + (f" ({available}/{desired} available)" if desired else "")
        + (". All healthy." if all_ok else f". ISSUES: {'; '.join(issues)}")
    )
    return _ok(summary, {"pods": pods, "issues": issues,
                          "desired": desired, "available": available})


def collect_coredns_config() -> dict:
    """CoreDNS ConfigMap — parse Corefile for misconfigs."""
    r = run_kubectl(["get", "configmap", "coredns", "-n", "kube-system", "-o", "json"])
    if not r.success:
        return _ok("CoreDNS ConfigMap not found — DNS may be broken.",
                   {"found": False, "issues": ["CoreDNS ConfigMap missing"]})

    data      = json.loads(r.output).get("data", {})
    corefile  = data.get("Corefile", "")
    issues    = []

    # Basic Corefile sanity checks
    if "forward" not in corefile and "proxy" not in corefile:
        issues.append("No 'forward' or 'proxy' directive — external DNS resolution may fail")
    if "cache" not in corefile:
        issues.append("No 'cache' directive — DNS may be slow")
    if "health" not in corefile:
        issues.append("No 'health' plugin — readiness probes may fail")
    if "errors" not in corefile:
        issues.append("No 'errors' plugin — DNS errors won't be logged")

    summary = "CoreDNS Corefile OK." if not issues else f"CoreDNS config issues: {'; '.join(issues)}"
    return _ok(summary, {"found": True, "corefile": corefile[:800],
                          "issues": issues})


def collect_dns_resolution() -> dict:
    """
    Test DNS resolution using a single busybox pod that runs all nslookup tests in one shot.
    Far faster than spawning one pod per test.
    """
    import time

    pod_name = "agent-dns-probe"
    run_kubectl(["delete", "pod", pod_name, "-n", "default", "--ignore-not-found"])

    TESTS = {
        "cluster_local": "kubernetes.default.svc.cluster.local",
        "kube_dns_svc":  "kube-dns.kube-system.svc.cluster.local",
        "external":      "google.com",
    }

    # One pod, all tests in a single sh -c script
    script = " && ".join(
        f"echo '=={name}==' && nslookup {host} 2>&1 || echo FAIL_{name}"
        for name, host in TESTS.items()
    )

    create_r = run_kubectl([
        "run", pod_name, "-n", "default",
        "--image=busybox:1.28", "--restart=Never",
        "--", "sh", "-c", script,
    ])

    if not create_r.success:
        return _err("dns_resolution", f"pod create failed: {create_r.error[:60]}")

    # Wait for pod to finish (up to 15 s)
    for _ in range(15):
        time.sleep(1)
        phase_r = run_kubectl([
            "get", "pod", pod_name, "-n", "default",
            "-o", "jsonpath={.status.phase}",
        ])
        if phase_r.success and phase_r.output in ("Succeeded", "Failed"):
            break

    logs_r = run_kubectl(["logs", pod_name, "-n", "default"])
    run_kubectl(["delete", "pod", pod_name, "-n", "default", "--ignore-not-found"])

    out     = logs_r.output if logs_r.success else ""
    results = {}
    issues  = []

    for test_name, hostname in TESTS.items():
        marker = f"=={test_name}=="
        # Find the block between this marker and the next
        start  = out.find(marker)
        block  = out[start:] if start != -1 else ""
        passed = "Address" in block or "Server" in block
        results[test_name] = {"ok": passed, "host": hostname,
                               "output": block[:150]}
        if not passed:
            issues.append(f"{test_name} ({hostname}): FAILED")

    summary = (
        "DNS resolution: "
        + ", ".join(f"{k}={'PASS' if v['ok'] else 'FAIL'}" for k, v in results.items())
    )
    if issues:
        summary += " | ISSUES: " + "; ".join(issues)

    return _ok(summary, {"results": results, "issues": issues})


def collect_dns_services() -> dict:
    """Check kube-dns service and its endpoints are healthy."""
    svc_r = run_kubectl(["get", "service", "kube-dns", "-n", "kube-system", "-o", "json"])
    if not svc_r.success:
        return _ok("kube-dns service not found — DNS broken.",
                   {"found": False, "issues": ["kube-dns service missing"]})

    svc     = json.loads(svc_r.output)
    spec    = svc.get("spec", {})
    cluster_ip = spec.get("clusterIP", "")
    ports   = [f"{p['port']}/{p['protocol']}" for p in spec.get("ports", [])]

    ep_r    = run_kubectl(["get", "endpoints", "kube-dns", "-n", "kube-system", "-o", "json"])
    ready_addrs = 0
    if ep_r.success:
        for subset in json.loads(ep_r.output).get("subsets", []):
            ready_addrs += len(subset.get("addresses", []))

    issues = []
    if not cluster_ip or cluster_ip == "None":
        issues.append("kube-dns ClusterIP is None")
    if ready_addrs == 0:
        issues.append("kube-dns has no ready endpoints — DNS completely broken")

    summary = (
        f"kube-dns service: ClusterIP={cluster_ip} ports={ports} "
        f"ready_endpoints={ready_addrs}"
        + (f" | ISSUES: {'; '.join(issues)}" if issues else "")
    )
    return _ok(summary, {"cluster_ip": cluster_ip, "ports": ports,
                          "ready_endpoints": ready_addrs, "issues": issues})


def collect_external_dns() -> dict:
    """Check external-dns operator (if installed)."""
    r = run_kubectl(["get", "pods", "-A",
                     "-l", "app.kubernetes.io/name=external-dns", "-o", "json"])
    if not r.success or not json.loads(r.output).get("items"):
        # Try by name pattern
        r2 = run_kubectl(["get", "pods", "-A", "--no-headers"])
        if r2.success:
            lines = [l for l in r2.output.splitlines() if "external-dns" in l.lower()]
            if not lines:
                return _ok("external-dns not installed (optional component).",
                           {"installed": False})

    items  = json.loads(r.output).get("items", [])
    pods   = []
    issues = []
    for pod in items:
        meta  = pod["metadata"]
        phase = pod.get("status", {}).get("phase", "?")
        cs    = pod.get("status", {}).get("containerStatuses", [])
        ready = all(c.get("ready", False) for c in cs)
        pods.append({"name": meta["name"], "namespace": meta["namespace"],
                     "phase": phase, "ready": ready})
        if not ready:
            issues.append(f"{meta['namespace']}/{meta['name']}: {phase}")

    summary = f"external-dns: {len(pods)} pod(s). " + ("All healthy." if not issues
              else f"ISSUES: {'; '.join(issues)}")
    return _ok(summary, {"installed": True, "pods": pods, "issues": issues})


def collect_ndots_policy() -> dict:
    """
    Check if any pods have ndots configured > 5 (common cause of slow DNS).
    Also check for custom dnsConfig overrides.
    """
    r = run_kubectl(["get", "pods", "-A", "-o", "json"])
    if not r.success:
        return _err("ndots_policy", r.error)

    high_ndots = []
    custom_dns = []

    for pod in json.loads(r.output).get("items", []):
        meta       = pod["metadata"]
        spec       = pod.get("spec", {})
        dns_config = spec.get("dnsConfig", {})
        dns_policy = spec.get("dnsPolicy", "ClusterFirst")
        name       = f"{meta.get('namespace','?')}/{meta.get('name','?')}"

        options = dns_config.get("options", [])
        for opt in options:
            if opt.get("name") == "ndots":
                val = int(opt.get("value", 5))
                if val > 5:
                    high_ndots.append(f"{name}: ndots={val}")

        if dns_policy not in ("ClusterFirst", "ClusterFirstWithHostNet", "Default"):
            custom_dns.append(f"{name}: dnsPolicy={dns_policy}")

    summary = (
        f"{len(high_ndots)} pod(s) with ndots > 5 (slow DNS). "
        f"{len(custom_dns)} pod(s) with non-standard dnsPolicy."
    )
    if high_ndots:
        summary += "\nHigh ndots:\n" + "\n".join(f"  {n}" for n in high_ndots[:5])

    return _ok(summary, {"high_ndots": high_ndots, "custom_dns": custom_dns})


# ---------------------------------------------------------------------------
# Parallel orchestrator
# ---------------------------------------------------------------------------

DNS_COLLECTORS = {
    "coredns_health":   collect_coredns_health,
    "coredns_config":   collect_coredns_config,
    "dns_resolution":   collect_dns_resolution,
    "dns_services":     collect_dns_services,
    "external_dns":     collect_external_dns,
    "ndots_policy":     collect_ndots_policy,
}


def collect_all_dns() -> tuple[dict[str, dict], float]:
    import time
    from concurrent.futures import wait, FIRST_COMPLETED, ALL_COMPLETED

    results: dict[str, dict] = {}
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=len(DNS_COLLECTORS)) as pool:
        futures = {pool.submit(fn): area for area, fn in DNS_COLLECTORS.items()}
        done, pending = wait(futures, timeout=_TIMEOUT + 10)

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
    log.info("dns_collectors.done", areas=list(results), elapsed_ms=elapsed_ms)
    return results, elapsed_ms
