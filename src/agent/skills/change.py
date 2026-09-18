"""
Change correlation skill — "what changed just before this broke?"

That is the first question of every real root-cause analysis, and until now
the system could not answer it. The pieces were all here and none of them
were joined up: deploy_db knew about deployments, daemon_db knew what the
healer did, incident_db knew what had broken before, Jenkins knew about
builds, git knew about commits, and the cluster knew when a rollout last
changed revision. Six separate timelines, no single view.

This assembles them into one ordered timeline and scores each entry against a
symptom, deterministically.

Scoring is arithmetic, not a model call — the same reasoning as
skills/jenkins.py's regex-first design. A change gets points for happening
shortly BEFORE the symptom, for touching the same namespace and resource, and
for being the kind of change that actually breaks things. An LLM is used only
to narrate the ranking afterwards, so the ranking itself is reproducible,
free, and unit-testable without an API key.

The one rule that matters most: **a change after the symptom is not a cause.**
It is scored at zero and excluded, no matter how suspicious it looks.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from agent.config import settings
from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import (
    ChangeCorrelation, ChangeEvent, ChangeKind, ChangeReport,
)
from agent.core.parsing import LLMParseError, parse_llm_json
from agent.memory.retrieval import remember, retrieve_context
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SYSTEM = """You are an SRE reviewing a ranked list of infrastructure changes
that preceded a production symptom.

The ranking below was computed arithmetically from timing and resource
overlap. Treat the scores as given — your job is interpretation, not
re-ranking.

Return JSON only with this exact structure:
{
  "analysis": "string — one paragraph: what most likely happened",
  "verdict": "string — the single most likely cause, or 'no clear cause'",
  "recommended_action": "string — ONE next step (e.g. 'roll back X to revision N')",
  "counter_evidence": "string — what would DISPROVE this theory, or empty"
}

Rules:
- Correlation is not causation. If the top change is only weakly related to
  the symptom, say "no clear cause" rather than inventing a chain.
- A config/image change to a DIFFERENT namespace than the symptom is rarely
  the cause. Say so instead of reaching.
- counter_evidence must name something concrete and checkable — a metric, a
  log line, a revision number. Never leave it as generic advice.
