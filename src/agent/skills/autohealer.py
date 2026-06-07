"""
Auto-remediation skill for Kubernetes.

Two-phase fix strategy:
  Phase 1 — try Claude's suggested fix_command via shlex-safe apply_fix
  Phase 2 — fetch full deployment YAML → ask Claude for a structured patch
             → apply via apply_patch() (passes JSON directly, no shell splitting)
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from agent.core import context, llm
from agent.core.models import PodDiagnosis, PodInfo
from agent.integrations.kubectl import (
    apply_fix as kubectl_apply_fix,
    apply_patch as kubectl_apply_patch,
    get_deployment_for_pod,
    get_pod_events,
    get_pod_logs,
    get_problematic_pods,
    get_resource_yaml,
    run_kubectl,
)
from agent.memory.retrieval import remember
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill
from agent.skills.k8s import K8sSkill

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Deep-fix system prompt — returns structured patch, NOT a shell string
# ---------------------------------------------------------------------------

_DEEP_FIX_SYSTEM = """You are a Kubernetes SRE performing automated remediation.
A pod is crashing. You have its logs, events, and the full deployment YAML.

Return JSON only — this exact structure:
{
  "resource_kind": "Deployment",
  "resource_name": "<name of the deployment/statefulset/daemonset>",
  "patch_type": "json",
  "patch": [ <valid JSON Patch array — RFC 6902> ],
  "fallback_command": "<optional single kubectl command if patch won't work, else null>",
  "explanation": "one sentence — what was wrong and what this fixes"
}

