"""
IncidentCorrelationSkill — turns scattered signals from every existing data
source into ONE incident with a real causal timeline, instead of treating
each symptom (a deploy, a restart, a resource alert) as its own disconnected
event.

Design principle: reuse every existing data source as-is — deploy_db,
daemon_db, the memory store (which already spans every skill uniformly:
jenkins-scan, security-audit, resource-monitor, k8s-diagnose, ingress-scan,
cost-aws, ...), and incident_db's existing incidents/incident_events schema
(already shaped for "one incident, many timestamped events"). Nothing new is
instrumented; this only reads what already gets written, and writes into a
schema that already supports the output shape.

Deterministic code does the gathering and time-window filtering (cheap, no
API cost). Claude is used for exactly one thing: reasoning about whether the
gathered signals represent one linked incident, and if so, what the likely
causal chain and root cause are — the "correlation ≠ causation" judgment
call a simple time-window join can't make safely on its own.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from agent.core import context, llm
from agent.core.models import CorrelatedIncident, TimelineEvent
from agent.core.parsing import parse_llm_json
from agent.integrations import daemon_db, deploy_db, incident_db
from agent.memory.store import get_recent
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill
from agent.skills.incident import open_incident as _open_incident

log = get_logger(__name__)

_SYSTEM = """\
You are a senior SRE doing incident correlation — the same judgment call an
experienced on-call engineer makes when several alerts fire close together:
are these ONE incident, or unrelated coincidences?

You will receive a chronological list of signals gathered from independent
systems (deployments, autonomous daemon actions, scan/diagnosis results from
every monitoring skill, and any already-open incidents) within one time
window.

Decide:
1. is_incident: true only if the signals form a plausible CAUSAL chain, not
   just temporal proximity. Two unrelated things happening minutes apart is
   NOT one incident — weigh whether the affected resource/service/namespace
   actually overlaps between signals, not just the timing.
2. confidence: "high" only if the causal chain is well-evidenced (e.g. a
   deploy to the exact same deployment that then shows errors/restarts).
   "medium" if plausible but circumstantial. "low" if you are guessing.
3. root_cause: the single most likely underlying cause, stated concretely
   (not "something went wrong" — name the specific mechanism if evident).
4. contributing_factors: anything that made it worse or is worth fixing
   even if not the root cause.
5. primary_service / namespace: the resource most central to the incident.
6. severity: "critical" | "warning" | "info".
7. timeline: reorder the signals into the causal chain you identified, each
   with a short event_type (e.g. "deploy", "error_rate_spike", "pod_restart",
   "memory_increase", "latency_increase") and a one-sentence detail.
8. title: one sentence, e.g. "Deployment at 14:32 caused API latency spike
   via memory-limit pod restarts."

