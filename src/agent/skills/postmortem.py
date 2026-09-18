"""
Postmortem skill — compile a written incident report from data every other
skill already produced.

This is deliberately the cheapest skill in the whole system to build, and
that is the point of building it now: it adds no new evidence source of its
own. It reads `incident_db`'s timeline (open/resolve/escalate events) and
whatever `change.py`, `topology.py`, `metrics.py`, `request_path.py`,
`k8s.py` and `jenkins.py` wrote to memory during the incident's time window,
merges them into one chronological account, and asks Claude to write the
narrative sections a human would otherwise type by hand at 2am after the page
finally stops.

Why this matters despite being "just" a compiler: postmortems are the
single most commonly-skipped SRE practice, precisely because writing one
after an incident is tedious and the evidence is scattered across five tools
by the time anyone sits down to do it. If a postmortem costs one command
instead of twenty minutes of memory reconstruction, it actually gets written
— and a system that has diagnosed hundreds of incidents but never learns
from written history is missing the whole point of `incident_correlation.py`
existing in the first place.

Design mirrors change.py exactly: gather from every source independently (one
broken source must never blank the others), and the deterministic parts
— duration, MTTR context, the timeline itself — stand without Claude. The
LLM's job is narrower than in any other skill here: turn already-assembled
facts into prose. It never invents a root cause the incident record doesn't
support; when the evidence is thin, it says so instead of fabricating.
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import (
    PostmortemActionItem, PostmortemReport, PostmortemTimelineEntry,
    TimelineEntryKind,
)
from agent.core.parsing import LLMParseError, parse_llm_json
from agent.memory.retrieval import remember
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SYSTEM = """You are an SRE writing a blameless postmortem from an assembled
incident timeline.

Everything in the timeline below is a FACT pulled from real systems — the
incident tracker and other diagnostic skills. Do not invent evidence that
isn't there, and do not name a root cause the timeline doesn't support.

Return JSON only with this exact structure:
{
  "summary": "string — one paragraph: what happened, in plain language",
  "root_cause": "string — the technical cause, or 'not conclusively established from available evidence'",
  "impact": "string — who/what was affected and for how long",
  "what_went_well": "string — one or two sentences, or empty if nothing stands out",
  "what_went_wrong": "string — one or two sentences on the response itself, or empty",
  "action_items": [
    {"title": "string", "rationale": "string", "owner_hint": "string or empty", "priority": "high|medium|low"}
  ]
}

Rules:
- This is BLAMELESS. Refer to systems and processes, never to a person by name.
- If the timeline has no diagnosis entries (no change-correlation, no
  workload/metrics findings), say root_cause is not conclusively established
  rather than guessing from the incident title alone.
