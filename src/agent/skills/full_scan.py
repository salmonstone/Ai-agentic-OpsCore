"""
Full cluster scan skill — smart one-Claude-call strategy.

Flow:
  1. Run ALL collectors in parallel (free kubectl, ~10-15s)
  2. Send combined summary to Claude (1 API call ~$0.01)
  3. For each CRITICAL issue: deep-dive with logs/YAML (1 call each)
  4. Return FullScanReport with everything
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

from agent.core import context, llm
from agent.core.models import FullScanReport, ScanIssue
from agent.integrations.collectors import collect_all
from agent.memory.retrieval import remember
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# System prompt — one call, all areas
# ---------------------------------------------------------------------------

_ANALYZE_SYSTEM = """You are a senior Kubernetes SRE performing a full cluster health audit.
You will receive a summary of all cluster areas collected in parallel.
Return a prioritised list of real issues only — do not invent issues not present in the data.

Return JSON only:
{
  "issues": [
    {
      "severity": "critical" | "warning" | "info",
      "area": "nodes|dns|network|pvcs|jobs|hpa|ingress|rbac|pods|tls",
      "resource": "<specific resource name>",
      "namespace": "<namespace or empty>",
      "description": "clear one-sentence description of the problem",
      "fix": "what should be done to resolve this",
      "fix_command": "exact kubectl command or null",
      "deep_dive": true | false
    }
  ],
  "analysis": "2-3 sentence executive summary of overall cluster health"
}

severity rules:
- critical: actively broken, causing downtime or data loss risk
- warning:  degraded, will become critical if left alone
- info:     best practice violation, not urgent

deep_dive: set true ONLY if the issue needs pod logs or full YAML to fix properly
fix_command: provide whenever a single kubectl command can fix or investigate the issue"""

_DEEP_DIVE_SYSTEM = """You are a Kubernetes SRE doing a deep-dive on a specific issue.
You have pod logs, events, and the resource YAML.
Return JSON:
{
  "root_cause": "precise technical root cause",
  "fix_command": "exact kubectl command that fixes this, or null",
  "explanation": "one sentence — what to do and why"
}"""


def _parse_json(content: str) -> dict:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text).strip()
    return json.loads(text)


def _build_prompt(collection: dict[str, dict]) -> str:
    """Turn all collector results into one structured prompt."""
    sections = []
    for area, result in collection.items():
        header  = f"=== {area.upper()} ==="
        summary = result.get("summary", "(no data)")
        sections.append(f"{header}\n{summary}")
    return "\n\n".join(sections)


class FullScanSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "full-scan"

    @property
    def description(self) -> str:
        return "Full cluster health audit — all areas, one Claude call, deep-dive on critical issues."

    def execute(self, input_data: dict) -> dict:
        areas  = input_data.get("areas")
        report = self.scan(areas=areas)
        return report.model_dump()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self, areas: list[str] | None = None) -> FullScanReport:
        log.info("full_scan.start", areas=areas or "all")

        # ── Phase 1: parallel collection (free) ───────────────────────────
        collection, elapsed_ms = collect_all(areas)

        ok_areas     = [a for a, r in collection.items() if r.get("ok")]
        failed_areas = [a for a, r in collection.items() if not r.get("ok")]

        report = FullScanReport(
            generated_at  = datetime.now(timezone.utc).isoformat(),
            collection_ms = elapsed_ms,
            areas_scanned = ok_areas,
            areas_failed  = failed_areas,
        )

        if not ok_areas:
            report.analysis = "All collectors failed — check cluster connectivity."
            return report

        # ── Phase 2: one Claude call for all areas ────────────────────────
        prompt = _build_prompt(collection)

        try:
            response = asyncio.run(
                llm.chat(
                    messages=[context.user_message(prompt)],
                    system=_ANALYZE_SYSTEM,
                    json_mode=True,
                    max_tokens=3000,
                )
            )
            parsed = _parse_json(response.content)
            report.issues   = [ScanIssue(**i) for i in parsed.get("issues", [])]
            report.analysis = parsed.get("analysis", "")
            log.info("full_scan.analyze.done", issues=len(report.issues))
        except Exception as exc:
            log.error("full_scan.analyze.error", error=str(exc))
            report.analysis = f"Analysis failed: {exc}"
            return report

        # ── Phase 3: deep-dive on critical issues that need it ────────────
        critical_needing_dive = [
            i for i in report.issues
            if i.severity == "critical" and i.deep_dive
        ]

        if critical_needing_dive:
            log.info("full_scan.deep_dive.start", count=len(critical_needing_dive))
            for issue in critical_needing_dive:
                enriched = self._deep_dive(issue)
                if enriched:
                    issue.fix_command = enriched.get("fix_command") or issue.fix_command
                    issue.fix        = enriched.get("explanation") or issue.fix

        # ── Save to memory ────────────────────────────────────────────────
        critical = sum(1 for i in report.issues if i.severity == "critical")
        warning  = sum(1 for i in report.issues if i.severity == "warning")

        remember(
            content=(
                f"Full scan: {len(ok_areas)} areas checked in {elapsed_ms:.0f}ms. "
                f"{critical} critical, {warning} warnings. {report.analysis}"
            ),
            source="full-scan",
            metadata={
                "critical": critical,
                "warning":  warning,
                "areas":    ok_areas,
                "elapsed_ms": elapsed_ms,
            },
        )

        log.info("full_scan.done", critical=critical, warning=warning, elapsed_ms=elapsed_ms)
        return report

    # ------------------------------------------------------------------
    # Deep dive
    # ------------------------------------------------------------------

    def _deep_dive(self, issue: ScanIssue) -> dict | None:
        from agent.integrations.kubectl import (
            describe_pod, get_pod_events, get_pod_logs, run_kubectl
        )

        lines = [
            f"Issue    : {issue.issue_type if hasattr(issue, 'issue_type') else issue.area}",
            f"Resource : {issue.resource}",
            f"Namespace: {issue.namespace}",
            f"Problem  : {issue.description}",
        ]

        if issue.namespace and issue.resource:
            logs   = get_pod_logs(issue.resource, issue.namespace)
            events = get_pod_events(issue.resource, issue.namespace)
            desc   = describe_pod(issue.resource, issue.namespace)
            lines += [
                f"\n--- LOGS ---\n{logs[:1500]}",
                f"\n--- EVENTS ---\n{events[:600]}",
                f"\n--- DESCRIBE ---\n{desc[:1500]}",
            ]

        try:
            response = asyncio.run(
                llm.chat(
                    messages=[context.user_message("\n".join(lines))],
                    system=_DEEP_DIVE_SYSTEM,
                    json_mode=True,
                    max_tokens=512,
                )
            )
            return _parse_json(response.content)
        except Exception as exc:
            log.warning("full_scan.deep_dive.error", error=str(exc))
            return None
