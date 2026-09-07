"""
JenkinsSkill — autonomous Jenkins CI/CD monitoring and self-healing.

Same shape as K8sSkill/IngressSkill/TLS skill: a thin AI layer over
integrations/jenkins.py (the raw client). Design principle: continuously
observe, detect early, diagnose root cause, remediate safely, validate
recovery, prevent recurrence — minimal human intervention for known-safe
patterns, human approval for anything unknown or risky.

Cheap, fast regex pattern-detection runs BEFORE any Claude call — known
failure signatures (credential errors, disk full, OOM, timeouts, docker
daemon issues, flaky-test history) are classified for free. Claude is only
called when the pattern is unclear.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

from agent.core import context, llm, safety
from agent.core.models import (
    BuildInfo,
    JenkinsDiagnosis,
    JenkinsFixAction,
    JenkinsFixResult,
    JenkinsPattern,
    JenkinsProblemType,
    JenkinsScanReport,
)
from agent.core.parsing import parse_llm_json
from agent.integrations import jenkins as jk
from agent.memory.retrieval import remember, retrieve_context
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Auto-fix policy — routed through the shared safety engine (core/safety.py)
# rather than a private whitelist, so this domain's rules are inspectable
# via `agent policy list` alongside every other skill's.
# ---------------------------------------------------------------------------

def _is_auto_fixable(diagnosis: JenkinsDiagnosis) -> bool:
    if diagnosis.risk_level == "high":
        return False  # Claude's own per-instance risk signal can still veto
    return safety.is_auto_approved(
        "jenkins", diagnosis.problem_type.value, diagnosis.fix_action.value, diagnosis.confidence,
    )


# ---------------------------------------------------------------------------
# Pattern detection — fast, free, runs before Claude
# ---------------------------------------------------------------------------

_LOG_PATTERNS: list[tuple[JenkinsProblemType, JenkinsFixAction, str, re.Pattern]] = [
    (JenkinsProblemType.CREDENTIAL_EXPIRED, JenkinsFixAction.MANUAL_ONLY,
     "Authentication/authorization failure against a remote system",
     re.compile(r"403 Forbidden|invalid credentials|authentication failed|401 Unauthorized", re.I)),

    (JenkinsProblemType.DISK_FULL, JenkinsFixAction.MANUAL_ONLY,
     "Build node has run out of disk space",
     re.compile(r"no space left on device|disk quota exceeded", re.I)),

    (JenkinsProblemType.OUT_OF_MEMORY, JenkinsFixAction.MANUAL_ONLY,
     "Build process ran out of memory",
     re.compile(r"java\.lang\.OutOfMemoryError|Cannot allocate memory|Killed process.*oom", re.I)),

    (JenkinsProblemType.DOCKER_ERROR, JenkinsFixAction.CLEAR_WORKSPACE,
     "Docker daemon unreachable or out of resources on the build node",
     re.compile(r"Cannot connect to the Docker daemon|docker: no space left on device", re.I)),

    (JenkinsProblemType.AGENT_OFFLINE, JenkinsFixAction.RESTART_AGENT,
     "Build agent disconnected mid-build",
     re.compile(r"hudson\.remoting\.ChannelClosedException|Agent went offline|"
                r"channel is already closed|Slave went offline", re.I)),

    (JenkinsProblemType.NETWORK_ERROR, JenkinsFixAction.RETRIGGER_BUILD,
     "Transient network failure (DNS/connection) during the build",
     re.compile(r"Connection refused|UnknownHostException|Network is unreachable|Could not resolve host", re.I)),

    (JenkinsProblemType.BUILD_TIMEOUT, JenkinsFixAction.CANCEL_AND_RETRIGGER,
     "Build exceeded its configured timeout",
     re.compile(r"Timeout after|timed out waiting|Build timed out", re.I)),

    (JenkinsProblemType.BAD_JENKINSFILE_SYNTAX, JenkinsFixAction.MANUAL_ONLY,
     "Pipeline script has a syntax error",
     re.compile(r"WorkflowScript:\s*\d+:\s*.*unexpected token|MissingPropertyException", re.I)),

    (JenkinsProblemType.PERMISSION_DENIED, JenkinsFixAction.MANUAL_ONLY,
     "Filesystem or resource permission error on the build node",
     re.compile(r"Permission denied", re.I)),
]


def _detect_pattern(log_text: str, history: list[BuildInfo]) -> JenkinsDiagnosis | None:
    """Regex-match known failure signatures. Returns None if nothing matched
    with high confidence — caller should fall back to Claude."""
    for problem_type, fix_action, root_cause, pattern in _LOG_PATTERNS:
        if pattern.search(log_text):
            return JenkinsDiagnosis(
                problem_type=problem_type,
                root_cause=root_cause,
                confidence="high",
                fix_action=fix_action,
                explanation=f"Matched known failure signature for {problem_type.value}.",
                prevention=_PREVENTION_HINTS.get(problem_type, ""),
                auto_fixable=safety.is_auto_approved("jenkins", problem_type.value, fix_action.value, "high"),
                risk_level="low" if fix_action != JenkinsFixAction.MANUAL_ONLY else "medium",
            )

    # Flaky-test heuristic: failed now, but passed most of recent history
    if history:
        recent = history[:5]
        pass_count = sum(1 for b in recent if b.status == "SUCCESS")
        fail_count = sum(1 for b in recent if b.status == "FAILURE")
        if fail_count >= 1 and pass_count >= 3 and recent[0].status != "SUCCESS":
            return JenkinsDiagnosis(
                problem_type=JenkinsProblemType.FLAKY_TEST,
                root_cause=(
                    f"Intermittent failure — passed {pass_count} of last "
                    f"{len(recent)} builds, same job otherwise stable."
                ),
                confidence="high",
                fix_action=JenkinsFixAction.RETRIGGER_BUILD,
                explanation="Failure does not correlate with a code or infra change; "
                            "pattern matches a flaky test/race condition.",
                prevention="Add retry logic to the flaky test, or fix the underlying race condition.",
                auto_fixable=True,
                risk_level="low",
            )

    return None


_PREVENTION_HINTS = {
    JenkinsProblemType.AGENT_OFFLINE: "Investigate the agent's connectivity/resource limits; consider a supervised restart policy.",
    JenkinsProblemType.CREDENTIAL_EXPIRED: "Rotate the credential and set a renewal reminder before expiry.",
    JenkinsProblemType.DISK_FULL: "Add a workspace-cleanup post-build step or increase node disk size.",
    JenkinsProblemType.OUT_OF_MEMORY: "Increase the JVM/container memory limit or reduce build parallelism.",
    JenkinsProblemType.DOCKER_ERROR: "Add periodic `docker system prune` to the build node's maintenance job.",
    JenkinsProblemType.NETWORK_ERROR: "Add retry-with-backoff around network-dependent build steps.",
    JenkinsProblemType.BUILD_TIMEOUT: "Profile the build to find the slow stage; raise the timeout only if justified.",
    JenkinsProblemType.BAD_JENKINSFILE_SYNTAX: "Add Jenkinsfile linting (`jenkins-cli declarative-linter`) to pre-commit.",
    JenkinsProblemType.PERMISSION_DENIED: "Fix file/directory ownership on the build node's workspace root.",
}


# ---------------------------------------------------------------------------
# System prompt for Claude diagnosis (unclear/unknown cases only)
# ---------------------------------------------------------------------------

_DIAGNOSE_SYSTEM = """\
You are a senior DevOps engineer and Jenkins expert specializing in CI/CD
pipeline failure analysis.