Rules:
- patch_type must be exactly: "json" | "merge" | "strategic"
- patch for type "json" must be a valid RFC 6902 array of operations
- patch for type "merge" must be a partial deployment object dict
- Common fixes:

  Crash command (exit 1, false, /bin/false):
    patch_type: "json"
    patch: [{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["sleep","infinity"]}]

  Wrong image tag:
    patch_type: "json"
    patch: [{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"nginx:latest"}]

  Add missing env var:
    patch_type: "json"
    patch: [{"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"KEY","value":"val"}}]

  Increase memory limit:
    patch_type: "merge"
    patch: {"spec":{"template":{"spec":{"containers":[{"name":"<container>","resources":{"limits":{"memory":"512Mi"}}}]}}}}

  Remove bad args:
    patch_type: "json"
    patch: [{"op":"remove","path":"/spec/template/spec/containers/0/args"}]

- Always set patch so the pod will actually stay Running after the fix
- If a patch truly cannot fix the problem, set patch to null and use fallback_command instead"""


def _parse_json(content: str) -> dict:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text).strip()
    return json.loads(text)


def _recheck_pod(pod_name: str, namespace: str, wait_secs: int = 15) -> str:
    """Wait, then return the new pod status (Running / CrashLoopBackOff / etc.)."""
    time.sleep(wait_secs)

    # Try label-based lookup first
    prefix = pod_name.rsplit("-", 2)[0]
    result = run_kubectl([
        "get", "pods", "-n", namespace, "--no-headers",
        "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.phase,READY:.status.containerStatuses[0].ready",
    ])
    if result.success:
        for line in result.output.splitlines():
            parts = line.split()
            if parts and parts[0].startswith(prefix):
                return parts[1] if len(parts) > 1 else "Unknown"

    return "Unknown"


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class AutoHealerSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "auto-healer"

    @property
    def description(self) -> str:
        return "Watch the cluster and automatically fix crashing pods using Claude."

    def execute(self, input_data: dict) -> dict:
        namespace = input_data.get("namespace", "all")
        results   = self.heal_all(namespace)
        return {"results": results}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def heal_all(self, namespace: str = "all") -> list[dict]:
        k8s  = K8sSkill()
        pods = get_problematic_pods()
        if namespace != "all":
            pods = [p for p in pods if p.namespace == namespace]

        log.info("autohealer.heal_all", namespace=namespace, count=len(pods))
        return [self.heal_pod(pod, k8s) for pod in pods]

    def heal_pod(self, pod: PodInfo, k8s: K8sSkill | None = None) -> dict:
        if k8s is None:
            k8s = K8sSkill()

        log.info("autohealer.heal_pod.start", pod=pod.name, namespace=pod.namespace)
        diagnosis = k8s.diagnose_pod(pod)

        result: dict = {
            "pod":          pod.name,
            "namespace":    pod.namespace,
            "problem_type": diagnosis.problem_type.value,
            "confidence":   diagnosis.confidence,
            "phase":        None,
            "fix_applied":  None,
            "success":      False,
            "new_status":   None,
            "explanation":  None,
        }

        # ── Phase 1: simple fix_command from diagnosis ────────────────────
        if diagnosis.fix_command:
            log.info("autohealer.phase1", pod=pod.name, command=diagnosis.fix_command)
            fix_result = kubectl_apply_fix(diagnosis.fix_command)

            if fix_result.success:
                new_status = _recheck_pod(pod.name, pod.namespace)
                result.update({
                    "phase":       "phase1",
                    "fix_applied": diagnosis.fix_command,
                    "success":     True,
                    "new_status":  new_status,
                    "explanation": diagnosis.suggested_fix,
                })
                self._remember_fix(pod, diagnosis.fix_command, True, "phase1")
                return result

            log.warning("autohealer.phase1.failed", pod=pod.name, error=fix_result.error[:120])

        # ── Phase 2: structured deep patch ───────────────────────────────
        log.info("autohealer.phase2.start", pod=pod.name)
        patch_data = self._get_deep_patch(pod, diagnosis)

        if not patch_data:
            result.update({
                "phase":       "phase2",
                "explanation": "Claude could not determine a fix for this pod.",
            })
            return result

        kind        = patch_data.get("resource_kind", "Deployment")
        name        = patch_data.get("resource_name", "")
        patch_type  = patch_data.get("patch_type", "json")
        patch       = patch_data.get("patch")
        fallback    = patch_data.get("fallback_command")
        explanation = patch_data.get("explanation", "")

        if patch and name:
            log.info("autohealer.phase2.patch", kind=kind, name=name, patch_type=patch_type)
            fix_result = kubectl_apply_patch(kind, name, pod.namespace, patch_type, patch)
            applied    = f"kubectl patch {kind.lower()}/{name} --type={patch_type} -p '{json.dumps(patch)}'"
        elif fallback:
            log.info("autohealer.phase2.fallback", command=fallback)
            fix_result = kubectl_apply_fix(fallback)
            applied    = fallback
        else:
            result.update({"phase": "phase2", "explanation": "No actionable fix returned by Claude."})
            return result

        if fix_result.success:
            new_status = _recheck_pod(pod.name, pod.namespace, wait_secs=20)
            result.update({
                "phase":       "phase2",
                "fix_applied": applied,
                "success":     True,
                "new_status":  new_status,
                "explanation": explanation,
            })
            self._remember_fix(pod, applied, True, "phase2")
        else:
            result.update({
                "phase":       "phase2",
                "fix_applied": applied,
                "success":     False,
                "explanation": fix_result.error[:300],
            })
            self._remember_fix(pod, applied, False, "phase2")

        return result

    # ------------------------------------------------------------------
    # Deep fix: fetch YAML → ask Claude → structured patch
    # ------------------------------------------------------------------

    def _get_deep_patch(self, pod: PodInfo, diagnosis: PodDiagnosis) -> dict | None:
        kind, owner_name = get_deployment_for_pod(pod.name, pod.namespace)
        if not kind or not owner_name:
            log.warning("autohealer.deep_fix.no_owner", pod=pod.name)
            return None

        yaml_text = get_resource_yaml(kind, owner_name, pod.namespace)
        logs      = get_pod_logs(pod.name, pod.namespace)
        events    = get_pod_events(pod.name, pod.namespace)

        user_text = (
            f"Pod        : {pod.name}\n"
            f"Namespace  : {pod.namespace}\n"
            f"Status     : {pod.status}\n"
            f"Restarts   : {pod.restarts}\n"
            f"Problem    : {diagnosis.problem_type.value}\n"
            f"Root cause : {diagnosis.root_cause}\n\n"
            f"--- LOGS ---\n{logs[:1500]}\n\n"
            f"--- EVENTS ---\n{events[:800]}\n\n"
            f"--- {kind.upper()} YAML ({owner_name}) ---\n{yaml_text[:3000]}"
        )

        try:
            response = asyncio.run(
                llm.chat(
                    messages=[context.user_message(user_text)],
                    system=_DEEP_FIX_SYSTEM,
                    json_mode=True,
                    max_tokens=1024,
                )
            )
            parsed = _parse_json(response.content)
            log.info("autohealer.deep_fix.response",
                     kind=parsed.get("resource_kind"),
                     name=parsed.get("resource_name"),
                     patch_type=parsed.get("patch_type"))
            return parsed
        except Exception as exc:
            log.error("autohealer.deep_fix.llm_error", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    def _remember_fix(self, pod: PodInfo, command: str, success: bool, phase: str) -> None:
        remember(
            content=(
                f"AutoHealer {phase} fix for {pod.name} in {pod.namespace}: "
                f"`{command}` — {'succeeded' if success else 'failed'}"
            ),
            source="auto-healer",
            metadata={
                "pod":       pod.name,
                "namespace": pod.namespace,
                "command":   command,
                "success":   success,
                "phase":     phase,
            },
        )
