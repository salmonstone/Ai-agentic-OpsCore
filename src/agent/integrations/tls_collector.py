"""
TLS and certificate collectors for the agentic OS.

Rules:
  - No Claude calls — data only
  - Every function returns {"summary": str, "data": dict, "ok": bool}
  - Never raises
  - All collectors run in parallel via collect_all_tls()
"""
from __future__ import annotations

import base64
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from datetime import datetime, timezone

from agent.integrations.kubectl import run_kubectl
from agent.observability.logging import get_logger

log = get_logger(__name__)

_TIMEOUT      = 20
_EXPIRY_WARN_DAYS = 30   # warn if cert expires within this many days


def _ok(summary: str, data: dict) -> dict:
    return {"ok": True, "summary": summary, "data": data}


def _err(area: str, error: str) -> dict:
    return {"ok": False, "summary": f"{area} failed: {error[:80]}", "data": {}}


# ---------------------------------------------------------------------------
# Certificate parsing helpers
# ---------------------------------------------------------------------------

def _parse_cert_expiry(pem_b64: str) -> dict:
    """
    Parse a base64-encoded DER/PEM certificate and return expiry info.
    Tries cryptography lib first, falls back to openssl subprocess.
    Returns {"valid": bool, "expires": ISO str or None, "days_left": int, "subject": str, "sans": list}.
    """
    try:
        pem_bytes = base64.b64decode(pem_b64)
    except Exception:
        return {"valid": False, "error": "base64 decode failed"}

    # Try cryptography library
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend

        cert = x509.load_pem_x509_certificate(pem_bytes, default_backend())
        now  = datetime.now(timezone.utc)
        exp  = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") \
               else cert.not_valid_after.replace(tzinfo=timezone.utc)
        days_left = (exp - now).days
        subject   = cert.subject.rfc4514_string()

        sans = []
        try:
            ext  = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            sans = [str(n.value) for n in ext.value]
        except Exception:
            pass

        return {
            "valid":     True,
            "expires":   exp.isoformat(),
            "days_left": days_left,
            "expired":   days_left < 0,
            "subject":   subject[:120],
            "sans":      sans[:10],
        }
    except ImportError:
        pass
    except Exception as exc:
        return {"valid": False, "error": str(exc)[:80]}

    # Fallback: openssl subprocess
    try:
        result = subprocess.run(
            ["openssl", "x509", "-noout", "-enddate", "-subject"],
            input=pem_bytes, capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            out = result.stdout.decode(errors="replace")
            exp_line = next((l for l in out.splitlines() if "notAfter" in l), "")
            exp_str  = exp_line.split("=", 1)[1].strip() if "=" in exp_line else ""
            subj_line = next((l for l in out.splitlines() if "subject" in l), "")
            subject  = subj_line.split("=", 1)[1].strip() if "=" in subj_line else ""
            if exp_str:
                exp = datetime.strptime(exp_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                days_left = (exp - datetime.now(timezone.utc)).days
                return {
                    "valid":     True,
                    "expires":   exp.isoformat(),
                    "days_left": days_left,
                    "expired":   days_left < 0,
                    "subject":   subject[:120],
                    "sans":      [],
                }
    except Exception:
        pass

    return {"valid": False, "error": "cannot parse cert (install cryptography: pip install cryptography)"}


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def collect_tls_secrets() -> dict:
    """All kubernetes.io/tls secrets — parse each cert for expiry and validity."""
    r = run_kubectl(["get", "secrets", "-A", "-o", "json"])
    if not r.success:
        return _err("tls_secrets", r.error)

    items    = json.loads(r.output).get("items", [])
    tls_secs = [i for i in items if i.get("type") == "kubernetes.io/tls"]

    if not tls_secs:
        return _ok("No TLS secrets found.", {"secrets": [], "issues": []})

    secrets  = []
    issues   = []
    expired  = []
    expiring = []

    for sec in tls_secs:
        meta    = sec["metadata"]
        name    = meta["name"]
        ns      = meta["namespace"]
        data    = sec.get("data", {})

        has_crt = "tls.crt" in data
        has_key = "tls.key" in data

        entry = {"name": name, "namespace": ns, "has_crt": has_crt, "has_key": has_key}

        if not has_crt or not has_key:
            issues.append(f"{ns}/{name}: missing tls.crt or tls.key")
            entry["error"] = "incomplete secret"
            secrets.append(entry)
            continue

        cert_info = _parse_cert_expiry(data["tls.crt"])
        entry.update(cert_info)

        if not cert_info.get("valid"):
            issues.append(f"{ns}/{name}: cannot parse certificate — {cert_info.get('error','unknown')}")
        elif cert_info.get("expired"):
            issues.append(f"{ns}/{name}: EXPIRED {abs(cert_info['days_left'])} day(s) ago")
            expired.append(f"{ns}/{name}")
        elif cert_info.get("days_left", 9999) <= _EXPIRY_WARN_DAYS:
            issues.append(f"{ns}/{name}: expires in {cert_info['days_left']} day(s)")
            expiring.append(f"{ns}/{name}")

        secrets.append(entry)

    summary = (
        f"{len(tls_secs)} TLS secret(s). "
        + (f"{len(expired)} EXPIRED. " if expired else "")
        + (f"{len(expiring)} expiring soon. " if expiring else "")
        + ("All valid." if not issues else f"{len(issues)} issue(s).")
    )
    if issues:
        summary += "\n" + "\n".join(f"  {i}" for i in issues[:8])

    return _ok(summary, {"secrets": secrets, "issues": issues,
                          "expired": expired, "expiring": expiring})


def collect_cert_manager_certs() -> dict:
    """All cert-manager Certificate resources — conditions, issuer, renewal."""
    r = run_kubectl(["get", "certificates", "-A", "-o", "json"])
    if not r.success:
        return _ok("cert-manager Certificate CRD not found (cert-manager may not be installed).",
                   {"available": False, "certs": []})

    items  = json.loads(r.output).get("items", [])
    certs  = []
    issues = []

    for item in items:
        meta   = item["metadata"]
        spec   = item.get("spec", {})
        status = item.get("status", {})
        name   = meta["name"]
        ns     = meta["namespace"]

        conds      = {c["type"]: c for c in status.get("conditions", [])}
        ready_cond = conds.get("Ready", {})
        ready      = ready_cond.get("status") == "True"
        reason     = ready_cond.get("reason", "")
        message    = ready_cond.get("message", "")

        renewal_time = status.get("renewalTime", "")
        not_after    = status.get("notAfter", "")
        days_left    = None
        if not_after:
            try:
                exp       = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
                days_left = (exp - datetime.now(timezone.utc)).days
            except Exception:
                pass

        issuer_ref  = spec.get("issuerRef", {})
        secret_name = spec.get("secretName", "")
        dns_names   = spec.get("dnsNames", [])

        entry = {
            "name": name, "namespace": ns,
            "ready": ready, "reason": reason,
            "message": message[:120],
            "issuer": f"{issuer_ref.get('kind','')}/{issuer_ref.get('name','')}",
            "secret_name": secret_name,
            "dns_names": dns_names[:5],
            "days_left": days_left,
            "renewal_time": renewal_time,
        }
        certs.append(entry)

        if not ready:
            issues.append(f"{ns}/{name}: NOT READY — {reason}: {message[:80]}")
        elif days_left is not None and days_left < 0:
            issues.append(f"{ns}/{name}: certificate EXPIRED")
        elif days_left is not None and days_left <= _EXPIRY_WARN_DAYS:
            issues.append(f"{ns}/{name}: expires in {days_left} day(s)")

    summary = f"{len(items)} Certificate(s). {len(issues)} issue(s)."
    if issues:
        summary += "\n" + "\n".join(f"  {i}" for i in issues[:8])

    return _ok(summary, {"available": True, "certs": certs, "issues": issues})


def collect_cert_manager_issuers() -> dict:
    """Issuer and ClusterIssuer readiness."""
    issuers = []
    issues  = []

    for kind, cmd in [
        ("Issuer",        ["get", "issuers",        "-A", "-o", "json"]),
        ("ClusterIssuer", ["get", "clusterissuers",       "-o", "json"]),
    ]:
        r = run_kubectl(cmd)
        if not r.success:
            continue
        for item in json.loads(r.output).get("items", []):
            meta   = item["metadata"]
            status = item.get("status", {})
            name   = meta["name"]
            ns     = meta.get("namespace", "cluster-wide")

            conds  = {c["type"]: c for c in status.get("conditions", [])}
            ready  = conds.get("Ready", {}).get("status") == "True"
            reason = conds.get("Ready", {}).get("reason", "")
            msg    = conds.get("Ready", {}).get("message", "")

            # Detect ACME type
            acme   = item.get("spec", {}).get("acme", {})
            server = acme.get("server", "")
            itype  = "ACME" if acme else "other"
            if "letsencrypt" in server:
                itype = "LetsEncrypt-" + ("staging" if "staging" in server else "prod")

            entry = {"kind": kind, "name": name, "namespace": ns,
                     "ready": ready, "reason": reason,
                     "message": msg[:100], "type": itype, "server": server}
            issuers.append(entry)

            if not ready:
                issues.append(f"{kind}/{name}: NOT READY — {reason}: {msg[:80]}")

    if not issuers:
        return _ok("No Issuers or ClusterIssuers found.", {"issuers": [], "issues": []})

    summary = f"{len(issuers)} Issuer(s)/ClusterIssuer(s). {len(issues)} not ready."
    if issues:
        summary += "\n" + "\n".join(f"  {i}" for i in issues)

    return _ok(summary, {"issuers": issuers, "issues": issues})


def collect_acme_challenges() -> dict:
    """ACME Orders and Challenges — detect failing HTTP-01 / DNS-01 challenges."""
    challenges = []
    orders     = []
    issues     = []

    # Challenges
    r = run_kubectl(["get", "challenges", "-A", "-o", "json"])
    if r.success:
        for item in json.loads(r.output).get("items", []):
            meta   = item["metadata"]
            spec   = item.get("spec", {})
            status = item.get("status", {})
            name   = meta["name"]
            ns     = meta["namespace"]
            state  = status.get("state", "")
            reason = status.get("reason", "")
            ctype  = spec.get("type", "")
            dns    = spec.get("dnsName", "")

            entry = {"name": name, "namespace": ns, "state": state,
                     "type": ctype, "dns": dns, "reason": reason[:100]}
            challenges.append(entry)

            if state not in ("valid", ""):
                issues.append(f"{ns}/{name} ({ctype} for {dns}): {state} — {reason[:80]}")

    # Orders
    r2 = run_kubectl(["get", "orders", "-A", "-o", "json"])
    if r2.success:
        for item in json.loads(r2.output).get("items", []):
            meta   = item["metadata"]
            status = item.get("status", {})
            name   = meta["name"]
            ns     = meta["namespace"]
            state  = status.get("state", "")
            reason = status.get("reason", "")
            entry  = {"name": name, "namespace": ns, "state": state, "reason": reason[:80]}
            orders.append(entry)
            if state in ("errored", "invalid", "expired"):
                issues.append(f"Order {ns}/{name}: {state} — {reason[:80]}")

    if not challenges and not orders:
        return _ok("No active ACME challenges or orders.", {"challenges": [], "orders": [], "issues": []})

    summary = (
        f"{len(challenges)} challenge(s), {len(orders)} order(s). "
        + (f"{len(issues)} failing." if issues else "All valid.")
    )
    if issues:
        summary += "\n" + "\n".join(f"  {i}" for i in issues[:6])

    return _ok(summary, {"challenges": challenges, "orders": orders, "issues": issues})


def collect_ingress_tls_refs() -> dict:
    """Cross-check every Ingress TLS spec against the actual secrets that exist."""
    r = run_kubectl(["get", "ingress", "-A", "-o", "json"])
    if not r.success:
        return _ok("No ingress resources found.", {"refs": [], "issues": []})

    refs   = []
    issues = []

    for item in json.loads(r.output).get("items", []):
        meta = item["metadata"]
        spec = item.get("spec", {})
        ns   = meta["namespace"]
        name = meta["name"]

        hosts_in_rules = [r.get("host", "") for r in spec.get("rules", [])]

        for tls in spec.get("tls", []):
            secret_name = tls.get("secretName", "")
            tls_hosts   = tls.get("hosts", [])

            secret_ok = False
            if secret_name:
                sec_r = run_kubectl(["get", "secret", secret_name, "-n", ns])
                secret_ok = sec_r.success

            # Check hostname coverage
            uncovered = [h for h in hosts_in_rules
                         if h and tls_hosts and h not in tls_hosts]

            entry = {
                "ingress": name, "namespace": ns,
                "secret": secret_name, "secret_exists": secret_ok,
                "tls_hosts": tls_hosts, "uncovered_hosts": uncovered,
            }
            refs.append(entry)

            if secret_name and not secret_ok:
                issues.append(f"{ns}/{name}: TLS secret '{secret_name}' does NOT exist")
            if not secret_name:
                issues.append(f"{ns}/{name}: TLS configured but no secretName set")
            for h in uncovered:
                issues.append(f"{ns}/{name}: host '{h}' not covered by TLS spec")

    summary = f"{len(refs)} ingress TLS reference(s). {len(issues)} issue(s)."
    if issues:
        summary += "\n" + "\n".join(f"  {i}" for i in issues[:8])

    return _ok(summary, {"refs": refs, "issues": issues})


def collect_cert_manager_pods() -> dict:
    """cert-manager component pod health (controller, webhook, cainjector)."""
    CERT_NS = ["cert-manager", "kube-system", "default"]
    COMPONENTS = {
        "cert-manager":            "app=cert-manager",
        "cert-manager-webhook":    "app=webhook",
        "cert-manager-cainjector": "app=cainjector",
    }

    found_ns = None
    all_pods = []

    for ns in CERT_NS:
        for comp, sel in COMPONENTS.items():
            r = run_kubectl(["get", "pods", "-n", ns, "-l", sel, "-o", "json"])
            if r.success:
                pods = json.loads(r.output).get("items", [])
                if pods:
                    found_ns = ns
                    for pod in pods:
                        meta    = pod["metadata"]
                        status  = pod.get("status", {})
                        phase   = status.get("phase", "Unknown")
                        cs      = status.get("containerStatuses", [])
                        ready   = all(c.get("ready", False) for c in cs)
                        restarts = sum(c.get("restartCount", 0) for c in cs)
                        all_pods.append({
                            "name": meta["name"], "namespace": ns,
                            "component": comp, "phase": phase,
                            "ready": ready, "restarts": restarts,
                        })

    if not all_pods:
        return _ok(
            "cert-manager not found. Install from https://cert-manager.io/docs/installation/",
            {"installed": False},
        )

    ok_pods  = [p for p in all_pods if p["ready"]]
    bad_pods = [p for p in all_pods if not p["ready"]]

    summary = (
        f"cert-manager: {len(ok_pods)}/{len(all_pods)} pod(s) ready in {found_ns}."
        + (f" NOT READY: {', '.join(p['name'] for p in bad_pods)}" if bad_pods else "")
    )

    return _ok(summary, {
        "installed": True,
        "namespace": found_ns,
        "pods":      all_pods,
        "ok_pods":   [p["name"] for p in ok_pods],
        "bad_pods":  [p["name"] for p in bad_pods],
    })


# ---------------------------------------------------------------------------
# Parallel orchestrator
# ---------------------------------------------------------------------------

TLS_COLLECTORS = {
    "tls_secrets":       collect_tls_secrets,
    "cm_certificates":   collect_cert_manager_certs,
    "cm_issuers":        collect_cert_manager_issuers,
    "acme_challenges":   collect_acme_challenges,
    "ingress_tls_refs":  collect_ingress_tls_refs,
    "cm_pods":           collect_cert_manager_pods,
}


def collect_all_tls() -> tuple[dict[str, dict], float]:
    """Run all TLS collectors in parallel. Returns ({area: result}, elapsed_ms)."""
    import time
    results: dict[str, dict] = {}

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(TLS_COLLECTORS)) as pool:
        futures = {pool.submit(fn): area for area, fn in TLS_COLLECTORS.items()}
        for future in as_completed(futures, timeout=_TIMEOUT + 5):
            area = futures[future]
            try:
                results[area] = future.result(timeout=_TIMEOUT)
            except TimeoutError:
                results[area] = _err(area, "timed out")
            except Exception as exc:
                results[area] = _err(area, str(exc))

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    log.info("tls_collectors.done", areas=list(results), elapsed_ms=elapsed_ms)
    return results, elapsed_ms
