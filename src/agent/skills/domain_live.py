"""
DomainLive — end-to-end domain reachability check and fix.

Checks 8 layers in order:
  1. LoadBalancer   — external IP / hostname on the Ingress
  2. DNS            — domain resolves and points to the LB
  3. TLS Section    — Ingress has spec.tls (most common miss)
  4. TLS Certificate — cert-manager cert exists in the SAME namespace as Ingress
  5. TLS Secret     — TLS secret is present in the Ingress namespace
  6. HTTP           — plain HTTP responds
  7. HTTPS          — HTTPS responds
  8. Backend        — service has ready endpoints

Deterministic fix patterns (no Claude guessing required):
  Pattern A  cert-manager annotation + no spec.tls
             → patch ingress to add spec.tls  (cert-manager re-issues in correct ns)
  Pattern B  Certificate exists in wrong namespace
             → same patch — spec.tls in correct ns forces re-issue
  Pattern C  DNS not resolving
             → add CNAME/A record in registrar
  Pattern D  LB pending
             → wait or check cloud provider
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import time

from agent.core import llm
from agent.integrations.kubectl import (
    get_all_ingresses,
    get_cert_manager_certificates,
    get_tls_secret,
    run_kubectl,
)
from agent.observability.logging import get_logger

log = get_logger(__name__)

_SYSTEM = """\
You are a Kubernetes/DNS/TLS expert. Given layer-by-layer domain health checks,
return ONLY valid JSON — no markdown fences:
{
  "overall_status": "live|degraded|down",
  "root_cause":     "one clear sentence",
  "ai_analysis":    "2-3 sentence explanation",
  "suggested_fix":  "exact human-readable steps",
  "next_steps":     ["step1", "step2"]
}
Focus on the FIRST failing layer — that is always the root cause.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_domain(domain: str | None = None,
                 namespace_hint: str | None = None) -> list[dict]:
    """
    Check one domain or every cluster domain.
    Returns a list of result dicts — one per ingress found.
    """
    all_ings = get_all_ingresses()
    if domain:
        ings = [i for i in all_ings if i.domain == domain]
        if namespace_hint:
            ings = [i for i in ings if i.namespace == namespace_hint] or ings
        if not ings:
            return [{"domain": domain, "found": False,
                     "error": f"No Ingress found for '{domain}'"}]
    else:
        ings = all_ings

    return [_check_one(ing) for ing in ings]


def fix_ingress_tls(ingress_name: str, namespace: str,
                    domain: str, secret_name: str) -> dict:
    patch = json.dumps({
        "spec": {"tls": [{"hosts": [domain], "secretName": secret_name}]}
    })
    r = run_kubectl(["patch", "ingress", ingress_name,
                     "-n", namespace, "--type=merge",
                     f"--patch={patch}"])
    return {"ok": r.success, "output": r.output, "error": r.error}


def wait_for_cert(domain: str, namespace: str, timeout: int = 90) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        certs = get_cert_manager_certificates()
        c = next((x for x in certs if x.domain == domain
                  and x.namespace == namespace), None)
        if c and c.ready:
            return {"ok": True, "expiry": c.expiry, "message": c.message}
        time.sleep(5)
    return {"ok": False, "error": "Timed out — ACME can take 1-3 min"}


# ---------------------------------------------------------------------------
# Core checker
# ---------------------------------------------------------------------------

