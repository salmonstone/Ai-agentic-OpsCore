"""
Security Audit Skill — production-smart K8s security scanner.

Thinks like a senior SRE: distinguishes real issues from AWS/K8s managed
components, scores only actionable findings, and surfaces what actually
matters instead of drowning engineers in noise.

Checks:
  privilege   — privileged containers (skips AWS/K8s system components)
  secret      — sensitive env vars (skips IRSA/AWS file-path patterns)
  rbac        — cluster-admin misuse, anonymous access
  network     — namespaces without NetworkPolicy (skips system namespaces)
  runtime     — containers running as root (skips AWS/K8s managed)
  exposure    — LoadBalancer/NodePort (marks ingress controllers as expected)
"""
from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import SecurityFinding, SecurityReport
from agent.integrations.kubectl import (
    get_current_context,
    get_deployment_for_pod,
    get_exposed_services,
    get_network_policy_gaps,
    get_pods_running_as_root,
    get_privileged_pods,
    get_rbac_issues,
    get_secrets_in_env,
    run_kubectl,
)
from agent.memory.retrieval import remember
from agent.observability.logging import get_logger
from agent.skills._fix_runner import apply_shell_fix
from agent.skills.base import BaseSkill

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Intelligence constants — what's expected vs what's a real issue
# ---------------------------------------------------------------------------

PRIVILEGED_SYSTEM_COMPONENTS: dict[str, str] = {
    "aws-node":               "AWS VPC CNI plugin — requires host network access",
    "aws-eks-nodeagent":      "AWS EKS node agent — AWS managed",
    "ebs-plugin":             "AWS EBS CSI driver — requires host volume access",
    "ebs-csi-node":           "AWS EBS CSI node — requires privileged for mounts",
    "node-driver-registrar":  "CSI driver registrar — AWS managed",
    "liveness-probe":         "CSI liveness probe — AWS managed",
    "kube-proxy":             "K8s core networking — requires host network",
    "coredns":                "K8s DNS — core system component",
    "ingress-nginx":          "Ingress controller — expected LoadBalancer",
    "ingress-nginx-controller": "Ingress controller — expected",
    "traefik":                "Traefik ingress — expected privileged",
    "istio-proxy":            "Istio sidecar — expected",
}

SYSTEM_NAMESPACES: frozenset[str] = frozenset({
    "kube-system", "kube-public", "kube-node-lease",
    "ingress-nginx", "cert-manager", "amazon-cloudwatch",
    "aws-load-balancer-controller", "external-dns",
    "cluster-autoscaler",
})

# AWS IRSA / K8s injected env vars that look like secrets but aren't
FALSE_POSITIVE_ENV_VARS: frozenset[str] = frozenset({
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
    "KUBERNETES_SERVICE_HOST",
    "KUBERNETES_SERVICE_PORT",
    "AWS_STS_REGIONAL_ENDPOINTS",
    "AWS_DEFAULT_REGION",
})

# RBAC binding names that are K8s system bindings — these are expected
_SYSTEM_BINDING_PREFIXES = (
    "system:", "eks:", "aws-node", "kube-proxy",
    "cluster-autoscaler", "cert-manager", "external-dns",
)

# ---------------------------------------------------------------------------
# Claude analysis system prompt — senior SRE perspective
# ---------------------------------------------------------------------------

_ANALYZE_SYSTEM = """\
You are a senior SRE and Kubernetes security expert with deep knowledge of AWS EKS architecture.

IGNORE these — they are expected, not security issues:
- AWS VPC CNI (aws-node) running privileged — required for pod networking
- EBS CSI driver (ebs-plugin, node-driver-registrar) running privileged — required for volume mounts
- kube-proxy running privileged — required for iptables/ipvs rules
- CoreDNS running as root — K8s core component
- AWS_WEB_IDENTITY_TOKEN_FILE, AWS_ROLE_ARN in env vars — IRSA pattern, not a credential leak
- system: prefixed RBAC bindings — K8s built-in, expected
- ingress-nginx as LoadBalancer — expected and required for HTTP ingress

FOCUS on these as real issues:
- APPLICATION containers running as root (not system components)
- Application sidecars running privileged (not AWS/K8s managed)
- Actual credentials (passwords, API keys, tokens) hardcoded in env vars
- BINDINGS of cluster-admin to non-system human or service accounts
- Missing NetworkPolicy in application namespaces
- Production services on unexpected ports without ingress

For each REAL finding:
1. Exact attack vector in plain English
2. What an attacker could gain (data, lateral movement, cluster takeover)
3. Step-by-step kubectl remediation commands
4. How to verify the fix worked

Be concise. Skip system components entirely. Prioritise by real-world exploitability."""

