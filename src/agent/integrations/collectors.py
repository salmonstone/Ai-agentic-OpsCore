"""
Pure kubectl data collectors for the full-scan skill.

Rules:
  - No Claude calls here — data only
  - No `-o json` — use --no-headers table output to minimise subprocess memory
  - Every function returns {"summary": str, "data": dict, "ok": bool}
  - Never raises — errors go into the result dict
  - All collectors run in parallel via collect_all()
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

from agent.integrations.kubectl import run_kubectl
from agent.observability.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = 25   # seconds per collector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(summary: str, data: dict) -> dict:
    return {"ok": True, "summary": summary, "data": data}

def _err(area: str, error: str) -> dict:
    return {"ok": False, "summary": f"{area} collection failed: {error[:80]}", "data": {}}

def _lines(result) -> list[str]:
    """Split non-empty lines from a kubectl result."""
    if not result.success or not result.output.strip():
        return []
    return [l for l in result.output.strip().splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# Individual collectors  (all use --no-headers, zero JSON parsing)
# ---------------------------------------------------------------------------

def collect_nodes() -> dict:
    """Node readiness + pressure conditions via table output."""
    r = run_kubectl(["get", "nodes", "--no-headers"])
    if not r.success:
        return _err("nodes", r.error)

    lines  = _lines(r)
    issues = []
    nodes  = []

    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        name   = parts[0]
        status = parts[1]          # Ready / NotReady / SchedulingDisabled
        nodes.append(f"{name}: {status}")
        if status != "Ready":
            issues.append(f"{name} is {status}")

    # Top metrics — best-effort, may not be available
    top_lines: list[str] = []
    top = run_kubectl(["top", "nodes", "--no-headers"])
    if top.success:
        for tl in _lines(top):
            parts = tl.split()
            if len(parts) >= 5:
                top_lines.append(f"  {parts[0]}: CPU {parts[1]} ({parts[2]})  Mem {parts[3]} ({parts[4]})")

    summary = f"{len(nodes)} node(s). " + (f"ISSUES: {'; '.join(issues)}" if issues else "All Ready.")
    if top_lines:
        summary += "\nMetrics:\n" + "\n".join(top_lines)

    return _ok(summary, {"nodes": nodes, "issues": issues, "top": top_lines})


def collect_pods() -> dict:
    """Pod health via table output — STATUS + RESTARTS columns."""
    r = run_kubectl(["get", "pods", "-A", "--no-headers"])
    if not r.success:
        return _err("pods", r.error)

    problems:     list[str] = []
    high_restart: list[str] = []
    total = 0

    for line in _lines(r):
        parts = line.split()
        # NAMESPACE  NAME  READY  STATUS  RESTARTS  AGE
        if len(parts) < 5:
            continue
        total += 1
        ns      = parts[0]
        name    = parts[1]
        status  = parts[3]
        restart_str = parts[4].split("(")[0].strip()  # strip "(Xh ago)" suffix
        try:
            restarts = int(restart_str)
        except ValueError:
            restarts = 0

        if status not in ("Running", "Succeeded", "Completed"):
            problems.append(f"{ns}/{name}: {status} restarts={restarts}")
        elif restarts > 10:
            high_restart.append(f"{ns}/{name}: {restarts} restarts")

    summary = f"{total} pods. {len(problems)} problem(s). {len(high_restart)} high-restart."
    if problems:
        summary += "\nProblems:\n" + "\n".join(f"  {p}" for p in problems[:10])
    if high_restart:
        summary += "\nHigh restarts:\n" + "\n".join(f"  {p}" for p in high_restart[:5])

    return _ok(summary, {"total": total, "problems": problems, "high_restart": high_restart})


def collect_dns() -> dict:
    """CoreDNS health — pod status + endpoint check, no pod creation."""
    r  = run_kubectl(["get", "pods", "-n", "kube-system",
                      "-l", "k8s-app=kube-dns", "--no-headers"])
    coredns_lines  = _lines(r)
    coredns_issues = [l for l in coredns_lines if "Running" not in l]

    ep    = run_kubectl(["get", "endpoints", "kube-dns", "-n", "kube-system", "--no-headers"])
    ep_ok = ep.success and bool(ep.output.strip()) and "<none>" not in ep.output

    cm    = run_kubectl(["get", "configmap", "coredns", "-n", "kube-system", "--no-headers"])
    cm_ok = cm.success

    issues = []
    if coredns_issues:
        issues.append(f"CoreDNS pods not healthy: {coredns_issues}")
    if not ep_ok:
        issues.append("kube-dns endpoint has no ready addresses")

    summary  = "DNS OK." if not issues else "DNS ISSUES: " + "; ".join(issues)
    summary += (f"\nCoreDNS pods: {len(coredns_lines)} running. "
                f"Endpoint: {'OK' if ep_ok else 'NO ADDRESSES'}. "
                f"ConfigMap: {'OK' if cm_ok else 'MISSING'}.")

    return _ok(summary, {
        "coredns_pods":   coredns_lines,
        "coredns_issues": coredns_issues,
        "endpoint_ok":    ep_ok,
        "cm_present":     cm_ok,
    })


def collect_network() -> dict:
    """Services and endpoints — detect services with no backing pods."""
    ep_r  = run_kubectl(["get", "endpoints", "-A", "--no-headers"])
    svc_r = run_kubectl(["get", "services",  "-A", "--no-headers"])

    empty_eps: list[str] = []
    svc_count = len(_lines(svc_r))

    for line in _lines(ep_r):
        parts = line.split()
        # NAMESPACE  NAME  ENDPOINTS  AGE
        if len(parts) < 3:
            continue
        ns, name, endpoints = parts[0], parts[1], parts[2]
        if name in ("kubernetes",):
            continue
        if endpoints in ("<none>", ""):
            empty_eps.append(f"{ns}/{name}")

    summary = f"{svc_count} services. {len(empty_eps)} with empty endpoints."
    if empty_eps:
        summary += "\nEmpty endpoints:\n" + "\n".join(f"  {e}" for e in empty_eps[:10])

    return _ok(summary, {"empty_endpoints": empty_eps, "service_count": svc_count})


def collect_pvcs() -> dict:
    """PVC binding status — table output only."""
    r = run_kubectl(["get", "pvc", "-A", "--no-headers"])
    if not r.success:
        return _err("pvcs", r.error)

    unbound:  list[str] = []
    all_pvcs: list[str] = []

    for line in _lines(r):
        parts = line.split()
        # NAMESPACE  NAME  STATUS  VOLUME  CAPACITY  ACCESS MODES  STORAGECLASS  AGE
        if len(parts) < 3:
            continue
        ns, name, status = parts[0], parts[1], parts[2]
        cap = parts[4] if len(parts) > 4 else "?"
        entry = f"{ns}/{name}: {status} {cap}"
        all_pvcs.append(entry)
        if status != "Bound":
            unbound.append(entry)

    summary = f"{len(all_pvcs)} PVC(s). {len(unbound)} not Bound."
    if unbound:
        summary += "\nUnbound:\n" + "\n".join(f"  {p}" for p in unbound)

    return _ok(summary, {"pvcs": all_pvcs, "unbound": unbound})


def collect_jobs() -> dict:
    """Failed jobs and cronjob count — table output only."""
    jobs_r  = run_kubectl(["get", "jobs",     "-A", "--no-headers"])
    cjobs_r = run_kubectl(["get", "cronjobs", "-A", "--no-headers"])

    failed_jobs: list[str] = []
    stale_jobs:  list[str] = []

    for line in _lines(jobs_r):
        parts = line.split()
        # NAMESPACE  NAME  COMPLETIONS  DURATION  AGE
        if len(parts) < 3:
            continue
        ns, name, completions = parts[0], parts[1], parts[2]
        # completions looks like "0/1" (failed) or "1/1" (done)
        done, total = (completions.split("/") + ["1"])[:2]
        try:
            if int(done) == 0 and int(total) > 0:
                failed_jobs.append(f"{ns}/{name}: 0/{total} completions")
            elif int(done) >= int(total):
                stale_jobs.append(f"{ns}/{name}: completed")
        except ValueError:
            pass

    cj_count = len(_lines(cjobs_r))
    summary = f"{cj_count} CronJob(s). {len(failed_jobs)} failed Job(s). {len(stale_jobs)} stale."
    if failed_jobs:
        summary += "\nFailed:\n" + "\n".join(f"  {j}" for j in failed_jobs)

    return _ok(summary, {
        "failed_jobs":   failed_jobs,
        "stale_jobs":    stale_jobs,
        "cronjob_count": cj_count,
    })


def collect_hpa() -> dict:
    """HPA replica counts — detect HPAs pegged at max."""
    r = run_kubectl(["get", "hpa", "-A", "--no-headers"])
    if not r.success:
        return _ok("No HPAs found.", {"hpas": [], "at_max": []})

    at_max:   list[str] = []
    all_hpas: list[str] = []

    for line in _lines(r):
        parts = line.split()
        # NAMESPACE  NAME  REFERENCE  TARGETS  MINPODS  MAXPODS  REPLICAS  AGE
        if len(parts) < 7:
            continue
        ns, name = parts[0], parts[1]
        try:
            minp = int(parts[4])
            maxp = int(parts[5])
            repl = int(parts[6])
        except (ValueError, IndexError):
            continue
        entry = f"{ns}/{name}: {repl}/{maxp} replicas (min={minp})"
        all_hpas.append(entry)
        if repl >= maxp:
            at_max.append(entry)

    summary = f"{len(all_hpas)} HPA(s). {len(at_max)} at max replicas."
    if at_max:
        summary += "\nAt max:\n" + "\n".join(f"  {h}" for h in at_max)
    elif all_hpas:
        summary += "\n" + "\n".join(f"  {h}" for h in all_hpas[:6])

    return _ok(summary, {"hpas": all_hpas, "at_max": at_max})


def collect_ingress() -> dict:
    """Ingress address and TLS status — table output only."""
    r = run_kubectl(["get", "ingress", "-A", "--no-headers"])
    if not r.success:
        return _ok("No Ingress resources found.", {"ingresses": [], "no_address": []})

    no_address: list[str] = []
    all_ing:    list[str] = []

    for line in _lines(r):
        parts = line.split()
        # NAMESPACE  NAME  CLASS  HOSTS  ADDRESS  PORTS  AGE
        if len(parts) < 5:
            continue
        ns, name = parts[0], parts[1]
        hosts   = parts[3]
        address = parts[4] if len(parts) > 4 else ""

        entry = f"{ns}/{name}: hosts={hosts} address={address or 'NONE'}"
        all_ing.append(entry)
        if not address or address == "<none>":
            no_address.append(f"{ns}/{name}")

    summary = f"{len(all_ing)} Ingress(es). {len(no_address)} without address."
    if no_address:
        summary += "\nNo address:\n" + "\n".join(f"  {i}" for i in no_address)

    return _ok(summary, {"ingresses": all_ing, "no_address": no_address})


def collect_rbac() -> dict:
    """ClusterRoleBindings with cluster-admin — lightweight jsonpath query."""
    r = run_kubectl([
        "get", "clusterrolebindings",
        "-o", "jsonpath={range .items[*]}{.roleRef.name}{'|'}{range .subjects[*]}{.kind}{':'}{.name}{':'}{.namespace}{' '}{end}{'\\n'}{end}",
    ])

    overpermissioned: list[str] = []

    if r.success:
        for line in r.output.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("|", 1)
            if len(parts) < 2:
                continue
            role, subjects_str = parts[0].strip(), parts[1].strip()
            if role != "cluster-admin":
                continue
            for subj in subjects_str.split():
                skind, sname, sns = (subj.split(":") + ["", "", ""])[:3]
                if skind == "ServiceAccount":
                    overpermissioned.append(f"ServiceAccount {sns}/{sname} has cluster-admin")
                elif skind == "User" and sname not in ("system:admin", "kubernetes-admin"):
                    overpermissioned.append(f"User {sname} has cluster-admin")

    summary = f"RBAC: {len(overpermissioned)} overpermissioned subject(s)."
    if overpermissioned:
        summary += "\n" + "\n".join(f"  {o}" for o in overpermissioned)

    return _ok(summary, {"overpermissioned": overpermissioned})


def collect_tls() -> dict:
    """Check TLS cert expiry/validity for every ingress TLS secret."""
    from agent.integrations.kubectl import get_all_ingresses, get_tls_secret

    try:
        ingresses = get_all_ingresses()
    except Exception as exc:
        return _err("tls", str(exc))

    expired:       list[str] = []
    expiring_soon: list[str] = []
    missing:       list[str] = []
    healthy:       list[str] = []

    seen: set[tuple[str, str]] = set()

    for ing in ingresses:
        if not ing.tls_enabled or not ing.tls_secret:
            continue
        key = (ing.namespace, ing.tls_secret)
        if key in seen:
            continue
        seen.add(key)

        label = f"{ing.namespace}/{ing.name} → {ing.tls_secret}"
        if ing.domain:
            label += f" (host: {ing.domain})"

        try:
            si = get_tls_secret(ing.tls_secret, ing.namespace)
        except Exception as exc:
            missing.append(f"{label}  (fetch error: {exc})")
            continue

        if si is None:
            missing.append(label)
        elif si.is_expired:
            expired.append(f"{label}  EXPIRED ({si.expiry_date})")
        elif si.is_expiring_soon:
            expiring_soon.append(f"{label}  expires in {si.days_until_expiry}d ({si.expiry_date})")
        else:
            days = si.days_until_expiry
            healthy.append(f"{label}  expires in {days}d" if days >= 0 else label)

    issues = expired + expiring_soon + missing
    parts  = [
        f"{len(healthy)} healthy.",
        f"{len(expiring_soon)} expiring <30d." if expiring_soon else "",
        f"{len(expired)} EXPIRED." if expired else "",
        f"{len(missing)} secret(s) missing." if missing else "",
    ]
    summary = "TLS: " + "  ".join(p for p in parts if p)
    if not issues:
        summary += "  All certificates valid."
    if expired:
        summary += "\nExpired:\n" + "\n".join(f"  {e}" for e in expired)
    if expiring_soon:
        summary += "\nExpiring soon:\n" + "\n".join(f"  {e}" for e in expiring_soon)
    if missing:
        summary += "\nMissing:\n" + "\n".join(f"  {m}" for m in missing)

    return _ok(summary, {
        "healthy": healthy, "expiring_soon": expiring_soon,
        "expired": expired, "missing": missing,
    })


def collect_deployments() -> dict:
    """
    Deployment + StatefulSet health — flags scaled-to-zero and unavailable workloads.
    NAMESPACE  NAME  READY  UP-TO-DATE  AVAILABLE  AGE
    """
    scaled_zero:   list[str] = []
    unavailable:   list[str] = []
    healthy:       list[str] = []

    for kind in ("deployments", "statefulsets"):
        r = run_kubectl(["get", kind, "-A", "--no-headers"])
        if not r.success:
            continue
        for line in _lines(r):
            parts = line.split()
            if len(parts) < 4:
                continue
            ns   = parts[0]
            name = parts[1]
            # READY column: "2/2" or "0/0" or "1/3"
            ready_raw = parts[2]
            try:
                current, desired = (int(x) for x in ready_raw.split("/"))
            except (ValueError, IndexError):
                continue

            label = f"{ns}/{name} ({kind[:-1]})"
            if desired == 0:
                scaled_zero.append(label)
            elif current < desired:
                unavailable.append(f"{label}: {current}/{desired} ready")
            else:
                healthy.append(label)

    issues = scaled_zero + unavailable
    summary = (
        f"{len(healthy)} healthy, {len(scaled_zero)} scaled-to-zero, "
        f"{len(unavailable)} unavailable."
    )
    if scaled_zero:
        summary += "\nScaled to zero (0 replicas):\n" + "\n".join(f"  {w}" for w in scaled_zero)
    if unavailable:
        summary += "\nUnavailable pods:\n" + "\n".join(f"  {w}" for w in unavailable)

    return _ok(summary, {
        "scaled_zero": scaled_zero,
        "unavailable": unavailable,
        "healthy": healthy,
        "issues": issues,
    })


# ---------------------------------------------------------------------------
# Parallel orchestrator
# ---------------------------------------------------------------------------

COLLECTORS = {
    "nodes":       collect_nodes,
    "pods":        collect_pods,
    "deployments": collect_deployments,
    "dns":         collect_dns,
    "network":     collect_network,
    "pvcs":        collect_pvcs,
    "jobs":        collect_jobs,
    "hpa":         collect_hpa,
    "ingress":     collect_ingress,
    "rbac":        collect_rbac,
    "tls":         collect_tls,
}


def collect_all(areas: list[str] | None = None) -> tuple[dict[str, dict], float]:
    """
    Run collectors with bounded parallelism.
    Returns ({area: result}, elapsed_ms).
    """
    import time
    selected = {k: v for k, v in COLLECTORS.items() if areas is None or k in areas}
    results: dict[str, dict] = {}

    # 3 workers on Windows — each kubectl is a Go subprocess; too many at once
    # exhausts the paging file even with --no-headers.
    workers = min(3, len(selected))

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn): area for area, fn in selected.items()}
        for future in as_completed(futures, timeout=_TIMEOUT + 5):
            area = futures[future]
            try:
                results[area] = future.result(timeout=_TIMEOUT)
            except TimeoutError:
                results[area] = _err(area, "timed out")
            except Exception as exc:
                results[area] = _err(area, str(exc))

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    log.info("collectors.collect_all.done", areas=list(results), elapsed_ms=elapsed_ms)
    return results, elapsed_ms
