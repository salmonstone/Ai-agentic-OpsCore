from __future__ import annotations

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import PodDiagnosis, PodInfo, ProblemType
from agent.core.parsing import LLMParseError, parse_llm_json
from agent.integrations.kubectl import (
    apply_fix as kubectl_apply_fix,
    describe_pod,
    get_pod_events,
    get_pod_logs,
    get_problematic_pods,
)
from agent.memory.retrieval import remember, retrieve_context
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SYSTEM = """You are a Kubernetes expert and SRE.
Analyze the pod information and diagnose the root cause.
Return JSON only with this exact structure:
{
  "problem_type": one of exactly these strings: "CrashLoopBackOff" | "OOMKilled" | "Pending" | "ImagePullBackOff" | "CreateContainerConfigError" | "Unknown",
  "root_cause": "string — clear one-paragraph explanation of the root cause",
  "suggested_fix": "string — what to do to resolve this",
  "fix_command": "string — exact kubectl command to run, or null if no single command applies",
  "confidence": "high" | "medium" | "low",
  "explanation": "string — detailed technical explanation"
}

Rules for problem_type:
- "CrashLoopBackOff": container crashes repeatedly after starting (exit code non-zero, crash logs present)
- "OOMKilled": container killed by kernel OOM killer (exit code 137, memory limit hit)
- "Pending": pod cannot be scheduled (insufficient resources, node selector mismatch, taints)
- "ImagePullBackOff": container image cannot be pulled (wrong tag, registry auth failure)
- "CreateContainerConfigError": container cannot start due to missing Secret or ConfigMap
- "Unknown": pod is healthy, problem is unclear, or does not fit any category above

Rules for confidence:
- "high": root cause is unambiguous from the evidence
- "medium": likely root cause but evidence is incomplete
- "low": pod appears healthy or evidence is too sparse to diagnose

If the pod appears healthy (Running, ready, no restarts, clean logs), set problem_type to "Unknown",
confidence to "low", and fix_command to null."""


def _to_problem_type(raw: str) -> ProblemType:
    try:
        return ProblemType(raw)
    except ValueError:
        return ProblemType.UNKNOWN


# Statuses where the problem type is unambiguous from kubectl alone — confidence = high
_HIGH_CONFIDENCE_STATUSES = {"OOMKilled", "ImagePullBackOff", "ErrImagePull"}


def _status_to_problem_type(status: str) -> ProblemType:
    mapping = {
        "CrashLoopBackOff":          ProblemType.CRASH_LOOP,
        "OOMKilled":                  ProblemType.OOM_KILLED,
        "Pending":                    ProblemType.PENDING,
        "ImagePullBackOff":           ProblemType.IMAGE_PULL,
        "ErrImagePull":               ProblemType.IMAGE_PULL,
        "CreateContainerConfigError": ProblemType.CONFIG_ERROR,
        "CreateContainerError":       ProblemType.CONFIG_ERROR,
    }
    return mapping.get(status, ProblemType.UNKNOWN)