def _check_one(ing) -> dict:
    domain = ing.domain
    ns     = ing.namespace
    chks: dict = {}

    # ── 1. LoadBalancer ───────────────────────────────────────────────────
    lb = ing.address or ""
    if not lb:
        for jp in ("{.status.loadBalancer.ingress[0].ip}",
                   "{.status.loadBalancer.ingress[0].hostname}"):
            r = run_kubectl(["get", "ingress", ing.name, "-n", ns,
                             "-o", f"jsonpath={jp}"])
            if r.success and r.output.strip():
                lb = r.output.strip()
                break

    chks["lb"] = {
        "ok":      bool(lb),
        "address": lb or None,
        "error":   "LoadBalancer pending — no external IP/hostname yet" if not lb else None,
    }

    # ── 2. DNS ────────────────────────────────────────────────────────────
    try:
        resolved = list({a[4][0] for a in socket.getaddrinfo(domain, None)})
        lb_ips: list[str] = []
        if lb:
            try:
                lb_ips = list({a[4][0] for a in socket.getaddrinfo(lb, None)})
            except Exception:
                lb_ips = [lb]
        points = any(ip in lb_ips for ip in resolved) if lb_ips else None
        chks["dns"] = {
            "ok":           True,
            "resolved_ips": resolved,
            "lb_ips":       lb_ips,
            "points_to_lb": points,
            "warning":      (None if points
                             else "DNS resolves but IPs differ from LB — "
                                  "check registrar A/CNAME record"),
        }
    except socket.gaierror as exc:
        chks["dns"] = {
            "ok":    False,
            "error": f"DNS not resolving: {exc}",
            "hint":  "Add a CNAME record in GoDaddy pointing to the LB hostname",
        }

    # ── 3. TLS section on Ingress (most common miss) ──────────────────────
    tls_hosts:  list[str] = []
    tls_secret: str       = ""
    has_cm_ann            = False

    r_ing = run_kubectl(["get", "ingress", ing.name, "-n", ns, "-o", "json"])
    if r_ing.success:
        try:
            obj     = json.loads(r_ing.output)
            ann     = obj.get("metadata", {}).get("annotations", {})
            has_cm_ann = bool(ann.get("cert-manager.io/cluster-issuer") or
                              ann.get("cert-manager.io/issuer"))
            for t in obj.get("spec", {}).get("tls", []):
                tls_hosts.extend(t.get("hosts", []))
                tls_secret = tls_secret or t.get("secretName", "")
        except Exception:
            pass

    in_tls = domain in tls_hosts
    chks["tls_section"] = {
        "ok":           in_tls,
        "hosts":        tls_hosts,
        "secret_name":  tls_secret or None,
        "has_cm_ann":   has_cm_ann,
        "error": (
            "Ingress has cert-manager annotation but NO spec.tls — "
            "patch it so cert-manager issues the cert in the right namespace"
            if has_cm_ann and not in_tls else
            "No TLS section and no cert-manager annotation" if not in_tls else None
        ),
    }

    # ── 4. cert-manager Certificate (namespace match) ─────────────────────
    cm_certs  = get_cert_manager_certificates()
    cm_same   = next((c for c in cm_certs if c.domain == domain
                      and c.namespace == ns), None)
    cm_other  = next((c for c in cm_certs if c.domain == domain), None)

    if cm_same:
        chks["tls_cert"] = {
            "ok":       cm_same.ready,
            "namespace": ns,
            "ready":    cm_same.ready,
            "expiry":   cm_same.expiry,
            "message":  cm_same.message,
            "error":    f"Certificate not ready: {cm_same.message}" if not cm_same.ready else None,
        }
    elif cm_other:
        chks["tls_cert"] = {
            "ok":          False,
            "namespace":   cm_other.namespace,
            "expected_ns": ns,
            "ready":       cm_other.ready,
            "error":       (f"Certificate is in namespace '{cm_other.namespace}' "
                            f"but Ingress is in '{ns}' — "
                            f"patch Ingress spec.tls to re-issue in correct namespace"),
        }
    else:
        chks["tls_cert"] = {
            "ok":    False,
            "error": f"No cert-manager Certificate for {domain} in any namespace",
        }

    # ── 5. TLS Secret in Ingress namespace ────────────────────────────────
    secret_name = tls_secret or f"{domain.replace('.', '-')}-tls"
    si = get_tls_secret(secret_name, ns)

    # Pattern E: Ingress points to a non-cert-manager secret while a
    # cert-manager managed secret exists for the same domain.
    # The active secret may work today but won't auto-renew.
    cm_managed_name = None
    if cm_same and cm_same.ready:
        # Get the secretName from the cert-manager Certificate resource
        r_cert = run_kubectl(["get", "certificate", "-n", ns,
                              "-o", f"jsonpath={{.items[?(@.spec.dnsNames[0]==\"{domain}\")].spec.secretName}}"])
        if r_cert.success and r_cert.output.strip():
            cm_managed_name = r_cert.output.strip().split()[0]

    unmanaged_but_works = (
        si and not si.is_expired and
        cm_managed_name and
        secret_name != cm_managed_name
    )

    if si:
        chks["tls_secret"] = {
            "ok":           not si.is_expired and not unmanaged_but_works,
            "name":         si.name,
            "issuer":       si.issuer,
            "expiry":       si.expiry_date,
            "days_left":    si.days_until_expiry,
            "unmanaged":    unmanaged_but_works,
            "managed_name": cm_managed_name,
            "error": (
                f"Certificate expired {abs(si.days_until_expiry)} days ago"
                if si.is_expired else
                f"Ingress uses unmanaged secret '{secret_name}' — "
                f"cert-manager manages '{cm_managed_name}' and will auto-renew it; "
                f"patch ingress to use the managed secret"
                if unmanaged_but_works else None
            ),
        }
    else:
        chks["tls_secret"] = {
            "ok":    False,
            "error": f"TLS secret '{secret_name}' not found in namespace '{ns}'",
        }

    # ── 6-7. HTTP / HTTPS ─────────────────────────────────────────────────
    chks["http"]  = _probe(f"http://{domain}")
    chks["https"] = _probe(f"https://{domain}")

    # ── 8. Backend endpoints ──────────────────────────────────────────────
    svc = ing.backend_service
    if svc:
        ep = run_kubectl(["get", "endpoints", svc, "-n", ns,
                          "-o", "jsonpath={.subsets[*].addresses}"])
        has_ep = ep.success and ep.output.strip() not in ("", "null")
        chks["backend"] = {
            "ok":    has_ep,
            "service": svc,
            "error": f"Service '{svc}' has no ready endpoints" if not has_ep else None,
        }
    else:
        chks["backend"] = {"ok": None, "note": "Backend service not detected"}

    # ── Deterministic fix resolution ──────────────────────────────────────
    # We resolve fix_type BEFORE calling Claude so Claude only adds narration.
    fix_type    = "none"
    fix_command: str | None = None
    root_cause  = ""
    overall     = "live"
    confidence  = "high"

    if not chks["lb"]["ok"]:
        overall    = "down"
        root_cause = "LoadBalancer has no external IP — check cloud provider / NodePort setup"

    elif not chks["dns"]["ok"]:
        overall    = "down"
        root_cause = (f"DNS not resolving for {domain} — "
                      "add a CNAME record pointing to the LB hostname")

    elif not chks["tls_section"]["ok"]:
        overall   = "down"
        _sec      = tls_secret or f"{domain.replace('.', '-')}-tls"
        fix_type  = "patch_ingress_tls"
        fix_command = (
            f"kubectl patch ingress {ing.name} -n {ns} --type=merge "
            f"-p '{{\"spec\":{{\"tls\":[{{\"hosts\":[\"{domain}\"],"
            f"\"secretName\":\"{_sec}\"}}]}}}}'"
        )
        root_cause = (
            f"Ingress '{ing.name}' has cert-manager annotation "
            f"but no spec.tls section — cert is issued in wrong namespace "
            f"so nginx has no certificate to serve"
            if has_cm_ann else
            f"Ingress '{ing.name}' has no TLS configuration at all"
        )

    elif not chks["tls_cert"]["ok"]:
        wrong_ns  = chks["tls_cert"].get("namespace", "")
        if wrong_ns and wrong_ns != ns:
            overall   = "down"
            _sec      = f"{domain.replace('.', '-')}-tls"
            fix_type  = "patch_ingress_tls"
            fix_command = (
                f"kubectl patch ingress {ing.name} -n {ns} --type=merge "
                f"-p '{{\"spec\":{{\"tls\":[{{\"hosts\":[\"{domain}\"],"
                f"\"secretName\":\"{_sec}\"}}]}}}}'"
            )
            root_cause = (
                f"Certificate is in namespace '{wrong_ns}' "
                f"but Ingress is in '{ns}' — nginx cannot read it"
            )
        else:
            overall    = "degraded"
            fix_type   = "restart_cert"
            fix_command = (
                f"kubectl delete certificaterequest -n {ns} "
                f"-l cert-manager.io/certificate-name={domain.replace('.', '-')}-tls"
            )
            root_cause = f"cert-manager Certificate not ready: {chks['tls_cert'].get('message','')}"

    elif chks["tls_secret"].get("unmanaged"):
        # Pattern E: working but unmanaged → will expire without renewal
        managed = chks["tls_secret"].get("managed_name", "")
        overall   = "degraded"
        confidence = "high"
        fix_type  = "patch_ingress_tls"
        fix_command = (
            f"kubectl patch ingress {ing.name} -n {ns} --type=merge "
            f"-p '{{\"spec\":{{\"tls\":[{{\"hosts\":[\"{domain}\"],"
            f"\"secretName\":\"{managed}\"}}]}}}}'"
        )
        root_cause = (
            f"Ingress uses unmanaged secret '{secret_name}' — "
            f"cert-manager manages '{managed}' which auto-renews; "
            f"patch ingress to switch to the managed secret"
        )

    elif not chks["tls_secret"]["ok"]:
        overall    = "down"
        root_cause = f"TLS secret missing in namespace '{ns}' — cert-manager may not have issued yet"
        confidence = "medium"

    elif not chks["https"]["ok"]:
        code = chks["https"].get("status_code", "")
        overall    = "degraded"
        root_cause = f"HTTPS not reachable (HTTP {code}) — backend may be down or ingress routing wrong"
        confidence = "medium"

    elif not chks["backend"]["ok"]:
        overall    = "degraded"
        root_cause = f"Service '{svc}' has no ready endpoints — pods may be crashing"
        confidence = "medium"

    elif chks["dns"].get("warning"):
        overall    = "degraded"
        root_cause = "DNS resolves but to different IPs than LB — registrar A/CNAME may be stale"
        confidence = "medium"

    # ── Claude for narrative (non-blocking) ───────────────────────────────
    ai_analysis    = ""
    suggested_fix  = root_cause
    next_steps: list[str] = []

    if fix_command:
        next_steps.append(fix_command)
    if fix_type == "patch_ingress_tls":
        next_steps += [
            f"kubectl get certificate -n {ns} -w  (watch until Ready)",
            f"curl -I https://{domain}",
        ]

    failing_layers = [k for k, v in chks.items()
                      if isinstance(v, dict) and v.get("ok") is False]
    if failing_layers or overall != "live":
        try:
            prompt = (
                f"Domain: {domain}  Namespace: {ns}  Ingress: {ing.name}\n"
                f"Status: {overall}  Root cause: {root_cause}\n\n"
                f"Layers:\n{json.dumps(chks, indent=2)}"
            )
            raw = asyncio.run(llm.chat(
                messages=[{"role": "user", "content": prompt}],
                system=_SYSTEM,
                json_mode=True,
                max_tokens=400,
            ))
            parsed = json.loads(raw.content.strip())
            ai_analysis   = parsed.get("ai_analysis", "")
            suggested_fix = parsed.get("suggested_fix", "") or suggested_fix
            next_steps    = parsed.get("next_steps", next_steps) or next_steps
        except Exception:
            pass

    return {
        "found":   True,
        "domain":  domain,
        "ingress": ing,
        "checks":  chks,
        "diagnosis": {
            "overall_status": overall,
            "root_cause":     root_cause,
            "ai_analysis":    ai_analysis,
            "suggested_fix":  suggested_fix,
            "fix_command":    fix_command,
            "fix_type":       fix_type,
            "confidence":     confidence,
            "next_steps":     next_steps,
        },
    }


def _probe(url: str) -> dict:
    import shutil
    if shutil.which("curl"):
        try:
            r = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--max-time", "7", "-L", "--insecure", url],
                capture_output=True, text=True, timeout=10,
            )
            code = r.stdout.strip()
            ok   = bool(code) and code[0] in ("2", "3")
            return {"ok": ok, "status_code": code, "url": url}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:80], "url": url}
    try:
        import ssl, urllib.request
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        resp = urllib.request.urlopen(url, timeout=7, context=ctx)  # type: ignore[arg-type]
        return {"ok": True, "status_code": str(resp.status), "url": url}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:80], "url": url}
