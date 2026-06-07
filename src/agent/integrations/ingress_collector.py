"""
Ingress and nginx-specific kubectl collectors.

Rules:
  - No Claude calls — data only
  - Every function returns {"summary": str, "data": dict, "ok": bool}
  - Never raises
  - All collectors run in parallel via collect_all_ingress()
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

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

def collect_nginx_controller() -> dict:
    """Find nginx ingress controller pods and check their health."""
    # Search across common namespaces and label selectors
    NAMESPACES   = ["ingress-nginx", "kube-system", "default", "nginx-ingress"]
    SELECTORS    = [
        "app.kubernetes.io/name=ingress-nginx",
        "app=ingress-nginx",
        "app=nginx-ingress",
        "app.kubernetes.io/component=controller",
    ]

    controller_pods = []
    found_ns = None

    for ns in NAMESPACES:
        for sel in SELECTORS:
            r = run_kubectl(["get", "pods", "-n", ns, "-l", sel, "-o", "json"])
            if r.success:
                items = json.loads(r.output).get("items", [])
                if items:
                    controller_pods.extend(items)
                    found_ns = ns
                    break
        if controller_pods:
            break

    # Also try a broad search by name pattern
    if not controller_pods:
        r = run_kubectl(["get", "pods", "-A", "-o", "json"])
        if r.success:
            for item in json.loads(r.output).get("items", []):
                name = item["metadata"]["name"]
                if "ingress" in name.lower() and ("nginx" in name.lower() or "controller" in name.lower()):
                    controller_pods.append(item)
                    found_ns = item["metadata"].get("namespace", "?")

    if not controller_pods:
        return _ok(
            "No nginx ingress controller pods found. "
            "Install with: kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/cloud/deploy.yaml",
            {"installed": False, "pods": [], "namespace": None},
        )

    pods_summary = []
    issues       = []

    for pod in controller_pods:
        meta    = pod["metadata"]
        status  = pod.get("status", {})
        phase   = status.get("phase", "Unknown")
        cs      = status.get("containerStatuses", [])
        ready   = all(c.get("ready", False) for c in cs)
        restarts = sum(c.get("restartCount", 0) for c in cs)
        name    = meta["name"]
        ns      = meta["namespace"]

        effective = phase
        for c in cs:
            w = c.get("state", {}).get("waiting", {})
            if w.get("reason"):
                effective = w["reason"]
                break

        entry = {
            "name": name, "namespace": ns, "phase": effective,
            "ready": ready, "restarts": restarts,
        }
        pods_summary.append(entry)

        if not ready or effective not in ("Running", "Succeeded"):
            issues.append(f"{ns}/{name}: {effective} (restarts={restarts})")

    all_ready = not issues
    summary = (
        f"nginx controller: {len(controller_pods)} pod(s) in {found_ns}. "
        + ("All healthy." if all_ready else f"ISSUES: {'; '.join(issues)}")
    )

    return _ok(summary, {
        "installed":  True,
        "namespace":  found_ns,
        "pods":       pods_summary,
        "issues":     issues,
        "all_ready":  all_ready,
    })


def collect_lb_services() -> dict:
    """Find LoadBalancer services — especially nginx ingress — and check external IPs."""
    r = run_kubectl(["get", "services", "-A", "-o", "json"])
    if not r.success:
        return _err("lb_services", r.error)

    items     = json.loads(r.output).get("items", [])
    lb_svcs   = []
    pending   = []
    healthy   = []

    for svc in items:
        if svc.get("spec", {}).get("type") != "LoadBalancer":
            continue

        meta    = svc["metadata"]
        status  = svc.get("status", {})
        ingress = status.get("loadBalancer", {}).get("ingress", [])
        name    = meta["name"]
        ns      = meta["namespace"]
        ports   = [f"{p.get('port')}/{p.get('protocol','TCP')}"
                   for p in svc.get("spec", {}).get("ports", [])]

        if ingress:
            addr = ingress[0].get("hostname") or ingress[0].get("ip", "")
        else:
            addr = ""

        is_nginx = any(k in name.lower() for k in ("ingress", "nginx"))
        entry = {
            "name": name, "namespace": ns, "ports": ports,
            "external_ip": addr, "pending": not bool(addr),
            "is_nginx": is_nginx,
        }
        lb_svcs.append(entry)

        if not addr:
            pending.append(f"{ns}/{name}")
        else:
            healthy.append(f"{ns}/{name} → {addr}")

    summary = f"{len(lb_svcs)} LoadBalancer service(s). {len(pending)} pending external IP."
    if pending:
        summary += "\nPENDING (no external IP):\n" + "\n".join(f"  {s}" for s in pending)
    if healthy:
        summary += "\nHealthy:\n" + "\n".join(f"  {s}" for s in healthy[:5])

    return _ok(summary, {
        "services":  lb_svcs,
        "pending":   pending,
        "healthy":   healthy,
    })


def collect_ingress_resources() -> dict:
    """All Ingress objects — rules, backends, TLS, addresses."""
    r = run_kubectl(["get", "ingress", "-A", "-o", "json"])
    if not r.success:
        return _ok("No Ingress resources found.", {"ingresses": [], "issues": []})

    items    = json.loads(r.output).get("items", [])
    ingresses = []
    issues   = []

    for item in items:
        meta   = item["metadata"]
        spec   = item.get("spec", {})
        status = item.get("status", {})
        ns     = meta["namespace"]
        name   = meta["name"]
        ann    = meta.get("annotations", {})

        lb     = status.get("loadBalancer", {}).get("ingress", [])
        addr   = lb[0].get("hostname", lb[0].get("ip", "")) if lb else ""

        ingress_class = (
            spec.get("ingressClassName")
            or ann.get("kubernetes.io/ingress.class")
            or ann.get("nginx.ingress.kubernetes.io/class")
            or ""
        )

        tls_hosts = []
        for t in spec.get("tls", []):
            tls_hosts.extend(t.get("hosts", []))

        tls_secrets = [t.get("secretName", "") for t in spec.get("tls", [])]

        rules = []
        backends_missing = []
        for rule in spec.get("rules", []):
            host = rule.get("host", "*")
            for path_item in rule.get("http", {}).get("paths", []):
                backend = path_item.get("backend", {})
                svc     = backend.get("service", {})
                svc_name = svc.get("name", "")
                svc_port = svc.get("port", {}).get("number") or svc.get("port", {}).get("name", "")
                path     = path_item.get("path", "/")
                ptype    = path_item.get("pathType", "")
                rules.append({"host": host, "path": path, "pathType": ptype,
                               "service": svc_name, "port": svc_port})
                if svc_name:
                    # Check if the service exists
                    svc_check = run_kubectl(["get", "service", svc_name, "-n", ns])
                    if not svc_check.success:
                        backends_missing.append(f"{ns}/{name}: backend service '{svc_name}' not found")

        if not addr:
            issues.append(f"{ns}/{name}: no external address assigned")
        if not ingress_class:
            issues.append(f"{ns}/{name}: no ingressClassName or annotation set")
        for m in backends_missing:
            issues.append(m)

        ingresses.append({
            "name": name, "namespace": ns,
            "address": addr, "ingress_class": ingress_class,
            "tls_hosts": tls_hosts, "tls_secrets": tls_secrets,
            "rules": rules, "annotations": dict(list(ann.items())[:10]),
            "backends_missing": backends_missing,
        })

    summary = f"{len(items)} Ingress resource(s). {len(issues)} issue(s)."
    if issues:
        summary += "\nIssues:\n" + "\n".join(f"  {i}" for i in issues[:10])

    return _ok(summary, {"ingresses": ingresses, "issues": issues})


def collect_ingress_classes() -> dict:
    """List available IngressClass resources."""
    r = run_kubectl(["get", "ingressclass", "-o", "json"])
    if not r.success:
        return _ok(
            "No IngressClass resources found (normal on older clusters).",
            {"classes": [], "default": None},
        )

    items   = json.loads(r.output).get("items", [])
    classes = []
    default = None

    for item in items:
        meta = item["metadata"]
        ann  = meta.get("annotations", {})
        name = meta["name"]
        ctrl = item.get("spec", {}).get("controller", "")
        is_default = ann.get("ingressclass.kubernetes.io/is-default-class") == "true"
        if is_default:
            default = name
        classes.append({"name": name, "controller": ctrl, "is_default": is_default})

    summary = f"{len(classes)} IngressClass(es): {', '.join(c['name'] for c in classes)}"
    if default:
        summary += f" | default: {default}"
    else:
        summary += " | no default class set"

    return _ok(summary, {"classes": classes, "default": default})


def collect_cert_manager() -> dict:
    """Check cert-manager pods and Certificate/Issuer status."""
    CERT_NS = ["cert-manager", "kube-system", "default"]
    cert_pods = []
    found_ns  = None

    for ns in CERT_NS:
        r = run_kubectl(["get", "pods", "-n", ns, "-l", "app=cert-manager", "-o", "json"])
        if r.success:
            items = json.loads(r.output).get("items", [])
            if items:
                cert_pods.extend(items)
                found_ns = ns
                break

    if not cert_pods:
        return _ok(
            "cert-manager not found. TLS auto-provisioning not available. "
            "Manual TLS secrets required.",
            {"installed": False},
        )

    pods_ok = []
    pods_bad = []
    for pod in cert_pods:
        phase  = pod.get("status", {}).get("phase", "?")
        name   = pod["metadata"]["name"]
        if phase == "Running":
            pods_ok.append(name)
        else:
            pods_bad.append(f"{name}: {phase}")

    # Check Certificate resources
    certs_r = run_kubectl(["get", "certificates", "-A", "-o", "json"])
    certs    = []
    cert_issues = []

    if certs_r.success:
        for cert in json.loads(certs_r.output).get("items", []):
            meta   = cert["metadata"]
            status = cert.get("status", {})
            conds  = {c["type"]: c for c in status.get("conditions", [])}
            ready  = conds.get("Ready", {}).get("status") == "True"
            reason = conds.get("Ready", {}).get("reason", "")
            certs.append({
                "name": meta["name"], "namespace": meta["namespace"],
                "ready": ready, "reason": reason,
            })
            if not ready:
                cert_issues.append(f"{meta['namespace']}/{meta['name']}: {reason or 'not ready'}")

    summary = (
        f"cert-manager: {len(pods_ok)} pod(s) running in {found_ns}. "
        + (f"{len(pods_bad)} NOT running." if pods_bad else "")
        + f" {len(certs)} Certificate(s)"
        + (f", {len(cert_issues)} with issues." if cert_issues else ", all ready." if certs else ".")
    )
    if cert_issues:
        summary += "\n" + "\n".join(f"  {c}" for c in cert_issues[:5])

    return _ok(summary, {
        "installed":   True,
        "namespace":   found_ns,
        "pods_ok":     pods_ok,
        "pods_bad":    pods_bad,
        "certs":       certs,
        "cert_issues": cert_issues,
    })


def collect_nginx_configmap() -> dict:
    """Check the nginx ingress controller ConfigMap for issues."""
    COMMON_NAMES = ["ingress-nginx-controller", "nginx-configuration", "nginx-ingress-controller"]
    NAMESPACES   = ["ingress-nginx", "kube-system", "default"]

    for ns in NAMESPACES:
        for name in COMMON_NAMES:
            r = run_kubectl(["get", "configmap", name, "-n", ns, "-o", "json"])
            if r.success:
                cm   = json.loads(r.output)
                data = cm.get("data", {})
                summary = (
                    f"nginx ConfigMap '{name}' in {ns}. "
                    f"{len(data)} setting(s) configured: "
                    + ", ".join(list(data.keys())[:8])
                )
                return _ok(summary, {"found": True, "name": name,
                                     "namespace": ns, "settings": data})

    return _ok(
        "nginx ConfigMap not found — using defaults.",
        {"found": False},
    )


def collect_backend_health() -> dict:
    """For every ingress backend service, check if pods are actually ready."""
    r = run_kubectl(["get", "ingress", "-A", "-o", "json"])
    if not r.success:
        return _ok("No ingresses to check backends for.", {"backends": []})

    checked  = []
    problems = []

    seen = set()
    for item in json.loads(r.output).get("items", []):
        ns   = item["metadata"]["namespace"]
        name = item["metadata"]["name"]
        for rule in item.get("spec", {}).get("rules", []):
            for path_item in rule.get("http", {}).get("paths", []):
                svc_name = (path_item.get("backend", {})
                            .get("service", {}).get("name", ""))
                if not svc_name or (ns, svc_name) in seen:
                    continue
                seen.add((ns, svc_name))

                # Check service endpoints
                ep_r = run_kubectl(["get", "endpoints", svc_name, "-n", ns, "-o", "json"])
                if not ep_r.success:
                    problems.append(f"{ns}/{svc_name}: service not found (ingress: {name})")
                    checked.append({"service": svc_name, "namespace": ns,
                                    "ready": False, "reason": "service not found"})
                    continue

                ep   = json.loads(ep_r.output)
                subs = ep.get("subsets") or []
                ready_addrs = sum(len(s.get("addresses", [])) for s in subs)
                not_ready   = sum(len(s.get("notReadyAddresses", [])) for s in subs)

                if ready_addrs == 0:
                    reason = "no ready endpoints (all pods down or not ready)"
                    if not_ready:
                        reason = f"{not_ready} pod(s) exist but not ready"
                    problems.append(f"{ns}/{svc_name}: {reason}")
                    checked.append({"service": svc_name, "namespace": ns,
                                    "ready": False, "reason": reason,
                                    "ready_pods": 0, "not_ready_pods": not_ready})
                else:
                    checked.append({"service": svc_name, "namespace": ns,
                                    "ready": True, "ready_pods": ready_addrs,
                                    "not_ready_pods": not_ready})

    summary = f"{len(checked)} backend(s) checked. {len(problems)} problem(s)."
    if problems:
        summary += "\nProblems:\n" + "\n".join(f"  {p}" for p in problems)

    return _ok(summary, {"backends": checked, "problems": problems})


# ---------------------------------------------------------------------------
# Parallel orchestrator
# ---------------------------------------------------------------------------

INGRESS_COLLECTORS = {
    "nginx_controller":  collect_nginx_controller,
    "lb_services":       collect_lb_services,
    "ingress_resources": collect_ingress_resources,
    "ingress_classes":   collect_ingress_classes,
    "cert_manager":      collect_cert_manager,
    "nginx_configmap":   collect_nginx_configmap,
    "backend_health":    collect_backend_health,
}


def collect_all_ingress() -> tuple[dict[str, dict], float]:
    """Run all ingress collectors in parallel. Returns ({area: result}, elapsed_ms)."""
    import time
    results: dict[str, dict] = {}

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(INGRESS_COLLECTORS)) as pool:
        futures = {pool.submit(fn): area for area, fn in INGRESS_COLLECTORS.items()}
        for future in as_completed(futures, timeout=_TIMEOUT + 5):
            area = futures[future]
            try:
                results[area] = future.result(timeout=_TIMEOUT)
            except TimeoutError:
                results[area] = _err(area, "timed out")
            except Exception as exc:
                results[area] = _err(area, str(exc))

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    log.info("ingress_collectors.done", areas=list(results), elapsed_ms=elapsed_ms)
    return results, elapsed_ms