Analyze this Jenkins build failure. You will receive: console log tail,
job config, build history, and past similar incidents from memory.

Identify:
1. problem_type (exact enum value)
2. root_cause (specific, not generic)
3. confidence (high/medium/low)
4. fix_action (exact enum value)
5. fix_params (parameters needed for the fix, as a JSON object)
6. explanation (technical but clear)
7. prevention (how to stop recurrence)
8. auto_fixable (true only if safe and known)
9. risk_level (low/medium/high)

problem_type must be exactly one of:
  FLAKY_TEST | BROKEN_DEPENDENCY | AGENT_OFFLINE | DISK_FULL |
  CREDENTIAL_EXPIRED | BAD_JENKINSFILE_SYNTAX | MERGE_CONFLICT |
  TEST_TIMEOUT | BUILD_TIMEOUT | NETWORK_ERROR | DOCKER_ERROR |
  OUT_OF_MEMORY | PERMISSION_DENIED | STUCK_IN_QUEUE |
  INFRASTRUCTURE_ISSUE | UNKNOWN

fix_action must be exactly one of:
  RETRIGGER_BUILD | RESTART_AGENT | CLEAR_WORKSPACE |
  CANCEL_AND_RETRIGGER | TOGGLE_AGENT_OFFLINE | MANUAL_ONLY | NO_ACTION_NEEDED

