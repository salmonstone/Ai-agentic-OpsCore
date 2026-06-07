"""
IngressSkill — diagnose and fix every nginx ingress / external-IP problem.

Problem coverage:
  ExternalIPPending      LoadBalancer svc stuck with no external IP
  ControllerNotRunning   nginx pods are crashing or not ready
  ControllerNotInstalled no nginx controller at all
  NoAddress              Ingress resource has no address assigned
  BackendDown            Backend service has no ready endpoints
  ServiceNotFound        Backend service referenced doesn't exist
  TLSSecretMissing       TLS configured but secret absent
  CertManagerError       cert-manager not running / Certificate not ready
  WrongIngressClass      ingressClassName doesn't match any controller
  NoIngressClass         ingressClassName / annotation missing
  ConfigMapError         nginx ConfigMap has bad config
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

from agent.core import context, llm
from agent.core.models import (
    IngressDiagnosis,
    IngressIssue,
    IngressProblemType,
    IngressScanReport,
)
from agent.integrations.ingress_collector import collect_all_ingress
from agent.integrations.kubectl import run_kubectl
from agent.memory.retrieval import remember, retrieve_context
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SCAN_SYSTEM = """\
You are a senior Kubernetes SRE specializing in nginx ingress and external networking.
You receive a structured snapshot of the cluster's ingress setup.
Identify every real issue. Do not invent issues not present in the data.

Return JSON only:
{
  "issues": [
    {
      "severity": "critical" | "warning" | "info",
      "problem_type": one of exactly:
        "ExternalIPPending" | "ControllerNotRunning" | "ControllerNotInstalled" |
        "NoAddress" | "BackendDown" | "ServiceNotFound" | "TLSSecretMissing" |
        "CertManagerError" | "WrongIngressClass" | "NoIngressClass" |
        "ConfigMapError" | "Healthy" | "Unknown",
      "resource": "<svc name, ingress name, or pod name>",
      "namespace": "<namespace or empty string>",
      "description": "one-sentence description of the specific problem",
      "fix": "what to do to resolve this",
      "fix_command": "exact kubectl command, or null",
      "deep_dive": true | false
    }
  ],
  "analysis": "2-3 sentence executive summary",
  "controller_ok": true | false,
  "external_ip": "the external IP or hostname if found, or null"
}

ExternalIPPending fix guidance:
  - On bare-metal/local: suggest MetalLB or changing service type to NodePort
  - On cloud (EKS/GKE/AKS): suggest checking cloud LB quota or IAM permissions
  - Quick NodePort workaround: kubectl patch svc <svc> -n <ns> -p '{"spec":{"type":"NodePort"}}'

NoAddress / ControllerNotRunning fix guidance:
  - Restart: kubectl rollout restart deployment/ingress-nginx-controller -n ingress-nginx
  - Check: kubectl describe pod <controller-pod> -n ingress-nginx

BackendDown / ServiceNotFound fix guidance:
  - Check service selector matches pod labels
  - Check pod readiness and logs