- action_items must be concrete and verifiable (e.g. "add a PodDisruptionBudget
  to X"), never generic advice like "improve monitoring".
- Limit action_items to at most 5, ranked by priority.
- what_went_well is not filler — if the auto-healer resolved this with no
  human paged, say so; that is a real success worth recording.
"""

# Sources that represent a DIAGNOSIS worth surfacing in the timeline, mapped
# to a short label. Anything from remember() with a source not in this map is
# still collected (as a NOTE) but not treated as a confirmed finding.
_DIAGNOSIS_SOURCES = {
    "change-correlation": "Change correlation",
    "topology":            "Blast radius",
    "metrics":             "Metrics anomaly",
    "metrics-verify":      "Recovery check",
    "request-path":        "Request-path trace",
    "workloads":           "Workload scan",
    "k8s-diagnose":        "Pod diagnosis",
    "jenkins":             "Jenkins diagnosis",
    "security":            "Security finding",
}


def _ts_of(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class PostmortemSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "postmortem"

    @property
    def description(self) -> str:
        return ("Compile a written postmortem from an incident's timeline and "
                "whatever other skills diagnosed during its window.")

    def execute(self, input_data: dict) -> dict:
        return self.generate(input_data["incident_id"]).model_dump()

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    def generate(self, incident_id: str, use_llm: bool = True) -> PostmortemReport:
        """Build the full report for one incident."""
        log.info("postmortem.generate.start", incident_id=incident_id)

        try:
            from agent.integrations.incident_db import get_incident
            incident = get_incident(incident_id)
        except Exception as e:
            log.warning("postmortem.incident_db_failed", error=str(e)[:200])
            incident = None

        if incident is None:
            return PostmortemReport(
                incident_id=incident_id, title="(incident not found)",
                degraded=True,
                degraded_reason=(f"No incident with id {incident_id!r}. Run "
                                 f"`agent incident list` to see valid ids."),
                generated_at=datetime.now(timezone.utc).isoformat(),
            )

        opened_ts = _ts_of(incident.get("opened_at", ""))
        resolved_ts = _ts_of(incident.get("resolved_at", "")) or datetime.now(timezone.utc).timestamp()
        duration_min = max(0.0, (resolved_ts - opened_ts) / 60.0)

        report = PostmortemReport(
            incident_id=incident_id,
            title=incident.get("title", "(untitled incident)"),
            severity=incident.get("severity", ""),
            service=incident.get("service", ""),
            namespace=incident.get("namespace", ""),
            opened_at=incident.get("opened_at", ""),
            resolved_at=incident.get("resolved_at", "") or "",
            duration_minutes=round(duration_min, 1),
            status=incident.get("status", ""),
            auto_fixed=bool(incident.get("auto_fixed")),
            fix_attempts=int(incident.get("fix_attempts", 0) or 0),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        timeline: list[PostmortemTimelineEntry] = []
        timeline.extend(self._from_incident_events(incident))
        timeline.extend(self._from_memory(incident, opened_ts, resolved_ts, report))
        timeline.sort(key=lambda e: e.ts)
        report.timeline = timeline

        if not timeline:
            report.degraded = True
            report.degraded_reason = (
                "No timeline entries found beyond the open/resolve events themselves. "
                "Either nothing else diagnosed this incident, or it predates memory retention."
            )

        if use_llm:
            self._narrate(report)
        else:
            self._fallback_narrative(report)

        report.markdown = self._render_markdown(report)
        self._remember(report)
        log.info("postmortem.generate.done", incident_id=incident_id,
                 timeline_entries=len(timeline), degraded=report.degraded)
        return report

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    def _from_incident_events(self, incident: dict) -> list[PostmortemTimelineEntry]:
        out = [PostmortemTimelineEntry(
            at=incident.get("opened_at", ""), ts=_ts_of(incident.get("opened_at", "")),
            kind=TimelineEntryKind.INCIDENT, source="incident_db",
            summary=f"Incident opened: {incident.get('title', '')} ({incident.get('severity', '')})",
            detail=incident.get("cause", "") or "",
        )]
        for ev in incident.get("events", []):
            out.append(PostmortemTimelineEntry(
                at=ev.get("timestamp", ""), ts=_ts_of(ev.get("timestamp", "")),
                kind=(TimelineEntryKind.FIX if "fix" in (ev.get("event_type") or "").lower()
                      else TimelineEntryKind.NOTE),
                source="incident_db",
                summary=f"{ev.get('event_type', 'event')}: {(ev.get('detail') or '')[:200]}",
            ))
        if incident.get("resolved_at"):
            out.append(PostmortemTimelineEntry(
                at=incident["resolved_at"], ts=_ts_of(incident["resolved_at"]),
                kind=TimelineEntryKind.INCIDENT, source="incident_db",
                summary=(f"Incident resolved"
                         + (" (auto-fixed)" if incident.get("auto_fixed") else "")),
            ))
        return out

    def _from_memory(self, incident: dict, opened_ts: float, resolved_ts: float,
                     report: PostmortemReport) -> list[PostmortemTimelineEntry]:
        """Pull in whatever other skills recorded during the incident window.

        Widened by 15 minutes on each side: the diagnosis that explains an
        incident often runs a few minutes before the symptom crossed whatever
        threshold opened it, and a verification check often lands a few
        minutes after resolution. A tight window would silently drop both.
        """
        try:
            from agent.memory.retrieval import get_memories_by_source
        except Exception:
            return []

        pad = 15 * 60
        lo, hi = opened_ts - pad, resolved_ts + pad
        out: list[PostmortemTimelineEntry] = []
        sources_seen: set[str] = set()

        for source, label in _DIAGNOSIS_SOURCES.items():
            try:
                memories = get_memories_by_source(source)
            except Exception as e:
                log.debug("postmortem.memory_source_failed", source=source, error=str(e)[:150])
                continue

            for m in memories:
                ts = m.created_at.replace(tzinfo=timezone.utc).timestamp() \
                    if m.created_at.tzinfo is None else m.created_at.timestamp()
                if not (lo <= ts <= hi):
                    continue
                # A namespace-scoped memory outside this incident's namespace
                # is almost always about something else happening concurrently.
                meta_ns = m.metadata.get("namespace", "")
                if report.namespace and meta_ns and meta_ns != report.namespace:
                    continue

                sources_seen.add(label)
                out.append(PostmortemTimelineEntry(
                    at=m.created_at.isoformat(), ts=ts,
                    kind=TimelineEntryKind.DIAGNOSIS, source=label,
                    summary=m.content[:300],
                ))
                if source == "change-correlation" and not report.prime_suspect:
                    report.prime_suspect = m.content[:300]

        report.evidence_sources = sorted(sources_seen)
        return out

    # ------------------------------------------------------------------
    # Narrative
    # ------------------------------------------------------------------

    def _narrate(self, report: PostmortemReport) -> None:
        lines = [
            f"Incident: {report.title}",
            f"Severity: {report.severity}  Service: {report.service or '?'}  "
            f"Namespace: {report.namespace or '?'}",
            f"Opened: {report.opened_at}  Resolved: {report.resolved_at or '(still open)'}  "
            f"Duration: {report.duration_minutes:.1f} minutes",
            f"Auto-fixed: {report.auto_fixed}  Fix attempts: {report.fix_attempts}",
            "",
            "TIMELINE (chronological, all entries are recorded facts):",
        ]
        for e in report.timeline:
            lines.append(f"  [{e.at[:19]}] ({e.kind.value}/{e.source}) {e.summary}")
            if e.detail:
                lines.append(f"       {e.detail[:200]}")

        try:
            response = run_sync(llm.chat(
                messages=[context.user_message("\n".join(lines))],
                system=_SYSTEM, json_mode=True, max_tokens=1200,
            ))
            parsed = parse_llm_json(response.content)
        except LLMParseError as exc:
            log.warning("postmortem.narrate.parse_failed", error=str(exc.cause))
            self._fallback_narrative(report)
            return
        except Exception as exc:
            log.warning("postmortem.narrate.failed", error=str(exc)[:200])
            self._fallback_narrative(report)
            return

        report.summary = parsed.get("summary", "")
        report.root_cause = parsed.get("root_cause", "not conclusively established from available evidence")
        report.impact = parsed.get("impact", "")
        report.what_went_well = parsed.get("what_went_well", "")
        report.what_went_wrong = parsed.get("what_went_wrong", "")
        report.action_items = [
            PostmortemActionItem(
                title=a.get("title", ""), rationale=a.get("rationale", ""),
                owner_hint=a.get("owner_hint", ""), priority=a.get("priority", "medium"),
            )
            for a in (parsed.get("action_items") or [])[:5]
        ]

    def _fallback_narrative(self, report: PostmortemReport) -> None:
        """The deterministic floor — a usable report even with no LLM.

        Everything here comes straight from the assembled facts, so it is
        never wrong, only less readable than the narrated version.
        """
        report.summary = (
            f"{report.title} ({report.severity}) affecting {report.service or 'an unspecified service'} "
            f"in {report.namespace or 'an unspecified namespace'}, lasting "
            f"{report.duration_minutes:.1f} minutes. {len(report.timeline)} timeline "
            f"entries recorded from {len(report.evidence_sources)} source(s): "
            f"{', '.join(report.evidence_sources) or 'incident tracker only'}."
        )
        report.root_cause = (report.prime_suspect or
                             "Not conclusively established — AI narration unavailable "
                             "and no change-correlation entry was found in this window.")
        report.impact = f"Duration: {report.duration_minutes:.1f} minutes."
        if report.auto_fixed:
            report.what_went_well = "Resolved automatically with no human intervention required."
        report.action_items = []

    # ------------------------------------------------------------------
    # Markdown rendering
    # ------------------------------------------------------------------

    def _render_markdown(self, report: PostmortemReport) -> str:
        lines = [
            f"# Postmortem: {report.title}",
            "",
            f"| | |",
            f"|---|---|",
            f"| Severity | {report.severity} |",
            f"| Service | {report.service or '—'} |",
            f"| Namespace | {report.namespace or '—'} |",
            f"| Opened | {report.opened_at} |",
            f"| Resolved | {report.resolved_at or '(still open)'} |",
            f"| Duration | {report.duration_minutes:.1f} minutes |",
            f"| Auto-fixed | {'yes' if report.auto_fixed else 'no'} |",
            f"| Evidence sources | {', '.join(report.evidence_sources) or 'incident tracker only'} |",
            "",
            "## Summary", "", report.summary or "_(none)_", "",
            "## Root cause", "", report.root_cause or "_(none)_", "",
            "## Impact", "", report.impact or "_(none)_", "",
        ]
        if report.what_went_well:
            lines += ["## What went well", "", report.what_went_well, ""]
        if report.what_went_wrong:
            lines += ["## What went wrong", "", report.what_went_wrong, ""]
        if report.action_items:
            lines += ["## Action items", ""]
            for a in report.action_items:
                lines.append(f"- **[{a.priority}]** {a.title} — {a.rationale}"
                            + (f" _(owner: {a.owner_hint})_" if a.owner_hint else ""))
            lines.append("")
        lines += ["## Timeline", ""]
        for e in report.timeline:
            lines.append(f"- `{e.at[:19]}` **{e.source}** — {e.summary}")
        if report.degraded:
            lines += ["", f"> ⚠️ {report.degraded_reason}"]
        return "\n".join(lines)

    def _remember(self, report: PostmortemReport) -> None:
        remember(
            content=(f"Postmortem generated for '{report.title}' "
                     f"({report.incident_id}): {report.root_cause[:200]}"),
            source="postmortem",
            metadata={"incident_id": report.incident_id, "severity": report.severity,
                      "service": report.service, "namespace": report.namespace,
                      "duration_minutes": report.duration_minutes,
                      "action_item_count": len(report.action_items)},
        )