For flaky tests: check if the same test fails intermittently across build history.
For agent issues: check if other jobs are affected too (systemic vs isolated).
For dependency issues: check if a version was recently bumped in the changes list.

Return JSON only:
{
  "problem_type": "...", "root_cause": "...", "confidence": "...",
  "fix_action": "...", "fix_params": {}, "explanation": "...",
  "prevention": "...", "auto_fixable": true|false, "risk_level": "..."
}"""

_PATTERNS_SYSTEM = """\
You are a senior SRE reviewing 30 days of Jenkins CI/CD incident history.
Given these incidents, identify:
- Which jobs are chronically flaky
- Which agents go offline repeatedly
- Any time-based patterns (e.g. always fails Monday morning)
- The systemic root cause and an architectural change that would prevent recurrence

Return JSON only:
{"patterns": [{"title": "...", "likely_cause": "...", "recommendation": "...", "affected_jobs": ["..."]}]}"""


def _to_enum(cls, raw: str, default):
    try:
        return cls(raw)
    except (ValueError, TypeError):
        return default


class JenkinsSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "jenkins-heal"

    @property
    def description(self) -> str:
        return "Monitor Jenkins CI/CD, diagnose build/agent/queue failures, and safely self-heal known patterns."

    def execute(self, input_data: dict) -> dict:
        mode = input_data.get("mode", "scan")
        if mode == "scan":
            return self.scan().model_dump()
        if mode == "diagnose":
            build = jk.get_build_info(input_data["job_name"], input_data["build_number"])
            log_text = jk.get_console_log(input_data["job_name"], input_data["build_number"])
            history = jk.get_build_history(input_data["job_name"])
            past = retrieve_context(f"jenkins {input_data['job_name']} failure")
            return self.diagnose(input_data["job_name"], build, log_text, history, past).model_dump()
        raise ValueError(f"Unknown mode: {mode!r}")

    # ------------------------------------------------------------------
    # scan — the primary observation method
    # ------------------------------------------------------------------

    def scan(self) -> JenkinsScanReport:
        log.info("jenkins.scan.start")

        all_jobs = jk.get_all_jobs()
        failed_jobs = [j for j in all_jobs if not j.is_folder and j.is_failing]
        offline_nodes = jk.get_offline_nodes()
        stuck_queue = jk.get_stuck_queue_items()

        report = JenkinsScanReport(
            total_jobs=sum(1 for j in all_jobs if not j.is_folder),
            failing_jobs=sum(1 for j in failed_jobs if "red" in j.color or "aborted" in j.color),
            unstable_jobs=sum(1 for j in failed_jobs if "yellow" in j.color),
            offline_nodes=len(offline_nodes),
            stuck_queue_items=len(stuck_queue),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        for job in failed_jobs:
            if job.last_build_number is None:
                continue
            try:
                build = jk.get_build_info(job.name, job.last_build_number)
                log_text = jk.get_console_log(job.name, job.last_build_number)
                history = jk.get_build_history(job.name, count=5)
                past = retrieve_context(f"jenkins {job.name} failure")
                diagnosis = self.diagnose(job.name, build, log_text, history, past)
                report.diagnoses.append(diagnosis)
            except Exception as exc:
                log.warning("jenkins.scan.job_error", job=job.name, error=str(exc))
                report.diagnoses.append(JenkinsDiagnosis(
                    job_name=job.name,
                    build_number=job.last_build_number or 0,
                    problem_type=JenkinsProblemType.UNKNOWN,
                    root_cause=f"Could not fetch diagnosis data: {exc}",
                    confidence="low",
                    fix_action=JenkinsFixAction.MANUAL_ONLY,
                ))

        report.auto_fixable_count = sum(1 for d in report.diagnoses if d.auto_fixable)
        report.manual_count = len(report.diagnoses) - report.auto_fixable_count
        report.health_score = self.get_health_score(report)

        remember(
            content=(
                f"Jenkins scan: {report.failing_jobs} failing, {report.unstable_jobs} unstable, "
                f"{report.offline_nodes} offline node(s), {report.stuck_queue_items} stuck queue item(s). "
                f"Health score {report.health_score}/100."
            ),
            source="jenkins-scan",
            metadata={"health_score": report.health_score,
                      "failing_jobs": report.failing_jobs,
                      "offline_nodes": report.offline_nodes},
        )
        log.info("jenkins.scan.done", diagnoses=len(report.diagnoses), health_score=report.health_score)
        return report

    # ------------------------------------------------------------------
    # list_jobs / trigger_build — pure data-fetch and direct action, no AI
    # reasoning involved, so these skip Claude entirely (deterministic code
    # for deterministic operations).
    # ------------------------------------------------------------------

    def list_jobs(self, folder: str | None = None) -> list[JenkinsJob]:
        """Every job Jenkins knows about, regardless of pass/fail status —
        unlike scan(), which only surfaces failing ones with a diagnosis."""
        return jk.get_all_jobs(folder)

    def trigger_build(self, job_name: str, params: dict | None = None,
                       confirm_destructive_name: bool = False) -> int:
        """
        Directly trigger a build for a named job — a deliberate, explicit
        action the caller asked for, not an autonomous decision, so this
        does NOT go through the safety/policy engine's auto-fix gating
        (that gate is for AI-initiated fixes, not a human/client explicitly
        naming a job and asking for a build).

        If job_name itself suggests destructive intent (contains "destroy",
        "teardown", "nuke", "purge", "wipe", "decommission"), the caller
        having reached this method at all is NOT sufficient — a distinct
        confirm_destructive_name=True is required in addition, so a single
        upstream "yes" can never be enough for something named like this.

        Returns the Jenkins queue item id (not the eventual build number —
        Jenkins assigns that once an executor is free). -1 if Jenkins didn't
        return a usable queue location.
        """
        token = safety.name_suggests_destructive(job_name)
        if token and not confirm_destructive_name:
            raise ValueError(
                f"'{job_name}' looks destructive (matched '{token}'). "
                "Pass confirm_destructive_name=True after getting a distinct, explicit "
                "acknowledgment — a normal confirmation is not enough for this."
            )
        jobs = {j.name: j for j in jk.get_all_jobs()}
        if job_name not in jobs:
            raise ValueError(f"No such Jenkins job: '{job_name}'.")
        queue_id = jk.retrigger_build(job_name, params)
        log.info("jenkins.trigger_build", job=job_name, queue_id=queue_id)
        remember(
            content=f"Manually triggered a build for {job_name}.",
            source="jenkins-trigger",
            metadata={"job": job_name, "queue_id": queue_id},
        )
        return queue_id

    # ------------------------------------------------------------------
    # diagnose — pattern match first (free), Claude only if unclear
    # ------------------------------------------------------------------

    def diagnose(self, job_name: str, build: BuildInfo, log_text: str,
                 history: list[BuildInfo], past_incidents: list) -> JenkinsDiagnosis:
        log.info("jenkins.diagnose.start", job=job_name, build=build.number)

        diagnosis = _detect_pattern(log_text, history)
        if diagnosis is not None:
            diagnosis.job_name = job_name
            diagnosis.build_number = build.number
            log.info("jenkins.diagnose.pattern_matched", job=job_name,
                     problem_type=diagnosis.problem_type.value)
            self._remember_diagnosis(diagnosis)
            return diagnosis

        # Unclear — ask Claude
        try:
            config = jk.get_job_config(job_name)
        except Exception:
            config = "(config not available)"

        history_text = "\n".join(
            f"  #{b.number}: {b.status} ({b.duration_ms}ms, node={b.node})" for b in history
        )
        past_text = "\n".join(f"  - {m.content}" for m in past_incidents) or "  (none)"

        user_text = (
            f"Job: {job_name}\nBuild: #{build.number}  Status: {build.status}\n"
            f"Causes: {', '.join(build.causes) or '(none)'}\n"
            f"Changes in this build: {', '.join(build.changes) or '(none)'}\n"
            f"Node: {build.node or '(unknown)'}\n\n"
            f"--- BUILD HISTORY (most recent first) ---\n{history_text or '(none)'}\n\n"
            f"--- PAST SIMILAR INCIDENTS FROM MEMORY ---\n{past_text}\n\n"
            f"--- CONSOLE LOG (tail) ---\n{log_text[-4000:]}\n\n"
            f"--- JOB CONFIG (truncated) ---\n{config[:1500]}"
        )

        system_prompt = context.build_system_prompt(_DIAGNOSE_SYSTEM, past_incidents)
        response = asyncio.run(llm.chat(
            messages=[context.user_message(user_text)],
            system=system_prompt,
            json_mode=True,
            max_tokens=1500,
        ))
        parsed = parse_llm_json(response.content)

        diagnosis = JenkinsDiagnosis(
            job_name=job_name,
            build_number=build.number,
            problem_type=_to_enum(JenkinsProblemType, parsed.get("problem_type", "UNKNOWN"), JenkinsProblemType.UNKNOWN),
            root_cause=parsed.get("root_cause", ""),
            confidence=parsed.get("confidence", "low"),
            fix_action=_to_enum(JenkinsFixAction, parsed.get("fix_action", "MANUAL_ONLY"), JenkinsFixAction.MANUAL_ONLY),
            fix_params=parsed.get("fix_params", {}) or {},
            explanation=parsed.get("explanation", ""),
            prevention=parsed.get("prevention", ""),
            auto_fixable=bool(parsed.get("auto_fixable", False)),
            risk_level=parsed.get("risk_level", "medium"),
        )
        # Never trust the model's auto_fixable claim over our own policy
        diagnosis.auto_fixable = _is_auto_fixable(diagnosis)

        self._remember_diagnosis(diagnosis)
        log.info("jenkins.diagnose.done", job=job_name,
                 problem_type=diagnosis.problem_type.value, confidence=diagnosis.confidence)
        return diagnosis

    def _remember_diagnosis(self, d: JenkinsDiagnosis) -> None:
        remember(
            content=(
                f"{d.job_name} #{d.build_number} failed: {d.root_cause}. "
                f"Fix: {d.fix_action.value}."
            ),
            source="jenkins",
            metadata={
                "job": d.job_name, "build": d.build_number,
                "problem_type": d.problem_type.value,
                "fix_action": d.fix_action.value,
                "auto_fixable": d.auto_fixable,
            },
        )

    # ------------------------------------------------------------------
    # apply_fix — never runs without confirmed=True
    # ------------------------------------------------------------------

    def apply_fix(self, diagnosis: JenkinsDiagnosis, confirmed: bool = False) -> JenkinsFixResult:
        if not confirmed:
            return JenkinsFixResult(success=False, message="Fix not applied — confirmation required.")

        action = diagnosis.fix_action
        log.info("jenkins.apply_fix", job=diagnosis.job_name, action=action.value)

        if action == JenkinsFixAction.MANUAL_ONLY:
            return JenkinsFixResult(
                success=False, verified=False,
                message="This issue requires manual intervention. See explanation for steps.",
            )

        if action == JenkinsFixAction.NO_ACTION_NEEDED:
            return JenkinsFixResult(success=True, verified=True, message="No action needed.")

        try:
            if action == JenkinsFixAction.RETRIGGER_BUILD:
                jk.retrigger_build(diagnosis.job_name, diagnosis.fix_params.get("params"))
                time.sleep(30)
                new_build = self._latest_build_number(diagnosis.job_name)
                result = JenkinsFixResult(success=True, action_taken="retrigger_build",
                                           new_build_number=new_build,
                                           message=f"Retriggered build #{new_build}.")

            elif action == JenkinsFixAction.RESTART_AGENT:
                node = diagnosis.fix_params.get("node", "")
                jk.toggle_agent_offline(node, offline=True, message="AtlasOS: restarting")
                time.sleep(10)
                jk.restart_agent(node)
                time.sleep(60)
                online = any(n.name == node and n.online for n in jk.get_all_nodes())
                result = JenkinsFixResult(success=online, action_taken="restart_agent",
                                           message=f"Agent '{node}' {'is back online' if online else 'still offline'}.")

            elif action == JenkinsFixAction.CLEAR_WORKSPACE:
                jk.clear_workspace(diagnosis.job_name)
                jk.retrigger_build(diagnosis.job_name)
                time.sleep(30)
                new_build = self._latest_build_number(diagnosis.job_name)
                result = JenkinsFixResult(success=True, action_taken="clear_workspace",
                                           new_build_number=new_build,
                                           message=f"Workspace cleared, build #{new_build} triggered.")

            elif action == JenkinsFixAction.CANCEL_AND_RETRIGGER:
                jk.cancel_build(diagnosis.job_name, diagnosis.build_number)
                time.sleep(5)
                jk.retrigger_build(diagnosis.job_name)
                time.sleep(30)
                new_build = self._latest_build_number(diagnosis.job_name)
                result = JenkinsFixResult(success=True, action_taken="cancel_and_retrigger",
                                           new_build_number=new_build,
                                           message=f"Cancelled #{diagnosis.build_number}, triggered #{new_build}.")

            elif action == JenkinsFixAction.TOGGLE_AGENT_OFFLINE:
                node = diagnosis.fix_params.get("node", "")
                ok = jk.toggle_agent_offline(node, offline=True,
                                              message=diagnosis.fix_params.get("message", "AtlasOS maintenance"))
                result = JenkinsFixResult(success=ok, action_taken="toggle_agent_offline",
                                           message=f"Agent '{node}' marked offline for maintenance.")
            else:
                result = JenkinsFixResult(success=False, message=f"Unrecognized fix action: {action}")

        except Exception as exc:
            log.error("jenkins.apply_fix.error", job=diagnosis.job_name, error=str(exc))
            result = JenkinsFixResult(success=False, message=f"Fix failed: {exc}")

        remember(
            content=f"Applied Jenkins fix for {diagnosis.job_name}: {action.value} — "
                    f"{'succeeded' if result.success else 'failed'}. {result.message}",
            source="jenkins-fix",
            metadata={"job": diagnosis.job_name, "action": action.value, "success": result.success},
        )
        return result

    def _latest_build_number(self, job_name: str) -> int | None:
        jobs = {j.name: j for j in jk.get_all_jobs()}
        job = jobs.get(job_name)
        return job.last_build_number if job else None

    # ------------------------------------------------------------------
    # verify_fix — poll for recovery, escalate on failure
    # ------------------------------------------------------------------

    def verify_fix(self, diagnosis: JenkinsDiagnosis, fix_result: JenkinsFixResult,
                    timeout_s: int = 600, poll_interval_s: int = 10) -> bool:
        verified = False

        if diagnosis.fix_action in (JenkinsFixAction.RETRIGGER_BUILD,
                                     JenkinsFixAction.CLEAR_WORKSPACE,
                                     JenkinsFixAction.CANCEL_AND_RETRIGGER):
            if fix_result.new_build_number is None:
                verified = False
            else:
                deadline = time.time() + timeout_s
                while time.time() < deadline:
                    try:
                        build = jk.get_build_info(diagnosis.job_name, fix_result.new_build_number)
                    except Exception:
                        time.sleep(poll_interval_s)
                        continue
                    if build.status == "SUCCESS":
                        verified = True
                        break
                    if build.status in ("FAILURE", "ABORTED"):
                        verified = False
                        break
                    time.sleep(poll_interval_s)

        elif diagnosis.fix_action == JenkinsFixAction.RESTART_AGENT:
            node = diagnosis.fix_params.get("node", "")
            deadline = time.time() + 120
            while time.time() < deadline:
                if any(n.name == node and n.online for n in jk.get_all_nodes()):
                    verified = True
                    break
                time.sleep(15)

        else:
            verified = fix_result.success

        remember(
            content=f"Verification of Jenkins fix for {diagnosis.job_name}: "
                    f"{'RECOVERED' if verified else 'DID NOT RECOVER'}.",
            source="jenkins-verify",
            metadata={"job": diagnosis.job_name, "verified": verified},
        )

        if not verified:
            diagnosis.fix_action = JenkinsFixAction.MANUAL_ONLY
            diagnosis.auto_fixable = False
            self._alert(f"ALERT: Auto-fix failed for {diagnosis.job_name} — needs manual intervention.",
                        severity="critical")

        return verified

    # ------------------------------------------------------------------
    # watch — autonomous continuous monitoring loop
    # ------------------------------------------------------------------

    def watch(self, interval_minutes: int = 5, auto_fix: bool = True,
              use_slack: bool = True, on_tick=None) -> None:
        """
        Runs forever until Ctrl+C (KeyboardInterrupt propagates to caller).
        `on_tick(message: str)` is called with a human-readable line after
        every event, so the CLI can render a live log without this method
        knowing about Rich/console.
        """
        def _log(msg: str) -> None:
            if on_tick:
                on_tick(msg)

        while True:
            report = self.scan()
            if not report.diagnoses:
                _log("Scan complete. All jobs passing.")
            for diagnosis in report.diagnoses:
                if auto_fix and _is_auto_fixable(diagnosis):
                    _log(f"{diagnosis.job_name} FAILED ({diagnosis.problem_type.value}). Auto-fixing...")
                    fix_result = self.apply_fix(diagnosis, confirmed=True)
                    verified = self.verify_fix(diagnosis, fix_result) if fix_result.success else False
                    if verified:
                        _log(f"{diagnosis.job_name} recovered. Auto-healed.")
                        if use_slack:
                            self._alert(f"Auto-fixed: {diagnosis.job_name} — {diagnosis.root_cause}", "info")
                    else:
                        _log(f"Auto-fix failed for {diagnosis.job_name} — escalated to manual.")
                        if use_slack:
                            self._alert(f"ALERT: Auto-fix failed: {diagnosis.job_name}", "critical")
                else:
                    _log(f"NEEDS ATTENTION: {diagnosis.job_name} — {diagnosis.root_cause}")
                    if use_slack:
                        self._alert(f"NEEDS ATTENTION: {diagnosis.job_name} — {diagnosis.root_cause}", "warning")

            time.sleep(interval_minutes * 60)

    def _alert(self, message: str, severity: str) -> None:
        try:
            from agent.integrations.slack import send_alert_generic
            send_alert_generic(title="Jenkins", message=message, severity=severity)
        except Exception as exc:
            log.warning("jenkins.alert.error", error=str(exc))

    # ------------------------------------------------------------------
    # get_health_score
    # ------------------------------------------------------------------

    def get_health_score(self, report: JenkinsScanReport) -> int:
        score = 100
        score -= 10 * report.failing_jobs
        score -= 15 * report.offline_nodes
        score -= 5 * report.stuck_queue_items
        score -= 3 * sum(1 for d in report.diagnoses if d.problem_type == JenkinsProblemType.FLAKY_TEST)
        return max(0, score)

    # ------------------------------------------------------------------
    # detect_patterns — weekly systemic analysis
    # ------------------------------------------------------------------

    def detect_patterns(self, days: int = 30) -> list[JenkinsPattern]:
        log.info("jenkins.detect_patterns.start", days=days)
        incidents = retrieve_context("jenkins failure incident", limit=50)
        cutoff = datetime.utcnow() - timedelta(days=days)
        recent = [m for m in incidents if m.created_at >= cutoff]

        if not recent:
            return []

        incidents_text = "\n".join(f"  - {m.content}" for m in recent)
        response = asyncio.run(llm.chat(
            messages=[context.user_message(f"Incidents (last {days} days):\n{incidents_text}")],
            system=_PATTERNS_SYSTEM,
            json_mode=True,
            max_tokens=1500,
        ))
        parsed = parse_llm_json(response.content)
        patterns = [JenkinsPattern(**p) for p in parsed.get("patterns", [])]

        for p in patterns:
            remember(
                content=f"Recurring Jenkins pattern: {p.title}. Cause: {p.likely_cause}. "
                        f"Recommendation: {p.recommendation}",
                source="jenkins-pattern",
                metadata={"affected_jobs": p.affected_jobs},
            )

        log.info("jenkins.detect_patterns.done", patterns=len(patterns))
        return patterns