If there is not enough evidence to call this a real correlated incident,
set is_incident=false and explain why in root_cause instead (e.g. "signals
are unrelated in time and resource scope").

Return JSON only:
{
  "is_incident": true|false, "confidence": "high"|"medium"|"low",
  "title": "...", "root_cause": "...", "contributing_factors": ["..."],
  "primary_service": "...", "namespace": "...", "severity": "...",
  "timeline": [{"event_type": "...", "detail": "...", "timestamp": "..."}]
}"""


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


class IncidentCorrelationSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "incident-correlation"

    @property
    def description(self) -> str:
        return (
            "Correlate deploys, autonomous actions, and every skill's memory "
            "within a time window into one incident with a causal timeline, "
            "instead of treating each symptom as its own disconnected alert."
        )

    def execute(self, input_data: dict) -> dict:
        minutes = input_data.get("minutes", 30)
        return self.correlate(minutes).model_dump()

    # ------------------------------------------------------------------
    # correlate
    # ------------------------------------------------------------------

    def correlate(self, minutes: int = 30) -> CorrelatedIncident:
        log.info("incident_correlation.start", minutes=minutes)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=minutes)

        # ── Gather from every existing source, no new instrumentation ──
        deploys = [
            d for d in deploy_db.list_reports(limit=30)
            if (ts := _parse_iso(d.timestamp)) and ts >= cutoff
        ]
        actions_raw = daemon_db.list_actions(since_hours=max(1, -(-minutes // 60)), limit=100)
        actions = [
            a for a in actions_raw
            if (ts := _parse_iso(a.get("timestamp", ""))) and ts >= cutoff
        ]
        memories = [m for m in get_recent(limit=150) if m.created_at.replace(tzinfo=timezone.utc) >= cutoff]
        open_incidents = incident_db.list_incidents(status="open", since_hours=max(1, -(-minutes // 60)))

        signal_counts = {
            "deploys": len(deploys), "daemon_actions": len(actions),
            "memory_entries": len(memories), "open_incidents": len(open_incidents),
        }
        total_signals = sum(signal_counts.values())

        if total_signals == 0:
            log.info("incident_correlation.no_signal", minutes=minutes)
            return CorrelatedIncident(
                window_minutes=minutes, has_signal=False,
                summary=f"No signals from any source in the last {minutes} minutes — nothing to correlate.",
                signal_counts=signal_counts,
            )

        # ── Build one chronological prompt — deterministic assembly, no AI yet ──
        lines: list[str] = []
        for d in deploys:
            lines.append(
                f"[{d.timestamp}] DEPLOY: {d.deployment}/{d.namespace} "
                f"{d.old_image}->{d.new_image} status={d.status} "
                f"errors_detected={d.errors_detected} rollback={d.rollback_triggered} "
                f"summary={d.claude_summary[:150]}"
            )
        for a in actions:
            lines.append(
                f"[{a.get('timestamp','')}] DAEMON_ACTION: {a.get('category','')}/{a.get('action','')} "
                f"resource={a.get('resource','')} namespace={a.get('namespace','')} "
                f"success={a.get('success')} note={str(a.get('note',''))[:150]}"
            )
        for m in memories:
            lines.append(f"[{m.created_at.isoformat()}] {m.source.upper()}: {m.content[:200]}")
        for inc in open_incidents:
            lines.append(
                f"[{inc.get('opened_at','')}] OPEN_INCIDENT: {inc.get('title','')} "
                f"service={inc.get('service','')} namespace={inc.get('namespace','')} "
                f"severity={inc.get('severity','')}"
            )
        lines.sort()  # chronological, since every line starts with an ISO [timestamp]

        user_text = (
            f"Time window: last {minutes} minutes (now={now.isoformat()}).\n"
            f"Signals gathered: {signal_counts}\n\n"
            "--- CHRONOLOGICAL SIGNALS ---\n" + "\n".join(lines)
        )

        response = asyncio.run(llm.chat(
            messages=[context.user_message(user_text)],
            system=_SYSTEM,
            json_mode=True,
            max_tokens=1500,
        ))
        parsed = parse_llm_json(response.content)

        timeline = [
            TimelineEvent(
                timestamp=ev.get("timestamp", ""), source="correlation",
                event_type=ev.get("event_type", "signal"), detail=ev.get("detail", ""),
            )
            for ev in parsed.get("timeline", [])
        ]

        result = CorrelatedIncident(
            window_minutes=minutes,
            has_signal=True,
            is_incident=bool(parsed.get("is_incident", False)),
            confidence=parsed.get("confidence", "low"),
            title=parsed.get("title", ""),
            root_cause=parsed.get("root_cause", ""),
            contributing_factors=parsed.get("contributing_factors", []) or [],
            primary_service=parsed.get("primary_service", ""),
            namespace=parsed.get("namespace", ""),
            severity=parsed.get("severity", "warning"),
            timeline=timeline,
            signal_counts=signal_counts,
            summary=parsed.get("title", "") or "Correlation complete.",
        )

        # Only ever open/reuse a real incident for high/medium confidence —
        # a low-confidence guess should not create noise in incident_db.
        if result.is_incident and result.confidence in ("high", "medium"):
            incident_id = _open_incident(
                title=result.title or "Correlated incident",
                severity=result.severity,
                service=result.primary_service,
                namespace=result.namespace,
                cause=result.root_cause,
            )
            for ev in timeline:
                incident_db.add_event(incident_id, ev.event_type, detail=ev.detail)
            result.incident_id = incident_id

        log.info("incident_correlation.done", is_incident=result.is_incident,
                 confidence=result.confidence, incident_id=result.incident_id,
                 signal_counts=signal_counts)
        return result
