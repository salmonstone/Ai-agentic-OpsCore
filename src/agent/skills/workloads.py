"""
Workloads skill — controller-level and scheduling diagnosis.

Every Kubernetes capability in this repo was pod-centric: it could tell you a
pod is CrashLooping and diagnose why. It could not tell you that a Deployment
has been stuck at 2/5 available for an hour, that a DaemonSet is missing from
three nodes, that a Job has burned through its backoff limit, that a CronJob
silently stopped firing six days ago, that a pod is Pending because every node
is out of CPU, or that a PodDisruptionBudget is the reason a node drain has
been hanging since the upgrade started.

Those are the failures that actually page people, and none of them are
visible from a pod list.

Detection is deterministic — a controller is either below its desired count
or it is not, a CronJob either fired or it did not. Claude is called only to
narrate a set of already-proven issues, and every finding stands without it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import (
    WorkloadInfo, WorkloadIssue, WorkloadKind, WorkloadProblemType, WorkloadReport,
)
from agent.core.parsing import LLMParseError, parse_llm_json
from agent.integrations.kubectl import (
    apply_fix as kubectl_apply_fix, get_pdbs, get_unschedulable_pods,
    get_workload_conditions, get_workloads,
)
from agent.memory.retrieval import remember, retrieve_context
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SYSTEM = """You are a Kubernetes expert reviewing already-detected workload issues.

The issues below were detected from live controller state and are FACTS — do
not dispute or re-derive them. Your job is to explain what is happening and
what to do next.

Return JSON only with this exact structure:
{
  "analysis": "string — one paragraph an on-call engineer can act on",
  "most_urgent": "string — which single issue to address first, and why",
  "likely_common_cause": "string — if several issues share one root cause, name it; else empty"
}

Rules:
- Several Pending pods across different namespaces at the same time usually
  means the CLUSTER is out of capacity, not that each workload is misconfigured.
  Say that rather than listing them individually.
- A DaemonSet short on exactly the nodes that are NotReady is a node problem,
  not a DaemonSet problem.
