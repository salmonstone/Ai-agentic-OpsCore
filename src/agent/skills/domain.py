"""
Domain / Let's Encrypt skill.

Provisions HTTPS certificates for domains via cert-manager + Let's Encrypt.
Works with HTTP-01 (nginx ingress) challenges — the most common setup.

Flow for `encrypt`:
  1. Verify cert-manager is running (or prompt to install)
  2. Create/verify ClusterIssuer letsencrypt-prod
  3. Find the Ingress serving the domain (or create a Certificate directly)
  4. Patch the Ingress with TLS section + cert-manager annotation
  5. Watch until Certificate is Ready

Because the Certificate resource is created in the cluster, `agent tls scan`
will automatically pick it up on the next run.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from agent.integrations.kubectl import run_kubectl
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# YAML templates
# ---------------------------------------------------------------------------

_CLUSTER_ISSUER_YAML = """\
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    email: {email}
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
    - http01:
        ingress:
          class: nginx
"""

_CLUSTER_ISSUER_STAGING_YAML = """\
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-staging
spec:
  acme:
    email: {email}
    server: https://acme-staging-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-staging-key
    solvers:
    - http01:
        ingress:
          class: nginx
"""

_CERTIFICATE_YAML = """\
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: {cert_name}
  namespace: {namespace}
spec:
  secretName: {secret_name}
  issuerRef:
    name: letsencrypt-prod
    kind: ClusterIssuer
  dnsNames:
  - {domain}
