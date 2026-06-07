"""
TLSSkill — diagnose and fix every TLS/certificate problem.

Problem coverage:
  CertExpired            Secret or cert-manager Certificate is expired
  CertExpiringSoon       Expires within 30 days
  CertManagerNotInstalled  cert-manager CRDs / pods absent
  CertManagerNotRunning  cert-manager pods crashing or not ready
  CertificateNotReady    cert-manager Certificate resource not Ready
  IssuerNotReady         Issuer or ClusterIssuer not Ready
  AcmeChallengeFailing   HTTP-01 or DNS-01 challenge in errored/invalid state
  SecretMissing          Ingress references a TLS secret that doesn't exist
  SecretInvalid          TLS secret missing tls.crt or tls.key keys
  HostnameMismatch       Cert SAN doesn't cover the ingress hostname
  RateLimitHit           Let's Encrypt rate limit detected in message
  SelfSigned             Self-signed cert detected (warning)
  WrongIssuerRef         Certificate points to issuer that doesn't exist
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

from agent.core import context, llm
from agent.core.models import (
    TLSDiagnosis,
    TLSIssue,
    TLSProblemType,
    TLSScanReport,
)
from agent.integrations.kubectl import run_kubectl
from agent.integrations.tls_collector import collect_all_tls
from agent.memory.retrieval import remember, retrieve_context
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SCAN_SYSTEM = """\
You are a senior Kubernetes SRE and TLS/PKI expert.
You receive a structured snapshot of all TLS certificates, cert-manager resources,
ACME challenges, and ingress TLS references in a Kubernetes cluster.
Identify every real issue. Do not invent issues not present in the data.

Return JSON only:
{
  "issues": [
    {
      "severity": "critical" | "warning" | "info",
      "problem_type": one of exactly:
        "CertExpired" | "CertExpiringSoon" | "CertManagerNotInstalled" |
        "CertManagerNotRunning" | "CertificateNotReady" | "IssuerNotReady" |
        "AcmeChallengeFailing" | "SecretMissing" | "SecretInvalid" |
        "HostnameMismatch" | "RateLimitHit" | "SelfSigned" |
        "WrongIssuerRef" | "Healthy" | "Unknown",
      "resource": "<secret name, cert name, ingress name, or issuer name>",
      "namespace": "<namespace or empty>",
      "description": "one-sentence description of the specific problem",
      "fix": "what to do to resolve this",
      "fix_command": "exact kubectl or cmctl command, or null",
      "deep_dive": true | false
    }
  ],
  "analysis": "2-3 sentence executive summary of overall TLS health",
  "cm_installed": true | false,
  "cm_healthy": true | false,
  "total_certs": <int>,
  "expired_certs": <int>,
  "expiring_certs": <int>
}