- A suspended CronJob is very often deliberate. Never assume it is a bug.
"""

# Cron expressions common enough to be worth parsing exactly. Anything else
# falls back to a deliberately generous 24h staleness threshold, because a
# false "your CronJob is broken" is worse than a missed one.
_CRON_INTERVALS_MIN = {
    "* * * * *":        1,
    "*/5 * * * *":      5,
    "*/10 * * * *":    10,
    "*/15 * * * *":    15,
    "*/30 * * * *":    30,
    "0 * * * *":       60,
    "@hourly":         60,
    "0 0 * * *":     1440,
    "@daily":        1440,
    "@midnight":     1440,
    "0 0 * * 0":    10080,
    "@weekly":      10080,
}


def _expected_interval_minutes(schedule: str) -> int | None:
    """Best-effort expected gap between CronJob runs.

    Returns None when the expression is not one we can read confidently —
    the caller then uses a much looser threshold rather than guessing.
    """
    s = (schedule or "").strip()
    if s in _CRON_INTERVALS_MIN:
        return _CRON_INTERVALS_MIN[s]
    # "*/N * * * *" for any N
    parts = s.split()
    if len(parts) == 5 and parts[0].startswith("*/") and parts[1:] == ["*", "*", "*", "*"]:
        try:
            return max(1, int(parts[0][2:]))
        except ValueError:
            return None
    return None


def _age_minutes(iso: str) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - then).total_seconds() / 60.0
    except ValueError:
        return None


class WorkloadsSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "workloads"

    @property
    def description(self) -> str:
        return ("Controller-level Kubernetes diagnosis — Deployments, StatefulSets, "
                "DaemonSets, Jobs, CronJobs, scheduling and PodDisruptionBudgets.")

    def execute(self, input_data: dict) -> dict:
        mode = input_data.get("mode", "scan")
        if mode == "scan":
            return self.scan(input_data.get("namespace", "all")).model_dump()
        if mode == "diagnose":
            return self.diagnose(
                input_data["kind"], input_data["name"],
                input_data.get("namespace", "default"),
            )
        raise ValueError(f"Unknown mode: {mode!r}")

    # ------------------------------------------------------------------
    # Detectors — all deterministic
    # ------------------------------------------------------------------

    def _check_replicated(self, w: WorkloadInfo) -> list[WorkloadIssue]:
        """Deployment / StatefulSet replica health."""
        issues: list[WorkloadIssue] = []

        if w.desired == 0:
            issues.append(WorkloadIssue(
                severity="warning", problem_type=WorkloadProblemType.SCALED_ZERO,
                kind=w.kind, name=w.name, namespace=w.namespace,
                description=f"{w.kind.value} {w.namespace}/{w.name} is scaled to 0 replicas.",
                evidence=["spec.replicas = 0", f"age {w.age}"],
                fix="Scale it back up if this was not deliberate.",
                fix_command=f"kubectl scale {w.kind.value.lower()} {w.name} -n {w.namespace} --replicas=1",
            ))
            return issues

        if w.ready < w.desired:
            missing = w.desired - w.ready
            severity = "critical" if w.ready == 0 else "warning"
            conditions = get_workload_conditions(w.kind.value.lower(), w.name, w.namespace)
            evidence = [f"ready {w.ready}/{w.desired}", f"available {w.available}", f"age {w.age}"]

            stuck = False
            for c in conditions:
                if c["type"] == "Progressing" and c["reason"] == "ProgressDeadlineExceeded":
                    stuck = True
                if c["reason"] or c["message"]:
                    evidence.append(f"{c['type']}={c['status']} {c['reason']}: {c['message']}"[:200])

            issues.append(WorkloadIssue(
                severity=severity,
                problem_type=(WorkloadProblemType.ROLLOUT_STUCK if stuck
                              else WorkloadProblemType.REPLICAS_UNAVAILABLE),
                kind=w.kind, name=w.name, namespace=w.namespace,
                description=(f"{w.kind.value} {w.namespace}/{w.name} has {missing} of "
                             f"{w.desired} replicas unavailable"
                             + (" and the rollout has exceeded its progress deadline."
                                if stuck else ".")),
                evidence=evidence,
                fix=("Inspect the pods to find why they will not become ready, then "
                     "roll back if the new revision is at fault."),
                fix_command=f"kubectl rollout status {w.kind.value.lower()}/{w.name} -n {w.namespace}",
            ))
        return issues

    def _check_daemonset(self, w: WorkloadInfo) -> list[WorkloadIssue]:
        if w.desired == 0 or w.ready >= w.desired:
            return []
        missing = w.desired - w.ready
        return [WorkloadIssue(
            severity="warning" if w.ready > 0 else "critical",
            problem_type=WorkloadProblemType.DAEMONSET_NOT_SCHEDULED,
            kind=w.kind, name=w.name, namespace=w.namespace,
            description=(f"DaemonSet {w.namespace}/{w.name} is running on {w.ready} of "
                         f"{w.desired} eligible nodes — missing from {missing}."),
            evidence=[f"desiredNumberScheduled={w.desired}", f"numberReady={w.ready}",
                      "Check whether the missing nodes are NotReady, tainted, or out of resources."],
            fix="Identify the nodes without a pod from this DaemonSet and check their taints and capacity.",
            fix_command=f"kubectl get pods -n {w.namespace} -l app -o wide | grep {w.name}",
        )]

    def _check_job(self, w: WorkloadInfo) -> list[WorkloadIssue]:
        issues: list[WorkloadIssue] = []

        if w.failed >= w.backoff_limit and w.backoff_limit > 0:
            issues.append(WorkloadIssue(
                severity="critical", problem_type=WorkloadProblemType.JOB_BACKOFF_EXCEEDED,
                kind=w.kind, name=w.name, namespace=w.namespace,
                description=(f"Job {w.namespace}/{w.name} failed {w.failed} times and has "
                             f"exhausted its backoffLimit of {w.backoff_limit}. It will not retry."),
                evidence=[f"failed={w.failed}", f"backoffLimit={w.backoff_limit}",
                          f"succeeded={w.succeeded}", f"age {w.age}"],
                fix="Read the failed pod's logs, fix the cause, then delete and recreate the Job.",
                fix_command=f"kubectl logs -n {w.namespace} job/{w.name} --tail=100",
            ))
            return issues

        # Active for a long time with nothing completed — the classic hung job.
        if w.active > 0 and w.succeeded == 0 and w.age.endswith(("h", "d")):
            issues.append(WorkloadIssue(
                severity="warning", problem_type=WorkloadProblemType.JOB_STUCK,
                kind=w.kind, name=w.name, namespace=w.namespace,
                description=(f"Job {w.namespace}/{w.name} has been active for {w.age} "
                             f"with nothing completed."),
                evidence=[f"active={w.active}", f"succeeded={w.succeeded}", f"age {w.age}"],
                fix="Check whether the pod is progressing or blocked on a dependency.",
                fix_command=f"kubectl logs -n {w.namespace} job/{w.name} --tail=50",
            ))
        return issues

    def _check_cronjob(self, w: WorkloadInfo) -> list[WorkloadIssue]:
        issues: list[WorkloadIssue] = []

        if w.suspended:
            issues.append(WorkloadIssue(
                severity="info", problem_type=WorkloadProblemType.CRONJOB_SUSPENDED,
                kind=w.kind, name=w.name, namespace=w.namespace,
                description=(f"CronJob {w.namespace}/{w.name} is suspended and will not run "
                             f"(schedule {w.schedule!r})."),
                evidence=["spec.suspend = true",
                          "Often deliberate — confirm with the owner before resuming."],
                fix="Resume it if the suspension was not intentional.",
                fix_command=(f"kubectl patch cronjob {w.name} -n {w.namespace} "
                             f"-p '{{\"spec\":{{\"suspend\":false}}}}'"),
                auto_fixable=False,
            ))
            return issues

        since = _age_minutes(w.last_schedule_time)
        expected = _expected_interval_minutes(w.schedule)

        if since is None:
            # Never fired. Only interesting once the schedule has had time to.
            if w.age.endswith(("h", "d")):
                issues.append(WorkloadIssue(
                    severity="warning", problem_type=WorkloadProblemType.CRONJOB_NOT_FIRING,
                    kind=w.kind, name=w.name, namespace=w.namespace,
                    description=(f"CronJob {w.namespace}/{w.name} has never run since it was "
                                 f"created {w.age} ago (schedule {w.schedule!r})."),
                    evidence=["status.lastScheduleTime is empty",
                              "Check the schedule expression and the controller's events."],
                    fix="Verify the cron expression is valid and the controller is healthy.",
                    fix_command=f"kubectl describe cronjob {w.name} -n {w.namespace}",
                ))
            return issues

        # Two missed windows is the signal — one can be a slow node or a
        # long-running previous job, and alerting on that is just noise.
        threshold = (expected * 2) if expected else 1440
        if since > threshold:
            confident = expected is not None
            issues.append(WorkloadIssue(
                severity="warning", problem_type=WorkloadProblemType.CRONJOB_NOT_FIRING,
                kind=w.kind, name=w.name, namespace=w.namespace,
                description=(f"CronJob {w.namespace}/{w.name} last ran {since/60:.1f}h ago but "
                             f"is scheduled {w.schedule!r}"
                             + (f" (expected roughly every {expected}min)." if confident
                                else " (schedule not parsed — using a 24h threshold).")),
                evidence=[f"lastScheduleTime={w.last_schedule_time}",
                          f"threshold={threshold}min",
                          f"active={w.active}"],
                fix="Check for a stuck active job blocking the next run, or a failing controller.",
                fix_command=f"kubectl get jobs -n {w.namespace} | grep {w.name}",
            ))
        return issues

    def _check_scheduling(self, namespace: str) -> list[WorkloadIssue]:
        """Pending pods, with the scheduler's own explanation attached."""
        issues: list[WorkloadIssue] = []
        for pod in get_unschedulable_pods(namespace):
            issues.append(WorkloadIssue(
                severity="critical", problem_type=WorkloadProblemType.UNSCHEDULABLE,
                kind=WorkloadKind.DEPLOYMENT,   # the pod's controller is not resolved here
                name=pod["name"], namespace=pod["namespace"],
                description=(f"Pod {pod['namespace']}/{pod['name']} cannot be scheduled "
                             f"({pod['reason'] or 'Unschedulable'}), pending for {pod['age']}."),
                evidence=[pod["message"] or "(scheduler gave no message)",
                          "The scheduler's message is the authoritative reason — read it first."],
                fix="Add capacity, relax the node selector/affinity, or tolerate the taint.",
                fix_command=f"kubectl describe pod {pod['name']} -n {pod['namespace']}",
            ))
        return issues

    def _check_pdbs(self, namespace: str) -> list[WorkloadIssue]:
        """PDBs allowing zero disruptions — the usual reason a node drain hangs."""
        issues: list[WorkloadIssue] = []
        for pdb in get_pdbs(namespace):
            if pdb["disruptions_allowed"] > 0:
                continue
            if pdb["expected_pods"] == 0:
                continue      # PDB selecting nothing — a config smell, not an outage
            issues.append(WorkloadIssue(
                severity="warning", problem_type=WorkloadProblemType.PDB_BLOCKING,
                kind=WorkloadKind.DEPLOYMENT,
                name=pdb["name"], namespace=pdb["namespace"],
                description=(f"PodDisruptionBudget {pdb['namespace']}/{pdb['name']} allows 0 "
                             f"disruptions — any node drain covering these pods will hang."),
                evidence=[f"currentHealthy={pdb['current_healthy']}",
                          f"desiredHealthy={pdb['desired_healthy']}",
                          f"expectedPods={pdb['expected_pods']}",
                          f"minAvailable={pdb['min_available'] or '-'} "
                          f"maxUnavailable={pdb['max_unavailable'] or '-'}"],
                fix=("Restore the workload to full health, or relax the budget before draining. "
                     "Do not delete the PDB to unblock a drain — that defeats its purpose."),
                fix_command=f"kubectl get pdb {pdb['name']} -n {pdb['namespace']} -o yaml",
            ))
        return issues

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def scan(self, namespace: str = "all", use_llm: bool = True) -> WorkloadReport:
        """Every controller-level and scheduling problem in one pass."""
        log.info("workloads.scan.start", namespace=namespace)
        report = WorkloadReport(
            namespace=namespace,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        checkers = {
            "deployment":  self._check_replicated,
            "statefulset": self._check_replicated,
            "daemonset":   self._check_daemonset,
            "job":         self._check_job,
            "cronjob":     self._check_cronjob,
        }

        for kind, checker in checkers.items():
            workloads = get_workloads(kind, namespace)
            report.by_kind[kind] = len(workloads)
            report.total_workloads += len(workloads)
            for w in workloads:
                try:
                    report.issues.extend(checker(w))
                except Exception as e:
                    # One malformed object must never abort the whole scan.
                    log.warning("workloads.check_failed", kind=kind,
                                name=w.name, error=str(e)[:200])

        report.issues.extend(self._check_scheduling(namespace))
        report.issues.extend(self._check_pdbs(namespace))

        order = {"critical": 0, "warning": 1, "info": 2}
        report.issues.sort(key=lambda i: order.get(i.severity, 3))

        if not report.issues:
            report.analysis = (f"All {report.total_workloads} workloads healthy across "
                               f"{len(report.by_kind)} kinds. No scheduling failures and no "
                               f"PodDisruptionBudget is blocking disruption.")
        elif use_llm:
            report.analysis = self._narrate(report)
        else:
            report.analysis = self._fallback_analysis(report)

        self._remember(report)
        log.info("workloads.scan.done", namespace=namespace,
                 workloads=report.total_workloads, issues=len(report.issues))
        return report

    # ------------------------------------------------------------------
    # Diagnose one workload
    # ------------------------------------------------------------------

    def diagnose(self, kind: str, name: str, namespace: str = "default") -> dict:
        """Deep dive on one controller, including its status conditions."""
        workloads = get_workloads(kind, namespace)
        target = next((w for w in workloads if w.name == name), None)
        if target is None:
            return {"found": False,
                    "message": (f"No {kind} named {name!r} in namespace {namespace!r}. "
                                f"Run `agent workloads scan -n {namespace}` to see what exists.")}

        checker = {
            "deployment": self._check_replicated, "statefulset": self._check_replicated,
            "daemonset": self._check_daemonset, "job": self._check_job,
            "cronjob": self._check_cronjob,
        }.get(kind.lower().rstrip("s"), self._check_replicated)

        issues = checker(target)
        conditions = get_workload_conditions(kind, name, namespace)
        return {
            "found": True,
            "workload": target.model_dump(),
            "conditions": conditions,
            "issues": [i.model_dump() for i in issues],
            "healthy": not issues,
        }

    # ------------------------------------------------------------------
    # Remediation — gated, one named resource, never bulk
    # ------------------------------------------------------------------

    def apply_fix(self, issue: WorkloadIssue, confirmed: bool = False) -> dict:
        """Run one issue's fix_command.

        Mirrors every other mutating path in this repo: nothing happens
        without an explicit confirmation, the command still passes through
        kubectl.apply_fix's destructive-command guard, and the action is
        scoped to the single named resource the issue is about.
        """
        if not issue.fix_command:
            return {"applied": False, "message": "This issue has no automated fix command."}
        if not confirmed:
            return {"applied": False,
                    "message": "Set confirmed=True to actually apply this fix.",
                    "fix_command": issue.fix_command}

        # Read-only commands are diagnostics, not fixes — refuse rather than
        # pretend that running `kubectl describe` remediated anything.
        first_verb = issue.fix_command.split()[1] if len(issue.fix_command.split()) > 1 else ""
        if first_verb in ("get", "describe", "logs", "rollout"):
            return {"applied": False,
                    "message": (f"{issue.fix_command!r} is a diagnostic command, not a "
                                f"remediation. Run it yourself to gather evidence."),
                    "fix_command": issue.fix_command}

        result = kubectl_apply_fix(issue.fix_command)
        remember(
            content=(f"Workload fix on {issue.namespace}/{issue.name} "
                     f"({issue.problem_type.value}): {issue.fix_command} -> "
                     f"{'ok' if result.success else 'failed'}"),
            source="workloads",
            metadata={"name": issue.name, "namespace": issue.namespace,
                      "problem_type": issue.problem_type.value, "success": result.success},
        )
        return {"applied": result.success, "fix_command": issue.fix_command,
                "output": result.output[:1000], "error": result.error[:500]}

    # ------------------------------------------------------------------
    # Narrative
    # ------------------------------------------------------------------

    def _narrate(self, report: WorkloadReport) -> str:
        lines = [
            f"Namespace: {report.namespace}",
            f"Workloads scanned: {report.total_workloads} ({report.by_kind})",
            "",
            "ISSUES (detected from live controller state):",
        ]
        for i in report.issues[:25]:
            lines.append(f"  [{i.severity}] {i.problem_type.value} {i.namespace}/{i.name}: {i.description}")
            for e in i.evidence[:2]:
                if e:
                    lines.append(f"      - {e}")

        memories = retrieve_context(f"workloads {report.namespace} issues")
        try:
            response = run_sync(llm.chat(
                messages=[context.user_message("\n".join(lines))],
                system=context.build_system_prompt(_SYSTEM, memories),
                json_mode=True, max_tokens=800,
            ))
            parsed = parse_llm_json(response.content)
        except LLMParseError as exc:
            log.warning("workloads.narrate.parse_failed", error=str(exc.cause))
            return self._fallback_analysis(report)
        except Exception as exc:
            log.warning("workloads.narrate.failed", error=str(exc)[:200])
            return self._fallback_analysis(report)

        parts = [parsed.get("analysis", "")]
        if parsed.get("most_urgent"):
            parts.append(f"Address first: {parsed['most_urgent']}")
        if parsed.get("likely_common_cause"):
            parts.append(f"Common cause: {parsed['likely_common_cause']}")
        return "\n\n".join(p for p in parts if p).strip()

    @staticmethod
    def _fallback_analysis(report: WorkloadReport) -> str:
        crit = sum(1 for i in report.issues if i.severity == "critical")
        warn = sum(1 for i in report.issues if i.severity == "warning")
        kinds = sorted({i.problem_type.value for i in report.issues})
        return (f"{crit} critical and {warn} warning issues across "
                f"{report.total_workloads} workloads ({', '.join(kinds)}). "
                f"AI narration unavailable — these detections come from controller "
                f"state and stand on their own.")

    def _remember(self, report: WorkloadReport) -> None:
        if not report.issues:
            return
        top = report.issues[0]
        remember(
            content=(f"Workload scan {report.namespace}: {len(report.issues)} issues, "
                     f"most severe {top.problem_type.value} on {top.namespace}/{top.name} "
                     f"— {top.description}"),
            source="workloads",
            metadata={"namespace": report.namespace, "issues": len(report.issues),
                      "top_problem": top.problem_type.value, "top_name": top.name},
        )