class K8sSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "k8s-diagnose"

    @property
    def description(self) -> str:
        return "Scan a Kubernetes cluster for unhealthy pods and diagnose root causes using Claude."

    def execute(self, input_data: dict) -> dict:
        mode = input_data.get("mode", "scan")
        if mode == "scan":
            namespace = input_data.get("namespace", "all")
            diagnoses = self.scan_cluster(namespace)
            return {"diagnoses": [d.model_dump() for d in diagnoses]}
        if mode == "diagnose":
            pod = PodInfo(**input_data["pod"])
            diagnosis = self.diagnose_pod(pod)
            return diagnosis.model_dump()
        if mode == "fix":
            diagnosis = PodDiagnosis(**input_data["diagnosis"])
            success = self.apply_fix(diagnosis)
            return {"success": success}
        raise ValueError(f"Unknown mode: {mode!r}")

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def scan_cluster(self, namespace: str = "all") -> list[PodDiagnosis]:
        """
        Fast scan — kubectl only, no Claude call.
        Returns lightweight diagnoses with accurate confidence per status.
        Also catches deployments/statefulsets scaled to 0 (no pods to find otherwise).
        """
        from agent.integrations.collectors import collect_deployments
        diagnoses: list[PodDiagnosis] = []

        # ── Scaled-to-zero deployments (invisible to pod scan) ─────────────
        dep_result = collect_deployments()
        for label in dep_result.get("data", {}).get("scaled_zero", []):
            # label format: "namespace/name (deployment)" or "namespace/name (statefulset)"
            try:
                ns_name, kind_raw = label.rsplit(" ", 1)
                ns, name = ns_name.split("/", 1)
                kind = "statefulset" if "statefulset" in kind_raw else "deployment"
            except ValueError:
                continue
            if namespace != "all" and ns != namespace:
                continue
            diagnoses.append(PodDiagnosis(
                pod=name,
                namespace=ns,
                problem_type=ProblemType.UNKNOWN,
                root_cause=f"{kind.capitalize()} '{name}' in '{ns}' is scaled to 0 replicas — no pods running.",
                suggested_fix=f"Scale up: kubectl scale {kind} {name} -n {ns} --replicas=1",
                fix_command=f"kubectl scale {kind} {name} -n {ns} --replicas=1",
                confidence="high",
                explanation="Desired replicas = 0. Either intentionally scaled down or misconfigured.",
            ))

        # ── Unhealthy / restarting pods ────────────────────────────────────
        pods = get_problematic_pods()
        if namespace != "all":
            pods = [p for p in pods if p.namespace == namespace]

        log.info("k8s.scan_cluster", namespace=namespace,
                 scaled_zero=len(dep_result.get("data", {}).get("scaled_zero", [])),
                 problematic=len(pods))

        diagnoses += [
            PodDiagnosis(
                pod=pod.name,
                namespace=pod.namespace,
                problem_type=_status_to_problem_type(pod.status),
                root_cause=f"Pod is in {pod.status} state with {pod.restarts} restart(s).",
                suggested_fix="Run `agent k8s diagnose` for a full AI diagnosis and fix.",
                fix_command=None,
                confidence="high" if pod.status in _HIGH_CONFIDENCE_STATUSES else "medium",
                explanation=(
                    f"Status: {pod.status}, Ready: {pod.ready}, "
                    f"Restarts: {pod.restarts}, Age: {pod.age}"
                ),
            )
            for pod in pods
        ]
        return diagnoses

    def diagnose_pod(self, pod: PodInfo) -> PodDiagnosis:
        logs        = get_pod_logs(pod.name, pod.namespace)
        events      = get_pod_events(pod.name, pod.namespace)
        description = describe_pod(pod.name, pod.namespace)

        memories = retrieve_context(f"{pod.name} {pod.status}")

        user_text = (
            f"Pod: {pod.name}\n"
            f"Namespace: {pod.namespace}\n"
            f"Status: {pod.status}\n"
            f"Ready: {pod.ready}\n"
            f"Restarts: {pod.restarts}\n"
            f"Age: {pod.age}\n"
            f"Node: {pod.node}\n\n"
            f"--- DESCRIBE ---\n{description}\n\n"
            f"--- EVENTS ---\n{events}\n\n"
            f"--- LOGS ---\n{logs}"
        )

        system_prompt = context.build_system_prompt(_SYSTEM, memories)

        # run_sync() handles both sync callers and async callers (FastAPI / dashboard)
        llm_response = run_sync(
            llm.chat(
                messages=[context.user_message(user_text)],
                system=system_prompt,
                json_mode=True,
                max_tokens=2048,
            )
        )

        try:
            parsed = parse_llm_json(llm_response.content)
        except LLMParseError as exc:
            log.error("k8s.diagnose_pod.parse_failed",
                      pod=pod.name, error=str(exc.cause), raw=exc.raw[:200])
            # Degrade gracefully — return a low-confidence diagnosis
            parsed = {
                "problem_type": _status_to_problem_type(pod.status).value,
                "root_cause":   f"LLM response could not be parsed. Pod status: {pod.status}.",
                "suggested_fix": "Run `kubectl describe pod` and `kubectl logs` manually.",
                "fix_command":  None,
                "confidence":   "low",
                "explanation":  "Automatic diagnosis failed — inspect pod manually.",
            }

        diagnosis = PodDiagnosis(
            pod=pod.name,
            namespace=pod.namespace,
            problem_type=_to_problem_type(parsed.get("problem_type", "Unknown")),
            root_cause=parsed["root_cause"],
            suggested_fix=parsed["suggested_fix"],
            fix_command=parsed.get("fix_command"),
            confidence=parsed.get("confidence", "low"),
            explanation=parsed["explanation"],
        )

        remember(
            content=(
                f"Pod {pod.name} in {pod.namespace} — status {pod.status} — "
                f"diagnosed as {diagnosis.problem_type.value}: {diagnosis.root_cause}. "
                f"Fix: {diagnosis.suggested_fix}"
            ),
            source="k8s-diagnose",
            metadata={
                "pod":          pod.name,
                "namespace":    pod.namespace,
                "status":       pod.status,
                "problem_type": diagnosis.problem_type.value,
                "confidence":   diagnosis.confidence,
            },
        )

        log.info(
            "k8s.diagnose_pod.done",
            pod=pod.name,
            namespace=pod.namespace,
            problem_type=diagnosis.problem_type.value,
            confidence=diagnosis.confidence,
        )

        return diagnosis

    def apply_fix(self, diagnosis: PodDiagnosis) -> bool:
        if not diagnosis.fix_command:
            log.warning("k8s.apply_fix.no_command", pod=diagnosis.pod)
            return False

        log.info("k8s.apply_fix", pod=diagnosis.pod, command=diagnosis.fix_command)

        result = kubectl_apply_fix(diagnosis.fix_command)

        remember(
            content=(
                f"Applied fix for {diagnosis.pod} in {diagnosis.namespace}: "
                f"`{diagnosis.fix_command}` — "
                f"{'succeeded' if result.success else 'failed'}: "
                f"{result.error or result.output[:200]}"
            ),
            source="k8s-fix",
            metadata={
                "pod":       diagnosis.pod,
                "namespace": diagnosis.namespace,
                "command":   diagnosis.fix_command,
                "success":   result.success,
            },
        )

        log.info(
            "k8s.apply_fix.result",
            pod=diagnosis.pod,
            success=result.success,
            duration_ms=result.duration_ms,
        )

        return result.success