Fix guidance:
- CertExpired / CertExpiringSoon (cert-manager): kubectl annotate certificate <name> -n <ns> cert-manager.io/issueonce="true"  OR  cmctl renew <name> -n <ns>
- CertExpired (raw secret, no cert-manager): must re-issue manually via the original CA
- CertManagerNotRunning: kubectl rollout restart deployment -n cert-manager
- IssuerNotReady (ACME): check DNS records, HTTP challenge reachability, and Let's Encrypt account
- AcmeChallengeFailing (HTTP-01): check that port 80 is reachable from internet, ingress is working
- AcmeChallengeFailing (DNS-01): check DNS provider API credentials in the solver secret
- RateLimitHit: switch to staging issuer temporarily, wait 1 week, or use a different domain
- SecretMissing: create the secret manually or fix the Certificate spec secretName
- HostnameMismatch: update dnsNames in the Certificate spec or fix the ingress host
- deep_dive: true only when you need pod logs or YAML to determine root cause"""

_DIAGNOSE_SYSTEM = """\
You are a Kubernetes SRE and TLS expert diagnosing a specific certificate problem.
You have kubectl describe output, events, cert-manager logs, and issuer status.
Return JSON:
{
  "problem_type": "<TLSProblemType value>",
  "root_cause": "precise technical root cause",
  "suggested_fix": "what to do step by step",
  "fix_command": "exact kubectl/cmctl command, or null",
  "confidence": "high" | "medium" | "low",
  "explanation": "detailed technical explanation"
}"""


def _parse_json(content: str) -> dict:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text).strip()
    return json.loads(text)


def _to_problem_type(raw: str) -> TLSProblemType:
    try:
        return TLSProblemType(raw)
    except ValueError:
        return TLSProblemType.UNKNOWN


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class TLSSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "tls-diagnose"

    @property
    def description(self) -> str:
        return "Diagnose and fix TLS certificate, cert-manager, and ACME problems."

    def execute(self, input_data: dict) -> dict:
        mode = input_data.get("mode", "scan")
        if mode == "scan":
            return self.scan().model_dump()
        if mode == "diagnose":
            d = self.diagnose(input_data["name"], input_data["namespace"],
                              input_data.get("kind", "certificate"))
            return d.model_dump()
        if mode == "renew":
            return self.renew(input_data["name"], input_data["namespace"])
        raise ValueError(f"Unknown mode: {mode!r}")

    # ------------------------------------------------------------------
    # scan — all TLS areas, one Claude call
    # ------------------------------------------------------------------

    def scan(self) -> TLSScanReport:
        log.info("tls.scan.start")

        collection, elapsed_ms = collect_all_tls()

        report = TLSScanReport(
            generated_at  = datetime.now(timezone.utc).isoformat(),
            collection_ms = elapsed_ms,
        )

        ok_areas = [a for a, r in collection.items() if r.get("ok")]
        if not ok_areas:
            report.analysis = "All collectors failed — check cluster connectivity."
            return report

        # Build prompt
        sections = []
        for area, res in collection.items():
            sections.append(f"=== {area.upper()} ===\n{res.get('summary', '(no data)')}")
            if res.get("ok") and res.get("data"):
                try:
                    sections.append(json.dumps(res["data"], indent=2)[:1000])
                except Exception:
                    pass
        prompt = "\n\n".join(sections)

        try:
            response = asyncio.run(llm.chat(
                messages=[context.user_message(prompt)],
                system=_SCAN_SYSTEM,
                json_mode=True,
                max_tokens=3000,
            ))
            parsed = _parse_json(response.content)

            report.issues         = [TLSIssue(**i) for i in parsed.get("issues", [])]
            report.analysis       = parsed.get("analysis", "")
            report.cm_installed   = parsed.get("cm_installed", False)
            report.cm_healthy     = parsed.get("cm_healthy", False)
            report.total_certs    = parsed.get("total_certs", 0)
            report.expired_certs  = parsed.get("expired_certs", 0)
            report.expiring_certs = parsed.get("expiring_certs", 0)

            log.info("tls.scan.done", issues=len(report.issues))
        except Exception as exc:
            log.error("tls.scan.error", error=str(exc))
            report.analysis = f"Analysis failed: {exc}"
            return report

        # Deep-dive on critical issues that need it
        for issue in report.issues:
            if issue.deep_dive and issue.severity == "critical":
                enriched = self._deep_dive(issue, collection)
                if enriched:
                    issue.fix_command = enriched.get("fix_command") or issue.fix_command
                    issue.fix         = enriched.get("explanation") or issue.fix

        # Save to memory
        critical = sum(1 for i in report.issues if i.severity == "critical")
        warning  = sum(1 for i in report.issues if i.severity == "warning")
        remember(
            content=(
                f"TLS scan: {critical} critical, {warning} warnings. "
                f"cm_installed={report.cm_installed}, cm_healthy={report.cm_healthy}. "
                f"expired={report.expired_certs}, expiring={report.expiring_certs}. "
                f"{report.analysis}"
            ),
            source="tls-scan",
            metadata={"critical": critical, "warning": warning,
                      "cm_installed": report.cm_installed,
                      "expired_certs": report.expired_certs},
        )

        return report

    # ------------------------------------------------------------------
    # diagnose — deep AI analysis of one cert/secret/issuer
    # ------------------------------------------------------------------

    def diagnose(self, name: str, namespace: str,
                 kind: str = "certificate") -> TLSDiagnosis:
        log.info("tls.diagnose.start", name=name, namespace=namespace, kind=kind)

        kind_lower = kind.lower()

        # Collect describe + events for the resource
        if kind_lower == "certificate":
            desc   = run_kubectl(["describe", "certificate", name, "-n", namespace])
            yaml_r = run_kubectl(["get", "certificate", name, "-n", namespace, "-o", "yaml"])
        elif kind_lower in ("secret", "tls_secret"):
            desc   = run_kubectl(["describe", "secret", name, "-n", namespace])
            yaml_r = run_kubectl(["get", "secret", name, "-n", namespace, "-o", "yaml"])
        elif kind_lower in ("issuer", "clusterissuer"):
            desc   = run_kubectl(["describe", kind_lower, name,
                                  *(["-n", namespace] if kind_lower == "issuer" else [])])
            yaml_r = run_kubectl(["get", kind_lower, name,
                                  *(["-n", namespace] if kind_lower == "issuer" else []),
                                  "-o", "yaml"])
        else:
            desc   = run_kubectl(["describe", kind_lower, name, "-n", namespace])
            yaml_r = run_kubectl(["get", kind_lower, name, "-n", namespace, "-o", "yaml"])

        events = run_kubectl(["get", "events", "-n", namespace,
                              "--field-selector", f"involvedObject.name={name}",
                              "--sort-by=.lastTimestamp"])

        # cert-manager controller logs
        cm_logs = self._get_cm_logs()

        # Related CertificateRequest
        cr_out = ""
        if kind_lower == "certificate":
            cr_r = run_kubectl(["get", "certificaterequests", "-n", namespace,
                                "-o", "wide", "--no-headers"])
            if cr_r.success:
                matching = [l for l in cr_r.output.splitlines() if name in l]
                cr_out   = "\n".join(matching[:5])

        memories = retrieve_context(f"tls certificate {name} {namespace}")

        user_text = (
            f"Resource: {kind}/{name}\nNamespace: {namespace}\n\n"
            f"--- DESCRIBE ---\n{desc.output[:2000] if desc.success else desc.error[:200]}\n\n"
            f"--- YAML ---\n{yaml_r.output[:1500] if yaml_r.success else '(not available)'}\n\n"
            f"--- EVENTS ---\n{events.output[:800] if events.success else '(none)'}\n\n"
            f"--- CERT-MANAGER LOGS (last 60 lines) ---\n{cm_logs[:1500]}\n\n"
            + (f"--- CERTIFICATE REQUESTS ---\n{cr_out}\n" if cr_out else "")
        )

        system_prompt = context.build_system_prompt(_DIAGNOSE_SYSTEM, memories)

        response = asyncio.run(llm.chat(
            messages=[context.user_message(user_text)],
            system=system_prompt,
            json_mode=True,
            max_tokens=1500,
        ))
        parsed = _parse_json(response.content)

        diagnosis = TLSDiagnosis(
            name          = name,
            namespace     = namespace,
            problem_type  = _to_problem_type(parsed.get("problem_type", "Unknown")),
            root_cause    = parsed.get("root_cause", ""),
            suggested_fix = parsed.get("suggested_fix", ""),
            fix_command   = parsed.get("fix_command"),
            confidence    = parsed.get("confidence", "low"),
            explanation   = parsed.get("explanation", ""),
        )

        remember(
            content=(
                f"TLS {kind}/{name} in {namespace} diagnosed as "
                f"{diagnosis.problem_type.value}: {diagnosis.root_cause}. "
                f"Fix: {diagnosis.suggested_fix}"
            ),
            source="tls-diagnose",
            metadata={"name": name, "namespace": namespace, "kind": kind,
                      "problem_type": diagnosis.problem_type.value,
                      "confidence": diagnosis.confidence},
        )

        log.info("tls.diagnose.done", name=name, problem_type=diagnosis.problem_type.value)
        return diagnosis

    # ------------------------------------------------------------------
    # renew — trigger cert-manager certificate renewal
    # ------------------------------------------------------------------

    def renew(self, name: str, namespace: str) -> dict:
        """Trigger renewal of a cert-manager Certificate resource."""
        log.info("tls.renew.start", name=name, namespace=namespace)

        # Try cmctl first
        import shutil
        if shutil.which("cmctl"):
            r = run_kubectl.__wrapped__ if hasattr(run_kubectl, "__wrapped__") else None
            import subprocess
            result = subprocess.run(
                ["cmctl", "renew", name, "-n", namespace],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                remember(
                    content=f"Triggered renewal of Certificate {name}/{namespace} via cmctl",
                    source="tls-renew",
                    metadata={"name": name, "namespace": namespace, "method": "cmctl"},
                )
                return {"ok": True, "method": "cmctl", "output": result.stdout.strip()}

        # Fallback: annotate to trigger re-issue
        r = run_kubectl([
            "annotate", "certificate", name, "-n", namespace,
            "cert-manager.io/issueonce=true", "--overwrite",
        ])

        if r.success:
            remember(
                content=f"Triggered renewal of Certificate {name}/{namespace} via annotation",
                source="tls-renew",
                metadata={"name": name, "namespace": namespace, "method": "annotate"},
            )
            return {"ok": True, "method": "annotate", "output": r.output}

        # Fallback 2: delete the secret so cert-manager re-issues
        return {
            "ok": False,
            "error": r.error,
            "hint": (
                f"Try manually: kubectl delete secret <tls-secret-name> -n {namespace}\n"
                f"cert-manager will re-create it automatically if Certificate resource exists."
            ),
        }

    # ------------------------------------------------------------------
    # install_cert_manager — dedicated installer, asks per step
    # ------------------------------------------------------------------

    def install_cert_manager(self) -> dict:
        """
        Install cert-manager. Tries helm first, falls back to kubectl apply
        from the official manifest. Returns a list of step results.
        """
        import shutil
        import subprocess

        log.info("tls.install_cert_manager.start")
        steps = []

        def _run_step(label: str, cmd: list[str]) -> dict:
            log.info("tls.install_cert_manager.step", label=label, cmd=" ".join(cmd))
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                ok  = r.returncode == 0
                out = (r.stdout + r.stderr).strip()
                log.info("tls.install_cert_manager.step_done",
                         label=label, ok=ok)
                return {"label": label, "cmd": " ".join(cmd),
                        "ok": ok, "output": out[:400]}
            except Exception as exc:
                return {"label": label, "cmd": " ".join(cmd),
                        "ok": False, "output": str(exc)[:200]}

        if shutil.which("helm"):
            steps.append(_run_step(
                "Add jetstack helm repo",
                ["helm", "repo", "add", "jetstack", "https://charts.jetstack.io"],
            ))
            steps.append(_run_step(
                "Update helm repos",
                ["helm", "repo", "update"],
            ))
            steps.append(_run_step(
                "Install cert-manager",
                [
                    "helm", "install", "cert-manager", "jetstack/cert-manager",
                    "--namespace", "cert-manager",
                    "--create-namespace",
                    "--set", "installCRDs=true",
                ],
            ))
        else:
            # helm not available — use kubectl apply from official manifest
            MANIFEST = (
                "https://github.com/cert-manager/cert-manager/releases/latest/"
                "download/cert-manager.yaml"
            )
            steps.append(_run_step(
                "Install cert-manager via kubectl apply (no helm found)",
                ["kubectl", "apply", "-f", MANIFEST],
            ))

        # Wait for pods to be ready
        steps.append(_run_step(
            "Wait for cert-manager pods to be ready",
            [
                "kubectl", "wait", "--for=condition=ready", "pod",
                "-l", "app.kubernetes.io/instance=cert-manager",
                "-n", "cert-manager",
                "--timeout=120s",
            ],
        ))

        all_ok = all(s["ok"] for s in steps)
        remember(
            content=f"cert-manager install: {'succeeded' if all_ok else 'failed'}. "
                    + " | ".join(f"{s['label']}: {'OK' if s['ok'] else 'FAIL'}" for s in steps),
            source="tls-fix",
            metadata={"action": "install_cert_manager", "success": all_ok},
        )
        return {"ok": all_ok, "steps": steps}

    # ------------------------------------------------------------------
    # apply_fix — smart runner: handles kubectl, helm, any binary, && chains
    # ------------------------------------------------------------------

    def apply_fix(self, fix_command: str, resource: str, namespace: str) -> bool:
        import shutil
        import subprocess

        log.info("tls.apply_fix", command=fix_command)

        # Split on && and run each sub-command in sequence
        sub_commands = [c.strip() for c in fix_command.split("&&") if c.strip()]
        all_ok = True

        for raw_cmd in sub_commands:
            parts = raw_cmd.split()
            if not parts:
                continue

            binary = parts[0]

            if binary == "kubectl":
                # route through the safe kubectl wrapper
                r = run_kubectl(parts[1:])
                ok  = r.success
                out = r.output or r.error
            elif shutil.which(binary):
                # any other installed binary (helm, cmctl, openssl…)
                try:
                    result = subprocess.run(
                        parts, capture_output=True, text=True, timeout=60,
                    )
                    ok  = result.returncode == 0
                    out = (result.stdout + result.stderr).strip()
                except Exception as exc:
                    ok  = False
                    out = str(exc)
            else:
                log.warning("tls.apply_fix.binary_not_found", binary=binary)
                ok  = False
                out = f"'{binary}' not found — install it first"

            log.info("tls.apply_fix.step", cmd=raw_cmd[:80], ok=ok)
            if not ok:
                all_ok = False
                log.warning("tls.apply_fix.step_failed", cmd=raw_cmd[:80], output=out[:120])
                break   # stop chain on first failure, same as shell &&

        remember(
            content=f"Applied TLS fix for {resource}/{namespace}: `{fix_command}` — "
                    f"{'succeeded' if all_ok else 'failed'}",
            source="tls-fix",
            metadata={"resource": resource, "namespace": namespace,
                      "command": fix_command, "success": all_ok},
        )
        return all_ok

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_cm_logs(self, lines: int = 60) -> str:
        """Get cert-manager controller pod logs."""
        for ns in ("cert-manager", "kube-system", "default"):
            pods_r = run_kubectl([
                "get", "pods", "-n", ns, "-l", "app=cert-manager",
                "--no-headers", "-o", "custom-columns=NAME:.metadata.name",
            ])
            if pods_r.success and pods_r.output.strip():
                pod_name = pods_r.output.strip().splitlines()[0].strip()
                logs_r   = run_kubectl(["logs", pod_name, "-n", ns,
                                        f"--tail={lines}", "--timestamps=false"])
                if logs_r.success:
                    return logs_r.output
        return "(cert-manager logs not available)"

    def _deep_dive(self, issue: TLSIssue, collection: dict[str, dict]) -> dict | None:
        lines = [
            f"Issue:     {issue.problem_type.value}",
            f"Resource:  {issue.resource}",
            f"Namespace: {issue.namespace}",
            f"Problem:   {issue.description}",
            "",
        ]

        # Add cert-manager logs
        lines.append("=== CERT-MANAGER LOGS ===")
        lines.append(self._get_cm_logs(80))

        # Add relevant section from collection
        for area in ("cm_certificates", "cm_issuers", "acme_challenges"):
            data = collection.get(area, {}).get("data", {})
            if data.get("issues"):
                lines.append(f"\n=== {area.upper()} ISSUES ===")
                for i in data["issues"][:5]:
                    lines.append(f"  {i}")

        if issue.namespace and issue.resource:
            desc_r = run_kubectl(["describe", "certificate", issue.resource,
                                  "-n", issue.namespace])
            if desc_r.success:
                lines.append(f"\n=== CERTIFICATE DESCRIBE ===\n{desc_r.output[:1500]}")

        deep_system = """\
You are a TLS/PKI expert doing a deep-dive on a critical certificate issue.
Return JSON:
{
  "root_cause": "precise technical root cause",
  "fix_command": "exact kubectl command, or null",
  "explanation": "one concise sentence — what to do and why"
}"""

        try:
            response = asyncio.run(llm.chat(
                messages=[context.user_message("\n".join(lines))],
                system=deep_system,
                json_mode=True,
                max_tokens=512,
            ))
            return _parse_json(response.content)
        except Exception as exc:
            log.warning("tls.deep_dive.error", error=str(exc))
            return None