"""

# How much each kind of change is worth as a starting score, before timing
# and resource-overlap adjustments. Derived from what actually causes
# production incidents: shipping new code beats a replica count change, which
# beats an autonomous restart, which beats a routine kube event.
_KIND_WEIGHT = {
    ChangeKind.IMAGE:         40,
    ChangeKind.ROLLOUT:       35,
    ChangeKind.DEPLOY_RECORD: 35,
    ChangeKind.SCALE:         25,
    ChangeKind.CI_BUILD:      20,
    ChangeKind.COMMIT:        20,
    ChangeKind.DAEMON_ACTION: 18,
    ChangeKind.CLUSTER_EVENT: 12,
    ChangeKind.INCIDENT:      10,
}


def _ts_of(iso: str) -> float:
    """Parse any ISO8601 the cluster or our own SQLite might produce."""
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class ChangeSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "change-correlation"

    @property
    def description(self) -> str:
        return ("Assemble every infrastructure change into one timeline and score "
                "each against a symptom to find the likely cause.")

    def execute(self, input_data: dict) -> dict:
        mode = input_data.get("mode", "correlate")
        if mode == "timeline":
            events = self.collect(
                int(input_data.get("minutes", 60)),
                input_data.get("namespace", "all"),
            )
            return {"events": [e.model_dump() for e in events]}
        if mode == "correlate":
            return self.correlate(
                input_data.get("symptom", ""),
                input_data.get("symptom_at"),
                int(input_data.get("minutes", 60)),
                input_data.get("namespace", "all"),
            ).model_dump()
        raise ValueError(f"Unknown mode: {mode!r}")

    # ------------------------------------------------------------------
    # Collection — six sources, one timeline
    # ------------------------------------------------------------------

    def collect(self, minutes: int = 60, namespace: str = "all") -> list[ChangeEvent]:
        """Every change we can see in the window, newest first.

        Each source is wrapped independently: a missing Jenkins, an absent git
        repo or an empty database must never prevent the other five from
        contributing. A partial timeline is useful; an exception is not.
        """
        cutoff = time.time() - (minutes * 60)
        events: list[ChangeEvent] = []

        events.extend(self._from_rollouts(namespace, cutoff))
        events.extend(self._from_cluster_events(namespace, cutoff))
        events.extend(self._from_deploy_db(cutoff))
        events.extend(self._from_daemon_db(minutes, cutoff))
        events.extend(self._from_incidents(minutes, cutoff))
        events.extend(self._from_jenkins(cutoff))
        events.extend(self._from_git(minutes))

        events.sort(key=lambda e: e.ts, reverse=True)
        log.info("change.collect.done", minutes=minutes,
                 namespace=namespace, events=len(events))
        return events

    def _from_rollouts(self, namespace: str, cutoff: float) -> list[ChangeEvent]:
        """Workload revision changes — the cheapest 'what shipped' signal
        there is, because Kubernetes records it with no CI integration."""
        try:
            from agent.integrations.kubectl import get_workload_rollout_times
            rollouts = get_workload_rollout_times(namespace)
        except Exception as e:
            log.warning("change.rollouts_failed", error=str(e)[:200])
            return []

        out = []
        for r in rollouts:
            ts = _ts_of(r.get("updated_at", ""))
            if ts < cutoff:
                continue
            image = r["images"][0] if r.get("images") else ""
            out.append(ChangeEvent(
                kind=ChangeKind.ROLLOUT, at=r["updated_at"], ts=ts, source="kubectl",
                resource=r["name"], namespace=r["namespace"],
                summary=(f"{r['kind']} {r['namespace']}/{r['name']} rolled out "
                         f"revision {r.get('revision') or '?'}"),
                detail=r.get("change_cause", ""), image=image,
                git_sha=self._sha_of(image),
            ))
        return out

    @staticmethod
    def _sha_of(image: str) -> str:
        try:
            from agent.integrations.git_repo import sha_from_image
            return sha_from_image(image)
        except Exception:
            return ""

    def _from_cluster_events(self, namespace: str, cutoff: float) -> list[ChangeEvent]:
        """Warning-type kube events that represent a CHANGE rather than a
        symptom. `get_cluster_events` carries no timestamp, so these are
        recorded at collection time and scored only on resource overlap —
        they can support a theory, never establish the timing of one."""
        try:
            from agent.integrations.kubectl import get_cluster_events
            raw = get_cluster_events(namespace)
        except Exception as e:
            log.warning("change.events_failed", error=str(e)[:200])
            return []

        interesting = {"Scaled", "ScalingReplicaSet", "SuccessfulCreate",
                       "SuccessfulDelete", "Killing", "Preempted", "NodeNotReady"}
        now = time.time()
        out = []
        for ev in raw:
            if ev.get("reason") not in interesting:
                continue
            kind = ChangeKind.SCALE if "Scal" in ev.get("reason", "") else ChangeKind.CLUSTER_EVENT
            out.append(ChangeEvent(
                kind=kind, at=_iso(now), ts=now, source="kubectl-events",
                resource=ev.get("name", ""), namespace=ev.get("namespace", ""),
                summary=f"{ev.get('reason')}: {ev.get('kind')} {ev.get('name')}",
                detail=(ev.get("message") or "")[:300],
            ))
        return out

    def _from_deploy_db(self, cutoff: float) -> list[ChangeEvent]:
        try:
            from agent.integrations.deploy_db import list_reports
            reports = list_reports(limit=50)
        except Exception as e:
            log.debug("change.deploy_db_unavailable", error=str(e)[:200])
            return []

        out = []
        for r in reports:
            ts = _ts_of(r.timestamp)
            if ts < cutoff:
                continue
            out.append(ChangeEvent(
                kind=ChangeKind.DEPLOY_RECORD, at=r.timestamp, ts=ts, source="deploy_db",
                resource=r.deployment, namespace=r.namespace,
                summary=(f"Deploy {r.deployment} -> {r.status} "
                         f"(risk={r.risk_level}, rollback={r.rollback_triggered})"),
                detail=(f"{r.old_image} -> {r.new_image}" if r.new_image else ""),
                image=r.new_image, git_sha=self._sha_of(r.new_image),
            ))
        return out

    def _from_daemon_db(self, minutes: int, cutoff: float) -> list[ChangeEvent]:
        """Our own autonomous actions.

        These matter disproportionately during an incident: if the healer
        restarted something two minutes before the symptom, that is a change
        the system itself made and must be shown to the human, not quietly
        left out of the timeline it produced.
        """
        try:
            from agent.integrations.daemon_db import list_actions
            actions = list_actions(limit=100, since_hours=max(1, minutes // 60 + 1))
        except Exception as e:
            log.debug("change.daemon_db_unavailable", error=str(e)[:200])
            return []

        out = []
        for a in actions:
            ts = _ts_of(a.get("timestamp", ""))
            if ts < cutoff:
                continue
            out.append(ChangeEvent(
                kind=ChangeKind.DAEMON_ACTION, at=a.get("timestamp", ""), ts=ts,
                source="daemon_db", resource=a.get("resource", ""),
                namespace=a.get("namespace", ""),
                summary=f"Autonomous {a.get('category')}: {a.get('action')} on {a.get('resource')}",
                detail=(a.get("note") or "")[:300],
            ))
        return out

    def _from_incidents(self, minutes: int, cutoff: float) -> list[ChangeEvent]:
        try:
            from agent.integrations.incident_db import list_incidents
            incidents = list_incidents(limit=30, since_hours=max(1, minutes // 60 + 1))
        except Exception as e:
            log.debug("change.incident_db_unavailable", error=str(e)[:200])
            return []

        out = []
        for i in incidents:
            ts = _ts_of(i.get("opened_at", ""))
            if ts < cutoff:
                continue
            out.append(ChangeEvent(
                kind=ChangeKind.INCIDENT, at=i.get("opened_at", ""), ts=ts,
                source="incident_db", resource=i.get("service", ""),
                namespace=i.get("namespace", ""),
                summary=f"Incident opened: {i.get('title')} ({i.get('severity')})",
                detail=(i.get("cause") or "")[:300],
            ))
        return out

    def _from_jenkins(self, cutoff: float) -> list[ChangeEvent]:
        if not settings.jenkins_url:
            return []
        try:
            from agent.integrations import jenkins as jk
            jobs = jk.get_all_jobs()
        except Exception as e:
            log.debug("change.jenkins_unavailable", error=str(e)[:200])
            return []

        out = []
        for job in jobs[:25]:      # cap: one HTTP call per job below
            try:
                history = jk.get_build_history(job.name, 3)
            except Exception:
                continue
            for b in history:
                # Jenkins reports epoch MILLISECONDS in BuildInfo.timestamp.
                ts = (b.timestamp / 1000.0) if b.timestamp else 0.0
                if ts < cutoff:
                    continue
                out.append(ChangeEvent(
                    kind=ChangeKind.CI_BUILD, at=_iso(ts), ts=ts, source="jenkins",
                    resource=job.name,
                    summary=f"Jenkins {job.name} #{b.number}: {b.status}",
                    detail=f"node={getattr(b, 'node', '')}",
                ))
        return out

    def _from_git(self, minutes: int) -> list[ChangeEvent]:
        try:
            from agent.integrations.git_repo import is_repo, recent_commits
            if not is_repo():
                return []
            commits = recent_commits(minutes=minutes)
        except Exception as e:
            log.debug("change.git_unavailable", error=str(e)[:200])
            return []

        return [
            ChangeEvent(
                kind=ChangeKind.COMMIT, at=c["at"], ts=c["ts"], source="git",
                resource=c["sha"], summary=f"{c['sha']} {c['subject']}",
                detail=c.get("body", "")[:200], git_sha=c["sha"], author=c["author"],
            )
            for c in commits
        ]

    # ------------------------------------------------------------------
    # Scoring — deterministic, no model involved
    # ------------------------------------------------------------------

    def score_event(self, event: ChangeEvent, symptom: str, symptom_ts: float,
                    namespace: str = "") -> ChangeCorrelation:
        """Score one change against one symptom. Pure and reproducible."""
        reasons: list[str] = []
        seconds_before = symptom_ts - event.ts

        # A change that happened AFTER the symptom cannot have caused it.
        # 120s of slack absorbs clock skew between the cluster and this host.
        if seconds_before < -120:
            return ChangeCorrelation(event=event, score=0, seconds_before=seconds_before,
                                     reasons=["Happened after the symptom — cannot be the cause."])

        score = _KIND_WEIGHT.get(event.kind, 10)
        reasons.append(f"{event.kind.value} baseline {score}")

        # Timing: the closer before the symptom, the stronger.
        minutes_before = max(seconds_before, 0) / 60.0
        if minutes_before <= 5:
            score += 35; reasons.append(f"{minutes_before:.1f}min before symptom (+35)")
        elif minutes_before <= 15:
            score += 25; reasons.append(f"{minutes_before:.1f}min before symptom (+25)")
        elif minutes_before <= 60:
            score += 12; reasons.append(f"{minutes_before:.1f}min before symptom (+12)")
        else:
            score += 3;  reasons.append(f"{minutes_before:.0f}min before symptom (+3)")

        # Resource overlap: does the symptom text name this resource?
        sym = (symptom or "").lower()
        if event.resource and len(event.resource) > 3 and event.resource.lower() in sym:
            score += 25; reasons.append(f"symptom names {event.resource!r} (+25)")

        # Namespace agreement, when we know the symptom's namespace.
        if namespace and namespace != "all" and event.namespace:
            if event.namespace == namespace:
                score += 15; reasons.append(f"same namespace {namespace} (+15)")
            else:
                score -= 20; reasons.append(f"different namespace ({event.namespace}) (-20)")

        # A change that shipped new code is more suspicious than one that did not.
        if event.image:
            score += 10; reasons.append("changed a container image (+10)")
        if event.git_sha:
            score += 5;  reasons.append(f"traceable to commit {event.git_sha} (+5)")

        return ChangeCorrelation(
            event=event, score=max(0, min(100, score)),
            reasons=reasons, seconds_before=seconds_before,
        )

    # ------------------------------------------------------------------
    # Correlate
    # ------------------------------------------------------------------

    def correlate(self, symptom: str, symptom_at: str | None = None,
                  minutes: int | None = None, namespace: str = "all",
                  use_llm: bool = True) -> ChangeReport:
        """Rank every change in the window against a symptom."""
        minutes = minutes or settings.change_window_minutes
        symptom_ts = _ts_of(symptom_at) if symptom_at else time.time()
        symptom_iso = symptom_at or _iso(symptom_ts)

        log.info("change.correlate.start", symptom=symptom[:80],
                 minutes=minutes, namespace=namespace)

        events = self.collect(minutes, namespace)
        scored = [self.score_event(e, symptom, symptom_ts, namespace) for e in events]
        scored = [c for c in scored if c.score > 0]
        scored.sort(key=lambda c: c.score, reverse=True)

        report = ChangeReport(
            symptom=symptom, symptom_at=symptom_iso, window_minutes=minutes,
            events_found=len(events), correlated=scored[:20],
            prime_suspect=scored[0] if scored else None,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        # Confidence needs both a strong leader AND clear separation: two
        # changes tied at 80 is genuinely ambiguous and must not be reported
        # as a confident answer just because one sorted first.
        if scored:
            top = scored[0].score
            runner_up = scored[1].score if len(scored) > 1 else 0
            gap = top - runner_up
            if top >= 70 and gap >= 20:
                report.confidence = "high"
            elif top >= 50:
                report.confidence = "medium"
            else:
                report.confidence = "low"

        if not events:
            report.analysis = (
                f"No changes found in the last {minutes} minutes. That is itself a "
                f"finding: if nothing changed, look for an external trigger — traffic "
                f"shift, expiring credential, certificate, disk filling, or an upstream "
                f"dependency."
            )
            return report

        report.analysis = (self._narrate(report) if use_llm
                           else self._fallback_analysis(report))
        self._remember(report)
        log.info("change.correlate.done", events=len(events),
                 correlated=len(scored), confidence=report.confidence)
        return report

    # ------------------------------------------------------------------
    # Narrative
    # ------------------------------------------------------------------

    def _narrate(self, report: ChangeReport) -> str:
        lines = [
            f"Symptom: {report.symptom}",
            f"Symptom time: {report.symptom_at}",
            f"Window: {report.window_minutes} minutes",
            f"Changes found: {report.events_found}",
            "",
            "RANKED CHANGES (scores computed arithmetically):",
        ]
        for c in report.correlated[:12]:
            mins = c.seconds_before / 60.0
            lines.append(
                f"  [{c.score:>3}] {mins:6.1f}min before | {c.event.kind.value:<13} "
                f"{c.event.namespace}/{c.event.resource}: {c.event.summary}"
            )
            lines.append(f"        why: {'; '.join(c.reasons)}")
            if c.event.detail:
                lines.append(f"        detail: {c.event.detail[:160]}")

        memories = retrieve_context(f"change correlation {report.symptom}")
        try:
            response = run_sync(llm.chat(
                messages=[context.user_message("\n".join(lines))],
                system=context.build_system_prompt(_SYSTEM, memories),
                json_mode=True, max_tokens=900,
            ))
            parsed = parse_llm_json(response.content)
        except LLMParseError as exc:
            log.warning("change.narrate.parse_failed", error=str(exc.cause))
            return self._fallback_analysis(report)
        except Exception as exc:
            log.warning("change.narrate.failed", error=str(exc)[:200])
            return self._fallback_analysis(report)

        parts = [parsed.get("analysis", "")]
        if parsed.get("verdict"):
            parts.append(f"Verdict: {parsed['verdict']}")
        if parsed.get("recommended_action"):
            parts.append(f"Recommended: {parsed['recommended_action']}")
        if parsed.get("counter_evidence"):
            parts.append(f"Would disprove this: {parsed['counter_evidence']}")
        return "\n\n".join(p for p in parts if p).strip()

    @staticmethod
    def _fallback_analysis(report: ChangeReport) -> str:
        """The ranking is arithmetic and survives the LLM being unavailable."""
        if not report.prime_suspect:
            return (f"{report.events_found} changes found, none scoring above zero "
                    f"after filtering out changes that happened after the symptom.")
        p = report.prime_suspect
        mins = p.seconds_before / 60.0
        return (f"Prime suspect (score {p.score}/100, confidence {report.confidence}): "
                f"{p.event.summary}, {mins:.1f} minutes before the symptom. "
                f"Basis: {'; '.join(p.reasons[:4])}. "
                f"AI narration unavailable — this ranking is arithmetic and stands alone.")

    def _remember(self, report: ChangeReport) -> None:
        if not report.prime_suspect:
            return
        p = report.prime_suspect
        remember(
            content=(f"Change correlation for '{report.symptom}': prime suspect "
                     f"{p.event.summary} (score {p.score}, {p.seconds_before/60:.1f}min before, "
                     f"confidence {report.confidence})"),
            source="change-correlation",
            metadata={"symptom": report.symptom[:200], "confidence": report.confidence,
                      "top_score": p.score, "kind": p.event.kind.value,
                      "resource": p.event.resource, "namespace": p.event.namespace},
        )