deep_dive: true only when pod logs or full YAML are needed to resolve"""

_DIAGNOSE_SYSTEM = """\
You are a Kubernetes SRE diagnosing a specific Ingress resource.
You have full kubectl describe, events, nginx controller logs, and the ingress YAML.
Return JSON:
{
  "problem_type": "<IngressProblemType value>",
  "root_cause": "precise technical root cause",
  "suggested_fix": "what to do",
  "fix_command": "exact kubectl command or null",
  "confidence": "high" | "medium" | "low",
  "explanation": "detailed technical explanation"
}"""


def _parse_json(content: str) -> dict:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text).strip()
    return json.loads(text)


def _to_problem_type(raw: str) -> IngressProblemType:
    try:
        return IngressProblemType(raw)
    except ValueError:
        return IngressProblemType.UNKNOWN


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class IngressSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "ingress-diagnose"

    @property
    def description(self) -> str:
        return "Diagnose and fix nginx ingress, external IP, and routing problems."

    def execute(self, input_data: dict) -> dict:
        mode = input_data.get("mode", "scan")
        if mode == "scan":
            return self.scan().model_dump()
        if mode == "diagnose":
            d = self.diagnose(input_data["name"], input_data["namespace"])
            return d.model_dump()
        if mode == "fix-ip":
            return self.fix_external_ip()
        raise ValueError(f"Unknown mode: {mode!r}")

    # ------------------------------------------------------------------
    # scan — all ingress areas, one Claude call
    # ------------------------------------------------------------------

    def scan(self) -> IngressScanReport:
        log.info("ingress.scan.start")

        collection, elapsed_ms = collect_all_ingress()

        report = IngressScanReport(
            generated_at  = datetime.now(timezone.utc).isoformat(),
            collection_ms = elapsed_ms,
        )

        ok_areas = [a for a, r in collection.items() if r.get("ok")]
        if not ok_areas:
            report.analysis = "All collectors failed — check cluster connectivity."
            return report

        # Build one prompt from all collector summaries
        sections = []
        for area, res in collection.items():
            sections.append(f"=== {area.upper()} ===\n{res.get('summary', '(no data)')}")
            if res.get("ok") and res.get("data"):
                try:
                    sections.append(json.dumps(res["data"], indent=2)[:800])
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

            report.issues       = [IngressIssue(**i) for i in parsed.get("issues", [])]
            report.analysis     = parsed.get("analysis", "")
            report.controller_ok = parsed.get("controller_ok", False)
            report.external_ip  = parsed.get("external_ip")

            log.info("ingress.scan.done", issues=len(report.issues))
        except Exception as exc:
            log.error("ingress.scan.error", error=str(exc))
            report.analysis = f"Analysis failed: {exc}"
            return report

        # Deep-dive on issues that need it
        for issue in report.issues:
            if issue.deep_dive and issue.severity == "critical":
                enriched = self._deep_dive_issue(issue, collection)
                if enriched:
                    issue.fix_command = enriched.get("fix_command") or issue.fix_command
                    issue.fix         = enriched.get("explanation") or issue.fix

        # Save to memory
        critical = sum(1 for i in report.issues if i.severity == "critical")
        warning  = sum(1 for i in report.issues if i.severity == "warning")
        remember(
            content=(
                f"Ingress scan: {critical} critical, {warning} warnings. "
                f"controller_ok={report.controller_ok}. "
                f"external_ip={report.external_ip}. {report.analysis}"
            ),
            source="ingress-scan",
            metadata={"critical": critical, "warning": warning,
                      "controller_ok": report.controller_ok,
                      "external_ip": report.external_ip},
        )

        return report

    # ------------------------------------------------------------------
    # diagnose — deep AI analysis of one Ingress resource
    # ------------------------------------------------------------------

    def diagnose(self, name: str, namespace: str) -> IngressDiagnosis:
        log.info("ingress.diagnose.start", name=name, namespace=namespace)

        # Collect all relevant data for this ingress
        desc    = run_kubectl(["describe", "ingress", name, "-n", namespace])
        yaml_r  = run_kubectl(["get", "ingress", name, "-n", namespace, "-o", "yaml"])
        events  = run_kubectl(["get", "events", "-n", namespace,
                               "--field-selector", f"involvedObject.name={name}"])

        # nginx controller pod logs
        ctrl_logs = self._get_controller_logs()

        # LB service status
        lb_svc   = self._get_nginx_lb_service()

        memories = retrieve_context(f"ingress {name} {namespace}")

        user_text = (
            f"Ingress: {name}\nNamespace: {namespace}\n\n"
            f"--- DESCRIBE ---\n{desc.output[:2000] if desc.success else desc.error[:200]}\n\n"
            f"--- YAML ---\n{yaml_r.output[:1500] if yaml_r.success else '(not available)'}\n\n"
            f"--- EVENTS ---\n{events.output[:800] if events.success else '(none)'}\n\n"
            f"--- NGINX CONTROLLER LOGS (last 50 lines) ---\n{ctrl_logs[:1500]}\n\n"
            f"--- LOAD BALANCER SERVICE ---\n{lb_svc}"
        )

        system_prompt = context.build_system_prompt(_DIAGNOSE_SYSTEM, memories)

        response = asyncio.run(llm.chat(
            messages=[context.user_message(user_text)],
            system=system_prompt,
            json_mode=True,
            max_tokens=1500,
        ))
        parsed = _parse_json(response.content)

        diagnosis = IngressDiagnosis(
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
                f"Ingress {name}/{namespace} diagnosed as "
                f"{diagnosis.problem_type.value}: {diagnosis.root_cause}. "
                f"Fix: {diagnosis.suggested_fix}"
            ),
            source="ingress-diagnose",
            metadata={
                "name": name, "namespace": namespace,
                "problem_type": diagnosis.problem_type.value,
                "confidence": diagnosis.confidence,
            },
        )

        log.info("ingress.diagnose.done", name=name,
                 problem_type=diagnosis.problem_type.value,
                 confidence=diagnosis.confidence)
        return diagnosis

    # ------------------------------------------------------------------
    # fix_external_ip — targeted fix for ExternalIPPending
    # ------------------------------------------------------------------

    def fix_external_ip(self) -> dict:
        """
        Targeted diagnosis + fix for the most common problem:
        nginx-ingress LoadBalancer service stuck at <pending>.
        """
        log.info("ingress.fix_external_ip.start")

        # 1. Find the nginx LB service
        r = run_kubectl(["get", "services", "-A", "-o", "json"])
        if not r.success:
            return {"ok": False, "error": "Cannot list services"}

        nginx_svc = None
        for svc in json.loads(r.output).get("items", []):
            name = svc["metadata"]["name"]
            stype = svc.get("spec", {}).get("type", "")
            if stype == "LoadBalancer" and "ingress" in name.lower():
                lb    = svc.get("status", {}).get("loadBalancer", {}).get("ingress", [])
                nginx_svc = svc
                if not lb:
                    break  # take the pending one

        if not nginx_svc:
            return {"ok": False, "error": "No nginx LoadBalancer service found"}

        svc_name = nginx_svc["metadata"]["name"]
        svc_ns   = nginx_svc["metadata"]["namespace"]
        lb       = nginx_svc.get("status", {}).get("loadBalancer", {}).get("ingress", [])
        addr     = lb[0].get("hostname", lb[0].get("ip", "")) if lb else ""

        if addr:
            return {
                "ok": True,
                "already_assigned": True,
                "external_ip": addr,
                "message": f"External IP already assigned: {addr}",
            }

        # 2. Check if we're on a cloud provider (EKS/GKE/AKS) or bare-metal
        node_r = run_kubectl(["get", "nodes", "-o", "json"])
        is_cloud = False
        if node_r.success:
            nodes = json.loads(node_r.output).get("items", [])
            for node in nodes:
                labels = node.get("metadata", {}).get("labels", {})
                if any(k in labels for k in ("eks.amazonaws.com/nodegroup",
                                              "cloud.google.com/gke-nodepool",
                                              "kubernetes.azure.com/cluster")):
                    is_cloud = True
                    break

        # 3. Collect describe for AI analysis
        desc = run_kubectl(["describe", "service", svc_name, "-n", svc_ns])
        events = run_kubectl(["get", "events", "-n", svc_ns,
                              "--field-selector", f"involvedObject.name={svc_name}"])

        context_text = (
            f"Service: {svc_name} in {svc_ns}\n"
            f"Type: LoadBalancer — external IP is PENDING\n"
            f"Cloud environment: {is_cloud}\n\n"
            f"--- DESCRIBE ---\n{desc.output[:1500] if desc.success else desc.error}\n\n"
            f"--- EVENTS ---\n{events.output[:800] if events.success else '(none)'}"
        )

        fix_system = """\
