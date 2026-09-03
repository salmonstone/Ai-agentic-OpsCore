"""
Log Analysis Skill — fetch pod logs and AI root-cause analysis.

Flow:
  1. get_pod_logs_smart()   → current + previous (crash) logs per container
  2. get_container_states() → exit codes, OOMKilled, restart counts
  3. extract_error_patterns()→ pre-highlight smoking-gun lines
  4. Claude (JSON mode)      → root_cause, error_type, fix_command
  5. memory.remember()       → save for future cross-referencing
"""
from __future__ import annotations

import json
import re

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import ContainerState, LogAnalysis, PodLogs
from agent.integrations.kubectl import (
    extract_error_patterns,
    get_container_states,
    get_pod_logs_smart,
    get_problematic_pods,
)
from agent.memory.retrieval import remember, retrieve_context
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_LOG_SYSTEM = """\
You are an expert SRE performing root cause analysis on a failing Kubernetes pod.

You are given:
- Container states (exit codes, restart counts, termination reasons)
- Current logs (what the container printed before dying)
- Previous logs (the ACTUAL CRASH that caused the restart — usually the smoking gun)
- Pre-extracted error patterns (lines matching known error signatures)
- Past similar incidents from memory

IMPORTANT:
- The PREVIOUS logs show the crash that caused the restart loop — analyse these first
- Exit code 137 = OOMKilled (kernel killed the process for using too much memory)
- Exit code 1 = Application error (check logs for exception/panic)
- Exit code 139 = Segfault

Return ONLY valid JSON — no markdown, no explanation outside JSON:
{
  "root_cause": "one clear sentence quoting the exact error",
  "error_type": "OOM|CONFIG|NETWORK|CRASH|PERMISSION|UNKNOWN",
  "confidence": "high|medium|low",
  "key_log_lines": ["the 2-4 most important log lines that prove the diagnosis"],
  "explanation": "clear explanation a junior engineer would understand (3-5 sentences)",
  "suggested_fix": "what to do to fix this",
  "fix_command": "exact kubectl command or null",
  "related_to_restart": true|false
}"""


class LogAnalysisSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "log-analysis"

    @property
    def description(self) -> str:
        return "Fetch pod logs and identify the root cause of failures using AI."

    def execute(self, input_data: dict) -> dict:
        analysis = self.analyze_logs(
            input_data["pod"],
            input_data["namespace"],
            input_data.get("lines", 200),
        )
        return analysis.model_dump()

    def analyze_logs(
        self,
        pod: str,
        namespace: str,
        lines: int = 200,
        container: str | None = None,
    ) -> LogAnalysis:
        # Fetch everything in parallel would be nice but these depend on the same pod JSON;
        # pod JSON is fetched once inside get_pod_logs_smart so keep sequential.
        pod_logs   = get_pod_logs_smart(pod, namespace, lines, container)
        con_states = get_container_states(pod, namespace)

        # Extract error patterns from all available logs
        all_log_text = "\n".join(list(pod_logs.current_logs.values()) +
                                  list(pod_logs.previous_logs.values()))
        patterns = extract_error_patterns(all_log_text)

        # Memory: past similar incidents
        past = ""
        try:
            past_mems = retrieve_context(f"pod logs error {pod}")
            if past_mems:
                past = "\n".join(m.content[:200] for m in past_mems[:3])
        except Exception:
            pass

        # If no logs at all, return early with what we know
        if not all_log_text.strip() and not pod_logs.fetch_errors:
            analysis = LogAnalysis(
                pod=pod, namespace=namespace,
                root_cause="No logs available — pod may not have started yet",
                error_type="UNKNOWN", confidence="low",
                explanation="\n".join(pod_logs.fetch_errors) or "Pod has not produced any logs yet.",
            )
            return analysis

        analysis = self._call_claude(pod, namespace, pod_logs, con_states, patterns, past)

        remember(
            content=(
                f"Log analysis for {namespace}/{pod}: "
                f"error_type={analysis.error_type} confidence={analysis.confidence} "
                f"root_cause={analysis.root_cause} fix={analysis.suggested_fix}"
            ),
            source="log-analysis",
            metadata={"pod": pod, "namespace": namespace, "error_type": analysis.error_type},
        )
        log.info("logs.analysis_done", pod=pod, namespace=namespace,
                 error_type=analysis.error_type, confidence=analysis.confidence)
        return analysis

    def _call_claude(
        self,
        pod: str,
        namespace: str,
        pod_logs: PodLogs,
        states: list[ContainerState],
        patterns: list[dict],
        past_incidents: str,
    ) -> LogAnalysis:
        # Build prompt
        parts: list[str] = [f"=== POD: {namespace}/{pod} ===\n"]

        # Container states
        parts.append("--- CONTAINER STATES ---")
        for s in states:
            exit_info = f" exit_code={s.exit_code}" if s.exit_code is not None else ""
            reason    = f" reason={s.reason}" if s.reason else ""
            parts.append(
                f"  {s.name}: state={s.state} ready={s.ready} "
                f"restarts={s.restart_count}{exit_info}{reason}"
            )

        # Previous logs FIRST (smoking gun)
        if pod_logs.previous_logs:
            parts.append("\n--- PREVIOUS LOGS (THE CRASH — ANALYSE FIRST) ---")
            for cname, logs in pod_logs.previous_logs.items():
                parts.append(f"[container: {cname}]")
                parts.append(logs[-3000:])   # last 3000 chars = tail of crash

        # Current logs
        if pod_logs.current_logs:
            parts.append("\n--- CURRENT LOGS ---")
            for cname, logs in pod_logs.current_logs.items():
                parts.append(f"[container: {cname}]")
                parts.append(logs[-2000:])

        # Pre-extracted patterns
        if patterns:
            parts.append("\n--- PRE-EXTRACTED ERROR PATTERNS ---")
            for p in patterns[:15]:
                parts.append(f"  [{p['pattern_type']}] line {p['line_no']}: {p['line']}")

        # Fetch errors
        if pod_logs.fetch_errors:
            parts.append("\n--- FETCH NOTES ---")
            for e in pod_logs.fetch_errors:
                parts.append(f"  {e}")

        # Past incidents
        if past_incidents.strip():
            parts.append("\n--- PAST SIMILAR INCIDENTS ---")
            parts.append(past_incidents[:500])

        user_text = "\n".join(parts)

        try:
            resp = run_sync(llm.chat(
                messages=[context.user_message(user_text)],
                system=_LOG_SYSTEM,
                json_mode=True,
                max_tokens=800,
            ))
            # Strip markdown fences if model wrapped the JSON
            text = resp.content.strip()
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```\s*$', '', text).strip()
            raw = json.loads(text)
        except Exception as exc:
            log.warning("logs.claude_failed", error=str(exc))
            # Fall back to pattern-based analysis
            return self._pattern_fallback(pod, namespace, patterns, states)

        return LogAnalysis(
            pod=pod,
            namespace=namespace,
            root_cause=raw.get("root_cause", "Unknown"),
            error_type=raw.get("error_type", "UNKNOWN").upper(),
            confidence=raw.get("confidence", "low"),
            key_log_lines=raw.get("key_log_lines", []),
            explanation=raw.get("explanation", ""),
            suggested_fix=raw.get("suggested_fix", ""),
            fix_command=raw.get("fix_command") or None,
            related_to_restart=raw.get("related_to_restart", False),
            claude_full_analysis=resp.content,
        )

    def _pattern_fallback(
        self,
        pod: str,
        namespace: str,
        patterns: list[dict],
        states: list[ContainerState],
    ) -> LogAnalysis:
        """Rule-based fallback when Claude is unavailable."""
        # Check exit codes first
        for s in states:
            if s.exit_code == 137 or (s.reason and "oom" in s.reason.lower()):
                return LogAnalysis(
                    pod=pod, namespace=namespace,
                    root_cause="Container OOMKilled — exceeded memory limit",
                    error_type="OOM", confidence="high",
                    suggested_fix="Increase memory limit",
                    fix_command=f"kubectl set resources deployment/{pod} -n {namespace} --limits=memory=1Gi",
                )

        # Check patterns
        if patterns:
            top = patterns[0]
            return LogAnalysis(
                pod=pod, namespace=namespace,
                root_cause=top["line"],
                error_type=top["pattern_type"], confidence="medium",
                key_log_lines=[p["line"] for p in patterns[:3]],
                suggested_fix="Check logs for more context",
            )

        return LogAnalysis(pod=pod, namespace=namespace, error_type="UNKNOWN", confidence="low")

    def analyze_namespace_logs(self, namespace: str) -> list[LogAnalysis]:
        """Analyze logs for all problematic pods in a namespace."""
        bad_pods = [
            p for p in get_problematic_pods()
            if namespace == "all" or p.namespace == namespace
        ]
        results: list[LogAnalysis] = []
        for pod in bad_pods[:10]:   # cap at 10 to avoid runaway API calls
            try:
                results.append(self.analyze_logs(pod.name, pod.namespace))
            except Exception as exc:
                log.warning("logs.namespace_skip", pod=pod.name, error=str(exc))
        return results