_SEV_DEDUCTIONS: dict[str, int] = {
    "critical": 15,
    "high":      8,
    "medium":    4,
    "low":       1,
    "info":      0,
}


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class SecurityAuditSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "security-audit"

    @property
    def description(self) -> str:
        return (
            "Audit Kubernetes cluster for security misconfigurations. "
            "Distinguishes real issues from AWS/K8s system components."
        )

    def execute(self, input_data: dict) -> dict:
        report = self.run_audit(input_data.get("namespace", "all"))
        return report.model_dump()

    # ------------------------------------------------------------------
    # Main audit
    # ------------------------------------------------------------------

    def run_audit(self, namespace: str = "all") -> SecurityReport:
        """Run all security checks in parallel, score, analyse, return report."""
        checks = {
            "privileged":  lambda: get_privileged_pods(namespace),
            "secrets_env": lambda: get_secrets_in_env(namespace),
            "rbac":        lambda: get_rbac_issues(),
            "network":     lambda: get_network_policy_gaps(namespace),
            "root":        lambda: get_pods_running_as_root(namespace),
            "exposure":    lambda: get_exposed_services(namespace),
        }

        raw: dict[str, list] = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(fn): key for key, fn in checks.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    raw[key] = future.result()
                except Exception as exc:
                    log.warning("security.check_failed", check=key, error=str(exc))
                    raw[key] = []

        findings: list[SecurityFinding] = []
        counters = {"critical": 0, "high": 0, "medium": 0, "low": 0,
                    "info": 0, "system_component": 0}
        idx: dict[str, int] = {"c": 0, "h": 0, "m": 0, "l": 0, "i": 0}

        def _add(f: SecurityFinding) -> None:
            sev    = f.severity
            letter = "i" if sev == "info" else sev[0].lower()
            prefix = "I" if sev == "info" else sev[0].upper()
            idx[letter] += 1
            f.id = f"{prefix}{idx[letter]}"
            findings.append(f)
            if sev in counters:
                counters[sev] += 1
            if f.category == "system_component":
                counters["system_component"] += 1

        # ── Privileged containers ──────────────────────────────────────
        for item in raw.get("privileged", []):
            for issue in item["issues"]:
                is_fp, reason = self.is_false_positive(
                    "privileged", item["container"], item["namespace"], item["container"]
                )
                if is_fp:
                    _add(SecurityFinding(
                        id="", severity="info", category="system_component",
                        title=f"Expected: {item['container']} is a system component",
                        description=reason,
                        affected_resource=item["pod"],
                        namespace=item["namespace"],
                        evidence=f"Container '{item['container']}' — {issue}",
                        recommendation="No action required — AWS/K8s managed component.",
                        fix_command=None,
                    ))
                else:
                    _add(SecurityFinding(
                        id="", severity="critical", category="privilege",
                        title=f"Privileged container: {item['container']}",
                        description=issue,
                        affected_resource=item["pod"],
                        namespace=item["namespace"],
                        evidence=f"Container '{item['container']}' — {issue}",
                        recommendation=(
                            "Remove privileged flag from container securityContext. "
                            "Run --fix to auto-patch the owning Deployment/DaemonSet."
                        ),
                        fix_command=None,
                        cve_reference="CIS Benchmark 5.2.1",
                    ))

        # ── Secrets in env vars ────────────────────────────────────────
        for item in raw.get("secrets_env", []):
            is_fp, reason = self.is_false_positive(
                "secret_env", "", item["namespace"], item["env_var"]
            )
            if is_fp:
                _add(SecurityFinding(
                    id="", severity="info", category="system_component",
                    title=f"Expected env var: {item['env_var']}",
                    description=reason,
                    affected_resource=item["pod"],
                    namespace=item["namespace"],
                    evidence=f"env var: {item['env_var']}",
                    recommendation="No action required — this is an AWS IRSA pattern.",
                    fix_command=None,
                ))
            else:
                _add(SecurityFinding(
                    id="", severity="high", category="secret",
                    title=f"Secret in plain env var: {item['env_var']}",
                    description=(
                        f"Pod '{item['pod']}' has sensitive env var '{item['env_var']}' "
                        f"as a hardcoded plain value, not a SecretKeyRef."
                    ),
                    affected_resource=item["pod"],
                    namespace=item["namespace"],
                    evidence=f"env var name: {item['env_var']} (value hidden)",
                    recommendation="Replace with a Kubernetes Secret and use secretKeyRef.",
                    fix_command=(
                        f"kubectl create secret generic app-secrets "
                        f"-n {item['namespace']} "
                        f"--from-literal={item['env_var']}=<value>"
                    ),
                ))

        # ── RBAC issues ────────────────────────────────────────────────
        for item in raw.get("rbac", []):
            is_fp, reason = self.is_false_positive(
                "rbac", item["binding"], item["namespace"], item["subject"]
            )
            if is_fp:
                _add(SecurityFinding(
                    id="", severity="info", category="system_component",
                    title=f"Expected RBAC binding: {item['binding']}",
                    description=reason,
                    affected_resource=item["binding"],
                    namespace=item["namespace"],
                    evidence=f"Binding: {item['binding']} | Subject: {item['subject']} | Role: {item['role']}",
                    recommendation="No action required — K8s system binding.",
                    fix_command=None,
                ))
            else:
                sev = (
                    "critical"
                    if "cluster-admin" in item["issue"] or "anonymous" in item["issue"]
                    else "high"
                )
                _add(SecurityFinding(
                    id="", severity=sev, category="rbac",
                    title=f"RBAC: {item['issue'][:60]}",
                    description=item["issue"],
                    affected_resource=item["binding"],
                    namespace=item["namespace"],
                    evidence=(
                        f"Binding: {item['binding']} | "
                        f"Subject: {item['subject']} | Role: {item['role']}"
                    ),
                    recommendation="Revoke excessive permissions. Apply principle of least privilege.",
                    fix_command=(
                        f"kubectl delete clusterrolebinding {item['binding']}"
                        if item["kind"] == "ClusterRoleBinding" else None
                    ),
                ))

        # ── Network policy gaps ────────────────────────────────────────
        for item in raw.get("network", []):
            ns = item["namespace"]
            if ns in SYSTEM_NAMESPACES:
                _add(SecurityFinding(
                    id="", severity="info", category="system_component",
                    title=f"Expected: no NetworkPolicy in {ns}",
                    description=f"System namespace '{ns}' — NetworkPolicy not required.",
                    affected_resource=ns,
                    namespace=ns,
                    evidence=f"kubectl get networkpolicies -n {ns} → (none)",
                    recommendation="No action required — system namespace.",
                    fix_command=None,
                ))
            else:
                sev = "high" if item["risk"] == "high" else "medium"
                _add(SecurityFinding(
                    id="", severity=sev, category="network",
                    title=f"No NetworkPolicy in namespace: {ns}",
                    description=item["issue"],
                    affected_resource=ns,
                    namespace=ns,
                    evidence=f"kubectl get networkpolicies -n {ns} → (none)",
                    recommendation=(
                        "Add a default-deny NetworkPolicy then explicitly allow "
                        "needed traffic. Run --fix to auto-apply."
                    ),
                    fix_command=None,
                ))

        # ── Pods running as root ───────────────────────────────────────
        for item in raw.get("root", []):
            is_fp, reason = self.is_false_positive(
                "root", item["container"], item["namespace"], item["container"]
            )
            if is_fp:
                _add(SecurityFinding(
                    id="", severity="info", category="system_component",
                    title=f"Expected: {item['container']} runs as root (system)",
                    description=reason,
                    affected_resource=item["pod"],
                    namespace=item["namespace"],
                    evidence=f"Container '{item['container']}' in pod '{item['pod']}'",
                    recommendation="No action required — AWS/K8s managed component.",
                    fix_command=None,
                ))
            else:
                sev = "critical" if item.get("explicit") else "low"
                _add(SecurityFinding(
                    id="", severity=sev, category="runtime",
                    title=f"Container runs as root: {item['container']}",
                    description=item["issue"],
                    affected_resource=item["pod"],
                    namespace=item["namespace"],
                    evidence=f"Container '{item['container']}' in pod '{item['pod']}'",
                    recommendation=(
                        "Set runAsNonRoot: true and runAsUser: 1000 in securityContext."
                    ),
                    fix_command=None,
                ))

        # ── Exposed services ──────────────────────────────────────────
        for item in raw.get("exposure", []):
            evidence = (
                f"type={item['type']} ports={item['ports']} "
                f"external_ip={item['external_ip'] or 'pending'}"
            )
            if item.get("is_ingress"):
                _add(SecurityFinding(
                    id="", severity="info", category="system_component",
                    title=f"Expected: ingress controller LoadBalancer — {item['service']}",
                    description=(
                        f"Service '{item['service']}' ({item['ingress_type']}) is a "
                        f"LoadBalancer ingress controller. This is expected and required "
                        f"for public HTTP/S traffic routing."
                    ),
                    affected_resource=item["service"],
                    namespace=item["namespace"],
                    evidence=evidence,
                    recommendation=(
                        "Ensure only ports 80/443 are exposed. "
                        "Add an AWS Security Group to restrict source IPs. "
                        "Do NOT change to ClusterIP — this will break all ingress routing."
                    ),
                    fix_command=None,
                ))
            else:
                sev = "high" if item["risk"] == "high" else "medium"
                _add(SecurityFinding(
                    id="", severity=sev, category="exposure",
                    title=f"Unexpected {item['type']} service: {item['service']}",
                    description=(
                        f"Service '{item['service']}' ({item['type']}) exposes "
                        f"ports {item['ports']} directly to the network and is not "
                        f"a known ingress controller. Investigate if intentional."
                    ),
                    affected_resource=item["service"],
                    namespace=item["namespace"],
                    evidence=evidence,
                    recommendation=(
                        "Use ClusterIP + Ingress controller for HTTP/S services. "
                        "If a LoadBalancer is required, restrict source IPs via "
                        "AWS Security Group rules."
                    ),
                    fix_command=(
                        f"kubectl patch svc {item['service']} "
                        f"-n {item['namespace']} "
                        f"-p '{{\"spec\":{{\"type\":\"ClusterIP\"}}}}'"
                    ),
                ))

            # Port audit — flag non-standard ports on any exposed service
            nsp = item.get("non_standard_ports", [])
            if nsp:
                port_list = ", ".join(str(p) for p in nsp)
                _add(SecurityFinding(
                    id="", severity="high", category="exposure",
                    title=f"Non-standard ports exposed: {item['service']} ({port_list})",
                    description=(
                        f"Service '{item['service']}' exposes non-standard port(s): {port_list}. "
                        f"Standard safe ports are 80, 443, 8080. "
                        f"Non-standard ports may expose internal services to the public internet."
                    ),
                    affected_resource=item["service"],
                    namespace=item["namespace"],
                    evidence=f"all ports: {item['ports']} — non-standard: {port_list}",
                    recommendation=(
                        f"Audit why port(s) {port_list} are publicly exposed. "
                        f"Restrict via AWS Security Group or move service to ClusterIP."
                    ),
                    fix_command=None,
                ))

        # ── Score, analysis, report ───────────────────────────────────
        real_findings = [
            f for f in findings
            if f.category != "system_component" and f.severity != "info"
        ]
        score   = self.calculate_score(findings)
        prod_ok = score > 80
        summary = (
            self.analyze_with_claude(real_findings)
            if real_findings else
            "No actionable security issues found. System components are operating as expected."
        )

        cluster = get_current_context() or "unknown"

        report = SecurityReport(
            cluster_name     = cluster,
            scan_time        = datetime.now(timezone.utc).isoformat(),
            total_findings   = len(findings),
            critical         = counters["critical"],
            high             = counters["high"],
            medium           = counters["medium"],
            low              = counters["low"],
            info             = counters["info"],
            system_components = counters["system_component"],
            findings         = findings,
            security_score   = score,
            production_ready = prod_ok,
            claude_summary   = summary,
        )

        remember(
            content=(
                f"Security audit on {cluster}: score={score}/100 "
                f"critical={counters['critical']} high={counters['high']} "
                f"medium={counters['medium']} low={counters['low']} "
                f"system_components={counters['system_component']}. "
                f"{summary[:300]}"
            ),
            source="security-audit",
            metadata={
                "cluster":           cluster,
                "score":             score,
                "critical":          counters["critical"],
                "high":              counters["high"],
                "production_ready":  prod_ok,
                "system_components": counters["system_component"],
            },
        )

        log.info("security.audit_done", cluster=cluster, score=score,
                 total=len(findings), system_components=counters["system_component"],
                 **{k: v for k, v in counters.items() if k != "system_component"})
        return report

    # ------------------------------------------------------------------
    # Intelligence
    # ------------------------------------------------------------------

    @staticmethod
    def is_false_positive(
        finding_type: str,
        container_name: str,
        namespace: str,
        resource_name: str,
    ) -> tuple[bool, str]:
        """Return (is_false_positive, reason).

        finding_type: 'privileged' | 'root' | 'secret_env' | 'rbac'
        container_name: container name (for privileged/root checks)
        namespace: pod/resource namespace
        resource_name: env var name, RBAC subject, or container name
        """
        if finding_type in ("privileged", "root"):
            name_lower = container_name.lower()
            for component, reason in PRIVILEGED_SYSTEM_COMPONENTS.items():
                if component in name_lower:
                    return True, f"System component: {reason}"
            if namespace in SYSTEM_NAMESPACES:
                return True, f"AWS/K8s managed namespace: {namespace}"

        if finding_type == "secret_env":
            if resource_name in FALSE_POSITIVE_ENV_VARS:
                return True, (
                    f"Not a secret — '{resource_name}' is an AWS IRSA env var "
                    f"(file path or ARN, not a credential)."
                )

        if finding_type == "rbac":
            binding = container_name   # binding name passed as container_name
            subject = resource_name
            # Skip K8s system bindings (not user-created)
            if any(binding.startswith(p) for p in _SYSTEM_BINDING_PREFIXES):
                return True, f"K8s system binding '{binding}' — expected, not user-created."
            if subject.startswith("system:") and "anonymous" not in subject:
                return True, (
                    f"K8s system account '{subject}' — expected binding for system component."
                )

        return False, ""

    # ------------------------------------------------------------------
    # Scoring — only actionable findings count
    # ------------------------------------------------------------------

    def calculate_score(self, findings: list[SecurityFinding]) -> int:
        score = 100
        for f in findings:
            if f.category == "system_component":
                continue    # never penalise system components
            if f.severity == "info":
                continue    # info is informational only
            score -= _SEV_DEDUCTIONS.get(f.severity, 0)
        return max(score, 0)

    # ------------------------------------------------------------------
    # Fix routing
    # ------------------------------------------------------------------

    @staticmethod
    def is_fixable(f: SecurityFinding) -> bool:
        """Return True if this finding can be automatically remediated."""
        if f.category in ("system_component",):
            return False   # never patch system components
        if f.severity == "info":
            return False
        if f.category == "privilege":
            return f.namespace not in SYSTEM_NAMESPACES
        if f.category == "network":
            return True
        return bool(f.fix_command) and "<value>" not in (f.fix_command or "")

    def apply_security_fix(self, finding: SecurityFinding) -> bool:
        """Apply the correct fix for a finding, route by category."""
        if finding.category in ("system_component",):
            return False
        if finding.category == "network":
            return self._apply_network_policy(finding)
        if finding.category == "privilege":
            return self._apply_privilege_fix(finding)
        if not finding.fix_command or "<value>" in finding.fix_command:
            log.info("security.fix.skipped", id=finding.id,
                     reason="no fix command or requires manual input")
            return False
        result = apply_shell_fix(finding.fix_command)
        ok     = result == "ok"
        self._remember_fix(finding, ok)
        return ok

    def _apply_privilege_fix(self, finding: SecurityFinding) -> bool:
        """Patch the owning Deployment/DaemonSet/StatefulSet to remove privileged."""
        pod       = finding.affected_resource
        ns        = finding.namespace
        container = finding.title.split(": ", 1)[-1] if ": " in finding.title else ""

        if ns in SYSTEM_NAMESPACES:
            log.info("security.fix.skipped", pod=pod, ns=ns,
                     reason="system namespace — do not patch")
            self._remember_fix(finding, False,
                               extra={"reason": f"{ns} — system namespace, do not patch"})
            return False

        kind, owner = get_deployment_for_pod(pod, ns)
        if not owner or kind not in ("Deployment", "DaemonSet", "StatefulSet"):
            log.warning("security.fix.no_owner", pod=pod, ns=ns, kind=kind)
            self._remember_fix(finding, False,
                               extra={"reason": f"owner not found (kind={kind})"})
            return False

        patch_json = json.dumps({
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "name": container,
                            "securityContext": {
                                "privileged": False,
                                "allowPrivilegeEscalation": False,
                            },
                        }]
                    }
                }
            }
        })
        r  = run_kubectl(["patch", kind.lower(), owner, "-n", ns,
                          "--type=strategic", "-p", patch_json])
        ok = r.success
        if not ok:
            log.warning("security.fix.patch_failed", owner=owner, ns=ns, error=r.error)
        self._remember_fix(finding, ok, extra={"owner": owner, "kind": kind})
        return ok

    def _apply_network_policy(self, finding: SecurityFinding) -> bool:
        """Write a default-deny NetworkPolicy YAML to tempfile and kubectl apply."""
        ns = finding.namespace
        yaml_content = (
            f"apiVersion: networking.k8s.io/v1\n"
            f"kind: NetworkPolicy\n"
            f"metadata:\n"
            f"  name: default-deny-all\n"
            f"  namespace: {ns}\n"
            f"spec:\n"
            f"  podSelector: {{}}\n"
            f"  policyTypes:\n"
            f"  - Ingress\n"
            f"  - Egress\n"
        )
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as fh:
                fh.write(yaml_content)
                tmp_path = fh.name
            r  = run_kubectl(["apply", "-f", tmp_path])
            ok = r.success
            if not ok:
                log.warning("security.fix.netpol_failed", ns=ns, error=r.error)
        except Exception as exc:
            log.warning("security.fix.netpol_exception", ns=ns, error=str(exc))
            ok = False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        self._remember_fix(finding, ok)
        return ok

    def _remember_fix(
        self,
        finding: SecurityFinding,
        success: bool,
        extra: dict | None = None,
    ) -> None:
        cluster = get_current_context() or "unknown"
        meta: dict = {
            "finding_id": finding.id,
            "category":   finding.category,
            "severity":   finding.severity,
            "namespace":  finding.namespace,
            "resource":   finding.affected_resource,
            "success":    success,
            "cluster":    cluster,
        }
        if extra:
            meta.update(extra)
        status = "applied" if success else "attempted (failed)"
        remember(
            content=(
                f"Security fix {status}: [{finding.severity.upper()}] {finding.title} "
                f"in {finding.namespace}/{finding.affected_resource} on cluster {cluster}."
            ),
            source="security-fix",
            metadata=meta,
        )
        log.info("security.fix_recorded", id=finding.id, success=success, cluster=cluster)

    # ------------------------------------------------------------------
    # AI analysis — only real findings, SRE context
    # ------------------------------------------------------------------

    def analyze_with_claude(self, real_findings: list[SecurityFinding]) -> str:
        """Analyse only actionable findings — system components are pre-filtered."""
        target = [f for f in real_findings if f.severity in ("critical", "high")][:8]
        if not target:
            target = real_findings[:5]
        if not target:
            return "No actionable findings to analyse."

        lines = ["=== ACTIONABLE SECURITY FINDINGS ==="]
        for f in target:
            lines.append(
                f"[{f.severity.upper()}] {f.title}\n"
                f"  Resource: {f.namespace}/{f.affected_resource}\n"
                f"  Evidence: {f.evidence}"
            )

        try:
            resp = run_sync(llm.chat(
                messages=[context.user_message("\n".join(lines))],
                system=_ANALYZE_SYSTEM,
                max_tokens=700,
            ))
            return resp.content.strip()
        except Exception as exc:
            log.warning("security.claude_failed", error=str(exc))
            return f"AI analysis unavailable: {exc}"