"""


class DomainSkill(BaseSkill):
    """Provision and manage Let's Encrypt TLS certificates for domains."""

    @property
    def name(self) -> str:
        return "domain"

    @property
    def description(self) -> str:
        return "Provision Let's Encrypt HTTPS certificates for domains via cert-manager."

    def execute(self, action: str = "scan", **kwargs) -> dict:
        if action == "scan":
            return self.scan(kwargs.get("domain", ""))
        if action == "encrypt":
            return self.encrypt(
                domain    = kwargs["domain"],
                email     = kwargs["email"],
                namespace = kwargs.get("namespace", "default"),
                ingress   = kwargs.get("ingress"),
                staging   = kwargs.get("staging", False),
            )
        if action == "live":
            return {"results": self.live(kwargs.get("domain"),
                                         kwargs.get("namespace"))}
        return {"ok": False, "error": f"unknown action: {action}"}

    # ------------------------------------------------------------------
    # live — end-to-end domain health check (DNS → LB → TLS → HTTP → pods)
    # ------------------------------------------------------------------

    def live(self, domain: str | None = None,
             namespace: str | None = None) -> list[dict]:
        """
        Check every layer that can cause a domain to not be live.

        Runs deterministic checks (no Claude required for well-known patterns):
          - LoadBalancer has external IP
          - DNS resolves and points to LB
          - Ingress has spec.tls section  ← most common miss
          - cert-manager Certificate is in the SAME namespace as Ingress
          - TLS Secret exists in Ingress namespace
          - HTTP / HTTPS respond
          - Backend service has ready endpoints

        Returns a list of result dicts — one per domain found.
        Each dict has: domain, ingress, checks{}, diagnosis{}.
        diagnosis.fix_type is one of: patch_ingress_tls | restart_cert | none
        diagnosis.fix_command is the exact kubectl command to run.
        """
        from agent.skills.domain_live import check_domain
        return check_domain(domain, namespace)

    def live_fix_ingress_tls(self, ingress_name: str, namespace: str,
                              domain: str, secret_name: str) -> dict:
        """Patch an Ingress to add a spec.tls section for the given domain."""
        from agent.skills.domain_live import fix_ingress_tls
        return fix_ingress_tls(ingress_name, namespace, domain, secret_name)

    def live_wait_cert(self, domain: str, namespace: str,
                       timeout: int = 90) -> dict:
        """Wait up to `timeout` seconds for the cert-manager cert to become Ready."""
        from agent.skills.domain_live import wait_for_cert
        return wait_for_cert(domain, namespace, timeout)

    # ------------------------------------------------------------------
    # scan — check TLS status for a domain
    # ------------------------------------------------------------------

    def scan(self, domain: str) -> dict:
        from rich.console import Console
        from rich.panel import Panel
        from rich.rule import Rule

        console = Console()
        console.print(Rule(f"[bold cyan]Domain TLS Status: {domain}[/bold cyan]"))

        issues = []
        details = {}

        # 1. HTTP reachability (external check via curl, best-effort)
        http_ok, http_msg = self._check_http(domain)
        details["http"] = {"ok": http_ok, "msg": http_msg}
        if not http_ok:
            issues.append(f"HTTP unreachable: {http_msg}")

        # 2. Certificate resources in cluster that cover this domain
        cert_r = run_kubectl(["get", "certificates", "-A", "-o", "json"])
        matching_certs = []
        if cert_r.success:
            for cert in json.loads(cert_r.output).get("items", []):
                dns_names = cert.get("spec", {}).get("dnsNames", [])
                if domain in dns_names or f"*.{'.'.join(domain.split('.')[1:])}" in dns_names:
                    cond_map = {
                        c["type"]: c["status"]
                        for c in cert.get("status", {}).get("conditions", [])
                    }
                    matching_certs.append({
                        "name":      cert["metadata"]["name"],
                        "namespace": cert["metadata"]["namespace"],
                        "ready":     cond_map.get("Ready") == "True",
                        "not_after": cert.get("status", {}).get("notAfter", ""),
                    })

        details["certificates"] = matching_certs

        # 3. TLS secrets
        secret_r = run_kubectl([
            "get", "secrets", "-A",
            "--field-selector=type=kubernetes.io/tls",
            "-o", "json",
        ])
        matching_secrets = []
        if secret_r.success:
            for s in json.loads(secret_r.output).get("items", []):
                ann = s.get("metadata", {}).get("annotations", {})
                # cert-manager annotates with the cert name
                if domain in json.dumps(ann):
                    matching_secrets.append(
                        f"{s['metadata']['namespace']}/{s['metadata']['name']}"
                    )
        details["tls_secrets"] = matching_secrets

        # 4. Ingress resources that reference this domain
        ing_r = run_kubectl(["get", "ingress", "-A", "-o", "json"])
        matching_ingress = []
        if ing_r.success:
            for ing in json.loads(ing_r.output).get("items", []):
                for rule in ing.get("spec", {}).get("rules", []):
                    if rule.get("host") == domain:
                        tls_hosts = [
                            h
                            for t in ing.get("spec", {}).get("tls", [])
                            for h in t.get("hosts", [])
                        ]
                        matching_ingress.append({
                            "name":      ing["metadata"]["name"],
                            "namespace": ing["metadata"]["namespace"],
                            "has_tls":   domain in tls_hosts,
                        })

        details["ingress"] = matching_ingress

        # ── Render ──────────────────────────────────────────────────────────
        if matching_certs:
            for c in matching_certs:
                expiry = c["not_after"][:10] if c["not_after"] else "unknown"
                color  = "green" if c["ready"] else "red"
                console.print(
                    f"  Certificate [{color}]{'Ready' if c['ready'] else 'NOT READY'}[/{color}]  "
                    f"[cyan]{c['namespace']}/{c['name']}[/cyan]  expiry: {expiry}"
                )
        else:
            console.print(f"  [yellow]No cert-manager Certificate found for {domain}[/yellow]")
            issues.append("No Certificate resource found — run: agent domain encrypt")

        for ing in matching_ingress:
            tls_label = "[green]TLS configured[/green]" if ing["has_tls"] else "[red]No TLS[/red]"
            console.print(
                f"  Ingress {tls_label}  "
                f"[cyan]{ing['namespace']}/{ing['name']}[/cyan]"
            )
            if not ing["has_tls"]:
                issues.append(f"Ingress {ing['name']} has no TLS section")

        if not matching_ingress:
            console.print(f"  [dim]No Ingress found with host={domain}[/dim]")

        console.print()
        if issues:
            console.print(Panel(
                "\n".join(f"  • {i}" for i in issues)
                + f"\n\n  [dim]Fix:[/dim]  agent domain encrypt {domain} --email you@example.com",
                title="[bold red]Issues[/bold red]",
                border_style="red",
            ))
        else:
            console.print(Panel(
                f"[bold green]Domain {domain} has a valid TLS certificate.[/bold green]\n"
                "[dim]Run[/dim] agent tls scan [dim]for full certificate audit.[/dim]",
                border_style="green",
            ))

        return {"ok": not issues, "issues": issues, "details": details}

    # ------------------------------------------------------------------
    # encrypt — provision Let's Encrypt cert
    # ------------------------------------------------------------------

    def encrypt(
        self,
        domain:    str,
        email:     str,
        namespace: str  = "default",
        ingress:   str | None = None,
        staging:   bool = False,
    ) -> dict:
        from rich.console import Console
        from rich.panel import Panel
        from rich.rule import Rule

        console = Console()
        console.print(Rule(f"[bold cyan]Let's Encrypt — {domain}[/bold cyan]"))

        steps   = []
        success = True

        def step(label: str, ok: bool, detail: str = "") -> None:
            icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
            console.print(f"  {icon}  {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))
            steps.append({"label": label, "ok": ok, "detail": detail})

        # ── 1. cert-manager check ────────────────────────────────────────────
        cm_ok = self._cert_manager_running()
        step("cert-manager is running", cm_ok,
             "" if cm_ok else "Run: agent tls heal  to install it first")
        if not cm_ok:
            return {"ok": False, "steps": steps,
                    "error": "cert-manager not running — install it first with: agent tls heal"}

        # ── 2. ClusterIssuer ─────────────────────────────────────────────────
        issuer_name = "letsencrypt-staging" if staging else "letsencrypt-prod"
        issuer_ok   = self._ensure_cluster_issuer(issuer_name, email, staging)
        step(f"ClusterIssuer {issuer_name}", issuer_ok)
        if not issuer_ok:
            success = False

        # ── 3. Find or accept Ingress ────────────────────────────────────────
        found_ingress = ingress or self._find_ingress_for_domain(domain, namespace)

        if found_ingress:
            # Patch the Ingress with TLS + annotation
            cert_secret = _safe_name(domain) + "-tls"
            patch_ok    = self._patch_ingress_tls(found_ingress, namespace, domain,
                                                   cert_secret, issuer_name)
            step(f"Patched Ingress {found_ingress} with TLS", patch_ok,
                 f"secretName={cert_secret}")
            if not patch_ok:
                success = False
        else:
            # No ingress found — create a standalone Certificate resource
            cert_name   = _safe_name(domain) + "-cert"
            secret_name = _safe_name(domain) + "-tls"
            cert_ok     = self._create_certificate(cert_name, secret_name,
                                                    domain, namespace, issuer_name)
            step(f"Created Certificate {cert_name}", cert_ok,
                 f"namespace={namespace} secret={secret_name}")
            if not cert_ok:
                success = False

        # ── 4. Wait and watch ────────────────────────────────────────────────
        console.print()
        if success:
            console.print(
                "  [cyan]Watching for certificate to become Ready...[/cyan]  "
                "[dim](up to 2 min — ACME HTTP-01 challenge)[/dim]"
            )
            ready, wait_msg = self._wait_for_cert(domain, namespace, timeout=120)
            step("Certificate issued and Ready", ready, wait_msg)
            if not ready:
                success = False
                console.print(Panel(
                    "[yellow]Certificate is not yet Ready.[/yellow]\n\n"
                    "This is normal — ACME HTTP-01 can take 1-3 minutes.\n\n"
                    f"  Watch:  kubectl get certificate -n {namespace} -w\n"
                    f"  Events: kubectl describe certificate -n {namespace}\n"
                    "  Debug:  kubectl get challenges -A",
                    title="Still provisioning",
                    border_style="yellow",
                ))
            else:
                console.print(Panel(
                    f"[bold green]HTTPS is now active for {domain}![/bold green]\n\n"
                    "[dim]cert-manager will auto-renew 30 days before expiry.[/dim]\n"
                    "[dim]Run[/dim] agent tls scan [dim]to see this cert in the full TLS report.[/dim]",
                    border_style="green",
                ))

        return {"ok": success, "steps": steps, "domain": domain,
                "issuer": issuer_name, "namespace": namespace}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _cert_manager_running(self) -> bool:
        for ns in ("cert-manager", "kube-system", "default"):
            r = run_kubectl(["get", "pods", "-n", ns, "-l", "app=cert-manager",
                             "--no-headers"])
            if r.success and "Running" in r.output:
                return True
        return False

    def _ensure_cluster_issuer(self, name: str, email: str, staging: bool) -> bool:
        # Check if already exists
        r = run_kubectl(["get", "clusterissuer", name, "--ignore-not-found",
                         "--no-headers"])
        if r.success and r.output.strip():
            log.info("domain.issuer.exists", name=name)
            return True

        # Create it
        template = _CLUSTER_ISSUER_STAGING_YAML if staging else _CLUSTER_ISSUER_YAML
        yaml_str = template.format(email=email)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml",
                                        delete=False) as f:
            f.write(yaml_str)
            tmp = f.name

        r = run_kubectl(["apply", "-f", tmp])
        Path(tmp).unlink(missing_ok=True)
        return r.success

    def _find_ingress_for_domain(self, domain: str, namespace: str) -> str | None:
        r = run_kubectl(["get", "ingress", "-n", namespace, "-o", "json"])
        if not r.success:
            return None
        for ing in json.loads(r.output).get("items", []):
            for rule in ing.get("spec", {}).get("rules", []):
                if rule.get("host") == domain:
                    return ing["metadata"]["name"]
        return None

    def _patch_ingress_tls(self, ingress_name: str, namespace: str,
                            domain: str, secret_name: str,
                            issuer_name: str) -> bool:
        # Add cert-manager annotation
        ann_r = run_kubectl([
            "annotate", "ingress", ingress_name,
            "-n", namespace,
            f"cert-manager.io/cluster-issuer={issuer_name}",
            "--overwrite",
        ])

        # Build TLS patch JSON
        tls_patch = json.dumps({
            "spec": {
                "tls": [{"hosts": [domain], "secretName": secret_name}]
            }
        })
        patch_r = run_kubectl([
            "patch", "ingress", ingress_name,
            "-n", namespace,
            "--type=merge",
            f"--patch={tls_patch}",
        ])
        return ann_r.success and patch_r.success

    def _create_certificate(self, cert_name: str, secret_name: str,
                             domain: str, namespace: str,
                             issuer_name: str) -> bool:
        yaml_str = _CERTIFICATE_YAML.format(
            cert_name   = cert_name,
            namespace   = namespace,
            secret_name = secret_name,
            domain      = domain,
        ).replace("letsencrypt-prod", issuer_name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml",
                                         delete=False) as f:
            f.write(yaml_str)
            tmp = f.name

        r = run_kubectl(["apply", "-f", tmp])
        Path(tmp).unlink(missing_ok=True)
        return r.success

    def _wait_for_cert(self, domain: str, namespace: str,
                        timeout: int = 120) -> tuple[bool, str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = run_kubectl(["get", "certificates", "-n", namespace, "-o", "json"])
            if r.success:
                for cert in json.loads(r.output).get("items", []):
                    dns_names = cert.get("spec", {}).get("dnsNames", [])
                    if domain not in dns_names:
                        continue
                    for cond in cert.get("status", {}).get("conditions", []):
                        if cond["type"] == "Ready" and cond["status"] == "True":
                            expiry = cert.get("status", {}).get("notAfter", "unknown")
                            return True, f"expires {expiry[:10]}"
            time.sleep(8)
        return False, "timed out — check kubectl get challenges -A"

    def _check_http(self, domain: str) -> tuple[bool, str]:
        import shutil
        if not shutil.which("curl"):
            return True, "curl not available — skipping HTTP check"
        try:
            r = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--max-time", "5", f"http://{domain}"],
                capture_output=True, text=True, timeout=8,
            )
            code = r.stdout.strip()
            ok   = code.startswith("2") or code.startswith("3")
            return ok, f"HTTP {code}"
        except Exception as exc:
            return False, str(exc)[:60]


def _safe_name(domain: str) -> str:
    """Convert domain.com → domain-com for use as k8s resource name."""
    return domain.replace(".", "-").replace("*", "wildcard")[:52]