You are a Kubernetes networking expert. The nginx ingress LoadBalancer service is stuck
with no external IP (pending). Diagnose and give an exact fix.
Return JSON:
{
  "root_cause": "why external IP is pending",
  "fix": "what to do",
  "fix_command": "exact kubectl command, or null",
  "workaround_command": "NodePort patch as fallback if cloud fix is not possible",
  "confidence": "high|medium|low",
  "explanation": "technical detail"
}
For bare-metal or local clusters (no cloud): recommend NodePort patch or MetalLB.
For cloud clusters (EKS/GKE/AKS): check IAM/permissions, cloud LB limits, subnet tags."""

        response = asyncio.run(llm.chat(
            messages=[context.user_message(context_text)],
            system=fix_system,
            json_mode=True,
            max_tokens=800,
        ))
        result = _parse_json(response.content)

        remember(
            content=(
                f"ExternalIPPending on {svc_name}/{svc_ns}. "
                f"Root cause: {result.get('root_cause','')}. "
                f"Fix: {result.get('fix','')}"
            ),
            source="ingress-fix-ip",
            metadata={"service": svc_name, "namespace": svc_ns,
                      "is_cloud": is_cloud},
        )

        return {
            "ok": True,
            "service": svc_name,
            "namespace": svc_ns,
            "is_cloud": is_cloud,
            "root_cause": result.get("root_cause", ""),
            "fix": result.get("fix", ""),
            "fix_command": result.get("fix_command"),
            "workaround_command": result.get("workaround_command"),
            "confidence": result.get("confidence", "medium"),
            "explanation": result.get("explanation", ""),
        }

    # ------------------------------------------------------------------
    # apply_fix
    # ------------------------------------------------------------------

    def apply_fix(self, fix_command: str, resource: str, namespace: str) -> bool:
        log.info("ingress.apply_fix", command=fix_command)
        result = run_kubectl(fix_command.replace("kubectl ", "").split())
        remember(
            content=f"Applied ingress fix for {resource}/{namespace}: `{fix_command}` — "
                    f"{'succeeded' if result.success else 'failed'}",
            source="ingress-fix",
            metadata={"resource": resource, "namespace": namespace,
                      "command": fix_command, "success": result.success},
        )
        return result.success

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_controller_logs(self) -> str:
        """Get logs from the nginx ingress controller pod."""
        NAMESPACES = ["ingress-nginx", "kube-system", "default"]
        SELECTORS  = [
            "app.kubernetes.io/name=ingress-nginx",
            "app=ingress-nginx",
            "app=nginx-ingress",
        ]
        for ns in NAMESPACES:
            for sel in SELECTORS:
                pods_r = run_kubectl(["get", "pods", "-n", ns, "-l", sel,
                                      "--no-headers", "-o",
                                      "custom-columns=NAME:.metadata.name"])
                if pods_r.success and pods_r.output.strip():
                    pod_name = pods_r.output.strip().splitlines()[0].strip()
                    logs_r   = run_kubectl(["logs", pod_name, "-n", ns,
                                            "--tail=50", "--timestamps=false"])
                    if logs_r.success:
                        return logs_r.output
        return "(nginx controller logs not available)"

    def _get_nginx_lb_service(self) -> str:
        """Get the nginx LoadBalancer service status as text."""
        r = run_kubectl(["get", "services", "-A", "-o", "wide"])
        if not r.success:
            return "(cannot list services)"
        lines = [l for l in r.output.splitlines()
                 if "ingress" in l.lower() or "nginx" in l.lower() or "EXTERNAL" in l]
        return "\n".join(lines[:10]) or "(no matching services)"

    def _deep_dive_issue(self, issue: IngressIssue,
                         collection: dict[str, dict]) -> dict | None:
        """Deep-dive on a critical issue using logs and resource YAML."""
        lines = [
            f"Issue:     {issue.problem_type.value}",
            f"Resource:  {issue.resource}",
            f"Namespace: {issue.namespace}",
            f"Problem:   {issue.description}",
            "",
        ]

        # Add controller logs
        ctrl_data = collection.get("nginx_controller", {}).get("data", {})
        if ctrl_data.get("pods"):
            lines.append("=== CONTROLLER POD STATUS ===")
            for pod in ctrl_data["pods"]:
                lines.append(str(pod))

        # Add LB service info
        lb_data = collection.get("lb_services", {}).get("data", {})
        if lb_data.get("services"):
            lines.append("\n=== LB SERVICES ===")
            for svc in lb_data["services"]:
                if svc.get("is_nginx"):
                    lines.append(str(svc))

        if issue.namespace and issue.resource:
            ctrl_logs = self._get_controller_logs()
            desc_r = run_kubectl(["describe", "ingress", issue.resource,
                                  "-n", issue.namespace])
            lines.append(f"\n=== DESCRIBE ===\n{desc_r.output[:1500] if desc_r.success else '(not available)'}")
            lines.append(f"\n=== CONTROLLER LOGS ===\n{ctrl_logs[:1000]}")

        deep_system = """\
You are a Kubernetes ingress SRE doing a deep-dive on a critical issue.
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
            log.warning("ingress.deep_dive.error", error=str(exc))
            return None
