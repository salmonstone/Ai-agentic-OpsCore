"""
Metrics skill — time-series analysis: golden signals, anomaly detection,
and recovery verification.

This is the skill the rest of Tier 1 leans on. Before it, the agent's only
metric source was `kubectl top`: one instantaneous reading, which answers
"is it hot right now" and cannot answer "has it been climbing for six hours"
— and that second shape is what separates a memory leak from a busy
afternoon, a real regression from normal variance, and a fix that worked
from a fix that merely finished running.

Design, deliberately mirroring skills/jenkins.py: **deterministic detectors
run first, Claude runs only on what is left.** Saturation, spikes, drops,
leak-shaped trends and dead scrape targets are all arithmetic — free,
instant, identical every time, and unit-testable without an API key. The LLM
is used for one thing only: turning a set of already-proven anomalies into a
sentence a human can act on. If it is unavailable, the anomalies still stand.

Degradation is explicit, never silent. With no Prometheus configured the
skill falls back to `kubectl top`, sets `degraded=True`, names the reason,
and reports only what a single datapoint can honestly support.
"""
from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone

from agent.config import settings
from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import (
    AnomalyKind, GoldenSignals, MetricAnomaly, MetricSeries, MetricSource,
    MetricsReport,
)
from agent.core.parsing import LLMParseError, parse_llm_json
from agent.integrations import cloudwatch, prometheus
from agent.memory.retrieval import remember, retrieve_context
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SYSTEM = """You are an SRE analysing already-computed metric anomalies.

The anomalies below were detected arithmetically and are FACTS — do not
dispute, re-derive or re-rank them. Your only job is interpretation.

Return JSON only with this exact structure:
{
  "analysis": "string — one paragraph a human on call can act on",
  "likely_cause": "string — the most plausible single cause, or 'unclear'",
  "suggested_next_step": "string — the ONE next diagnostic action, not a list"
}

Rules:
- If the anomalies are consistent with a memory leak (steady TrendUp on
  memory with flat request rate), say so explicitly.
- A Drop in request rate with no error increase usually means traffic stopped
  upstream, NOT that this service failed. Say that rather than blaming it.
- A Flatline at zero usually means a dead scrape target, not a dead service.
- If the anomalies do not support a conclusion, say "unclear" and ask for the
  one signal that would resolve it. Never invent a cause to fill the field.
"""

# Candidate PromQL per signal, tried in order until one returns data.
# Different clusters expose different exporters; rather than demanding one
# convention, probe for the metric that actually exists. {ns}/{sel} are
# substituted before the query runs.
_CPU_QUERIES = [
    'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}",pod=~"{sel}",container!=""}}[5m]))',
    'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}"}}[5m]))',
]
_CPU_LIMIT_QUERIES = [
    'sum(kube_pod_container_resource_limits{{namespace="{ns}",pod=~"{sel}",resource="cpu"}})',
    'sum(kube_pod_container_resource_limits{{namespace="{ns}",resource="cpu"}})',
]
_MEM_QUERIES = [
    'sum(container_memory_working_set_bytes{{namespace="{ns}",pod=~"{sel}",container!=""}})',
    'sum(container_memory_working_set_bytes{{namespace="{ns}"}})',
]
_MEM_LIMIT_QUERIES = [
    'sum(kube_pod_container_resource_limits{{namespace="{ns}",pod=~"{sel}",resource="memory"}})',
    'sum(kube_pod_container_resource_limits{{namespace="{ns}",resource="memory"}})',
]
_RATE_QUERIES = [
    'sum(rate(http_requests_total{{namespace="{ns}",pod=~"{sel}"}}[5m]))',
    'sum(rate(http_server_requests_seconds_count{{namespace="{ns}",pod=~"{sel}"}}[5m]))',
    'sum(rate(nginx_ingress_controller_requests{{namespace="{ns}"}}[5m]))',
    'sum(rate(istio_requests_total{{destination_service_namespace="{ns}"}}[5m]))',
]
_ERROR_QUERIES = [
    'sum(rate(http_requests_total{{namespace="{ns}",pod=~"{sel}",status=~"5.."}}[5m]))',
    'sum(rate(nginx_ingress_controller_requests{{namespace="{ns}",status=~"5.."}}[5m]))',
    'sum(rate(istio_requests_total{{destination_service_namespace="{ns}",response_code=~"5.."}}[5m]))',
]
_LATENCY_QUERIES = [
    'histogram_quantile({q}, sum(rate(http_request_duration_seconds_bucket{{namespace="{ns}",pod=~"{sel}"}}[5m])) by (le))',
    'histogram_quantile({q}, sum(rate(istio_request_duration_milliseconds_bucket{{destination_service_namespace="{ns}"}}[5m])) by (le)) / 1000',
]
_RESTART_QUERIES = [
    'sum(rate(kube_pod_container_status_restarts_total{{namespace="{ns}",pod=~"{sel}"}}[15m])) * 3600',
]


# ---------------------------------------------------------------------------
# Pure math — no I/O, no LLM, fully unit-testable
# ---------------------------------------------------------------------------

def _linear_fit(values: list[float]) -> tuple[float, float]:
    """Least-squares slope (per sample) and r^2 for an evenly-spaced series.

    r^2 matters as much as the slope: memory that climbs steadily is a leak,
    memory that climbs because of one step change is a deploy. Both have a
    positive slope; only the first is straight.
    """
    n = len(values)
    if n < 3:
        return 0.0, 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0, 0.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)) / denom
    intercept = mean_y - slope * mean_x

    ss_tot = sum((y - mean_y) ** 2 for y in values)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, values))
    r2 = 0.0 if ss_tot == 0 else max(0.0, 1.0 - (ss_res / ss_tot))
    return slope, r2


def detect_anomalies(series: MetricSeries, *, limit: float | None = None,
                     label: str = "") -> list[MetricAnomaly]:
    """Every deterministic detector, run against one series.

    Pure: same input always produces the same output, no network, no model.
    This is the metrics equivalent of jenkins._detect_pattern().
    """
    name = label or series.name
    values = series.values()
    out: list[MetricAnomaly] = []
    if len(values) < 3:
        return out

    current = values[-1]
    baseline = statistics.median(values[:-1]) if len(values) > 1 else current
    stdev = statistics.pstdev(values[:-1]) if len(values) > 2 else 0.0

    # --- Flatline: no variance at zero. Almost always a dead scrape target,
    # and saying so stops someone diagnosing a service that is in fact fine.
    if max(values) == 0.0 and min(values) == 0.0:
        out.append(MetricAnomaly(
            kind=AnomalyKind.FLATLINE, severity="warning", metric=name,
            labels=series.labels, current=0.0, baseline=0.0,
            description=f"{name} is flat at zero across the whole window.",
            evidence=[f"{len(values)} datapoints, all 0",
                      "Usually a dead scrape target or a renamed metric, not a dead service."],
        ))
        return out    # every other detector on an all-zero series is noise

    # --- Saturation against a known limit
    if limit and limit > 0:
        ratio = current / limit
        if ratio >= settings.metric_saturation_crit:
            out.append(MetricAnomaly(
                kind=AnomalyKind.SATURATION, severity="critical", metric=name,
                labels=series.labels, current=current, baseline=limit,
                change_pct=round(ratio * 100, 1),
                description=f"{name} is at {ratio:.0%} of its limit.",
                evidence=[f"current={current:.4g}", f"limit={limit:.4g}"],
            ))
        elif ratio >= settings.metric_saturation_warn:
            out.append(MetricAnomaly(
                kind=AnomalyKind.SATURATION, severity="warning", metric=name,
                labels=series.labels, current=current, baseline=limit,
                change_pct=round(ratio * 100, 1),
                description=f"{name} is at {ratio:.0%} of its limit.",
                evidence=[f"current={current:.4g}", f"limit={limit:.4g}"],
            ))

    # --- Spike / drop against the series' own baseline
    if stdev > 0:
        sigma = settings.metric_spike_sigma
        if current > baseline + sigma * stdev:
            out.append(MetricAnomaly(
                kind=AnomalyKind.SPIKE, severity="warning", metric=name,
                labels=series.labels, current=current, baseline=baseline,
                change_pct=round(((current - baseline) / baseline * 100) if baseline else 0.0, 1),
                description=f"{name} spiked to {current:.4g} against a baseline of {baseline:.4g}.",
                evidence=[f"{sigma}-sigma threshold = {baseline + sigma * stdev:.4g}",
                          f"stdev={stdev:.4g}"],
            ))
        elif current < baseline - sigma * stdev:
            out.append(MetricAnomaly(
                kind=AnomalyKind.DROP, severity="warning", metric=name,
                labels=series.labels, current=current, baseline=baseline,
                change_pct=round(((current - baseline) / baseline * 100) if baseline else 0.0, 1),
                description=f"{name} dropped to {current:.4g} from a baseline of {baseline:.4g}.",
                evidence=[f"{sigma}-sigma threshold = {baseline - sigma * stdev:.4g}",
                          "A drop in rate with no error rise usually means traffic stopped upstream."],
            ))

    # --- Leak-shaped trend: sustained, straight growth
    slope, r2 = _linear_fit(values)
    first = values[0] or 0.0
    growth = ((current - first) / first) if first > 0 else 0.0
    if (slope > 0 and r2 >= settings.metric_trend_min_r2
            and growth >= settings.metric_trend_min_growth):
        hours = max(series.span_seconds() / 3600.0, 0.01)
        out.append(MetricAnomaly(
            kind=AnomalyKind.TREND_UP, severity="warning", metric=name,
            labels=series.labels, current=current, baseline=first,
            change_pct=round(growth * 100, 1),
            description=(f"{name} grew {growth:.0%} over {hours:.1f}h in a straight line "
                         f"(r2={r2:.2f}) — leak-shaped, not load-shaped."),
            evidence=[f"start={first:.4g}", f"end={current:.4g}", f"r2={r2:.2f}",
                      "Compare against request rate: flat traffic + rising memory = leak."],
        ))

    return out


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class MetricsSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "metrics"

    @property
    def description(self) -> str:
        return ("Time-series analysis — golden signals, anomaly detection and "
                "post-remediation recovery verification.")

    def execute(self, input_data: dict) -> dict:
        mode = input_data.get("mode", "analyze")
        if mode == "analyze":
            return self.analyze(
                input_data.get("target", ".*"),
                input_data.get("namespace", "default"),
                int(input_data.get("minutes", 60)),
            ).model_dump()
        if mode == "signals":
            return self.golden_signals(
                input_data.get("target", ".*"),
                input_data.get("namespace", "default"),
                int(input_data.get("minutes", 60)),
            ).model_dump()
        if mode == "status":
            return self.status()
        raise ValueError(f"Unknown mode: {mode!r}")

    # ------------------------------------------------------------------
    # Source resolution
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Which time-series backends are actually reachable.

        Deliberately the first thing to run: every other answer this skill
        gives is qualified by it, and 'no metrics backend' is a configuration
        problem a human fixes in a minute, not something to diagnose around.
        """
        prom = prometheus.check_connection()
        cw = cloudwatch.check_connection()
        source = (MetricSource.PROMETHEUS if prom.get("connected")
                  else MetricSource.CLOUDWATCH if cw.get("connected")
                  else MetricSource.KUBECTL_TOP)
        return {
            "prometheus": prom,
            "cloudwatch": cw,
            "effective_source": source.value,
            "degraded": source == MetricSource.KUBECTL_TOP,
            "note": ("No time-series backend reachable — falling back to a single "
                     "kubectl-top reading. Trend, spike and leak detection are "
                     "unavailable in that mode."
                     if source == MetricSource.KUBECTL_TOP else ""),
        }

    def _first_with_data(self, queries: list[str], ns: str, sel: str,
                         minutes: int, unit: str = "", q: str = "") -> MetricSeries | None:
        """Try candidate PromQL in order, return the first that has points."""
        for template in queries:
            promql = template.format(ns=ns, sel=sel, q=q)
            series = prometheus.query_range(promql, minutes=minutes, unit=unit)
            for s in series:
                if s.points:
                    return s
        return None

    # ------------------------------------------------------------------
    # Golden signals
    # ------------------------------------------------------------------

    def golden_signals(self, target: str = ".*", namespace: str = "default",
                       minutes: int = 60) -> GoldenSignals:
        """RED + USE for one target. `target` is a pod-name regex."""
        signals = GoldenSignals(target=f"{namespace}/{target}")
        if not prometheus.is_configured():
            return signals

        rate = self._first_with_data(_RATE_QUERIES, namespace, target, minutes, "req/s")
        errors = self._first_with_data(_ERROR_QUERIES, namespace, target, minutes, "req/s")
        if rate is not None:
            signals.rate_per_sec = rate.latest()
        if errors is not None and rate is not None:
            r = rate.latest() or 0.0
            e = errors.latest() or 0.0
            # Guard the divide: zero traffic with zero errors is healthy, and
            # reporting it as a 0/0 error ratio would page someone at 3am.
            signals.error_ratio = round(e / r, 4) if r > 0 else (None if e == 0 else 1.0)

        for q, attr in (("0.50", "latency_p50"), ("0.95", "latency_p95"), ("0.99", "latency_p99")):
            s = self._first_with_data(_LATENCY_QUERIES, namespace, target, minutes, "s", q=q)
            if s is not None:
                setattr(signals, attr, s.latest())

        cpu = self._first_with_data(_CPU_QUERIES, namespace, target, minutes, "cores")
        cpu_lim = self._first_with_data(_CPU_LIMIT_QUERIES, namespace, target, minutes, "cores")
        if cpu is not None and cpu_lim is not None:
            limit = cpu_lim.latest() or 0.0
            signals.cpu_saturation = round((cpu.latest() or 0.0) / limit, 4) if limit > 0 else None

        mem = self._first_with_data(_MEM_QUERIES, namespace, target, minutes, "bytes")
        mem_lim = self._first_with_data(_MEM_LIMIT_QUERIES, namespace, target, minutes, "bytes")
        if mem is not None and mem_lim is not None:
            limit = mem_lim.latest() or 0.0
            signals.mem_saturation = round((mem.latest() or 0.0) / limit, 4) if limit > 0 else None

        restarts = self._first_with_data(_RESTART_QUERIES, namespace, target, minutes, "per hour")
        if restarts is not None:
            signals.restart_rate = restarts.latest()

        return signals

    # ------------------------------------------------------------------
    # Main analysis
    # ------------------------------------------------------------------

    def analyze(self, target: str = ".*", namespace: str = "default",
                minutes: int = 60, use_llm: bool = True) -> MetricsReport:
        """Full time-series analysis of one target."""
        log.info("metrics.analyze.start", target=target, namespace=namespace, minutes=minutes)
        now = datetime.now(timezone.utc).isoformat()

        if not prometheus.is_configured():
            return self._degraded_report(target, namespace, minutes, now)

        collected: list[tuple[MetricSeries, float | None, str]] = []

        cpu = self._first_with_data(_CPU_QUERIES, namespace, target, minutes, "cores")
        cpu_lim = self._first_with_data(_CPU_LIMIT_QUERIES, namespace, target, minutes, "cores")
        if cpu is not None:
            collected.append((cpu, cpu_lim.latest() if cpu_lim else None, "cpu"))

        mem = self._first_with_data(_MEM_QUERIES, namespace, target, minutes, "bytes")
        mem_lim = self._first_with_data(_MEM_LIMIT_QUERIES, namespace, target, minutes, "bytes")
        if mem is not None:
            collected.append((mem, mem_lim.latest() if mem_lim else None, "memory"))

        for queries, label, unit in (
            (_RATE_QUERIES, "request_rate", "req/s"),
            (_ERROR_QUERIES, "error_rate", "req/s"),
            (_RESTART_QUERIES, "restart_rate", "per hour"),
        ):
            s = self._first_with_data(queries, namespace, target, minutes, unit)
            if s is not None:
                collected.append((s, None, label))

        anomalies: list[MetricAnomaly] = []
        for series, limit, label in collected:
            anomalies.extend(detect_anomalies(series, limit=limit, label=label))

        # Firing alerts and dead targets are evidence a human already agreed
        # mattered — cheaper and more trustworthy than re-deriving them.
        for alert in prometheus.active_alerts():
            if alert.get("namespace") and alert["namespace"] != namespace:
                continue
            anomalies.append(MetricAnomaly(
                kind=AnomalyKind.SPIKE,
                severity="critical" if alert.get("severity") == "critical" else "warning",
                metric=f"alert:{alert['name']}",
                labels={k: v for k, v in alert.items() if k in ("pod", "service", "namespace") and v},
                description=alert.get("summary") or alert["name"],
                evidence=[alert.get("description", "")[:200], f"firing since {alert.get('active_at', '?')}"],
            ))

        report = MetricsReport(
            target=target, namespace=namespace, window_minutes=minutes,
            source=MetricSource.PROMETHEUS, series_count=len(collected),
            signals=self.golden_signals(target, namespace, minutes),
            anomalies=anomalies, generated_at=now,
        )

        if not collected:
            report.degraded = True
            report.degraded_reason = (
                "Prometheus answered but no series matched. Check the namespace, the pod "
                "regex, and whether kube-state-metrics / cAdvisor are scraped in this cluster."
            )
            return report

        if anomalies and use_llm:
            report.analysis = self._narrate(report)
        elif not anomalies:
            report.analysis = (f"No anomalies across {len(collected)} series in the last "
                               f"{minutes}m. Saturation, spike, drop, trend and flatline "
                               f"detectors all clear.")

        self._remember(report)
        log.info("metrics.analyze.done", target=target,
                 anomalies=len(anomalies), series=len(collected))
        return report

    def _degraded_report(self, target: str, namespace: str, minutes: int,
                         now: str) -> MetricsReport:
        """kubectl-top fallback: one datapoint, so saturation only.

        This reports strictly less than the Prometheus path and says exactly
        why, rather than pretending a single reading supports trend analysis.
        """
        from agent.integrations.kubectl import get_pod_metrics

        report = MetricsReport(
            target=target, namespace=namespace, window_minutes=minutes,
            source=MetricSource.KUBECTL_TOP, degraded=True, generated_at=now,
            degraded_reason=("PROMETHEUS_URL is not set. Falling back to a single "
                             "kubectl-top reading: saturation is detectable, trends, "
                             "spikes and leaks are not."),
        )

        try:
            pods = get_pod_metrics(namespace)
        except Exception as e:
            log.warning("metrics.degraded.top_failed", error=str(e)[:200])
            report.degraded_reason += " kubectl top also failed — no metrics at all."
            return report

        import re
        try:
            pattern = re.compile(target)
        except re.error:
            pattern = re.compile(re.escape(target))

        matched = [p for p in pods if pattern.search(p.name)]
        report.series_count = len(matched)

        for p in matched:
            for pct, kind_label in ((p.cpu_percent, "cpu"), (p.memory_percent, "memory")):
                if pct >= settings.metric_saturation_crit * 100:
                    sev = "critical"
                elif pct >= settings.metric_saturation_warn * 100:
                    sev = "warning"
                else:
                    continue
                report.anomalies.append(MetricAnomaly(
                    kind=AnomalyKind.SATURATION, severity=sev,
                    metric=f"{kind_label}_percent", labels={"pod": p.name, "namespace": p.namespace},
                    current=float(pct), baseline=100.0, change_pct=float(pct),
                    description=f"{p.name} is at {pct}% of its {kind_label} limit.",
                    evidence=[f"usage={p.cpu_usage if kind_label == 'cpu' else p.memory_usage}",
                              f"limit={p.cpu_limit if kind_label == 'cpu' else p.memory_limit}",
                              "Single datapoint — no trend available."],
                ))
        return report

    # ------------------------------------------------------------------
    # Recovery verification — the loop that was missing
    # ------------------------------------------------------------------

    def verify_recovery(self, target: str, namespace: str = "default",
                        minutes: int = 15) -> dict:
        """Did a remediation actually work?

        Every self-healing path in this repo could previously confirm that a
        fix *ran* — a pod restarted, a rollout completed. None could confirm
        the symptom went away, because that requires comparing a metric
        against its own recent past. This closes that loop.
        """
        report = self.analyze(target, namespace, minutes, use_llm=False)
        blocking = [a for a in report.anomalies if a.severity == "critical"]
        warnings = [a for a in report.anomalies if a.severity == "warning"]

        if report.degraded:
            verdict, recovered = "unknown", False
            reason = f"Cannot verify: {report.degraded_reason}"
        elif blocking:
            verdict, recovered = "not_recovered", False
            reason = f"{len(blocking)} critical anomalies still present: " + \
                     "; ".join(a.description for a in blocking[:3])
        elif warnings:
            verdict, recovered = "partial", False
            reason = f"{len(warnings)} warnings remain: " + \
                     "; ".join(a.description for a in warnings[:3])
        else:
            verdict, recovered = "recovered", True
            reason = f"All detectors clear across {report.series_count} series over {minutes}m."

        result = {
            "target": target, "namespace": namespace, "window_minutes": minutes,
            "recovered": recovered, "verdict": verdict, "reason": reason,
            "anomalies": [a.model_dump() for a in report.anomalies],
            "source": report.source.value, "degraded": report.degraded,
        }
        remember(
            content=(f"Recovery check for {namespace}/{target}: {verdict} — {reason}"),
            source="metrics-verify",
            metadata={"target": target, "namespace": namespace,
                      "verdict": verdict, "recovered": recovered},
        )
        return result

    # ------------------------------------------------------------------
    # Cluster-wide sweep
    # ------------------------------------------------------------------

    def scan(self, namespace: str = "all", minutes: int = 60) -> MetricsReport:
        """Anomalies across a namespace (or the cluster), plus dead targets."""
        ns = "" if namespace == "all" else namespace
        now = datetime.now(timezone.utc).isoformat()

        if not prometheus.is_configured():
            return self._degraded_report(".*", namespace, minutes, now)

        report = self.analyze(".*", ns or ".*", minutes, use_llm=False)
        report.target = f"namespace={namespace}"

        for t in prometheus.targets_down():
            if ns and t.get("namespace") and t["namespace"] != ns:
                continue
            report.anomalies.append(MetricAnomaly(
                kind=AnomalyKind.FLATLINE, severity="warning",
                metric=f"scrape:{t['job']}", labels={"instance": t["instance"]},
                description=f"Scrape target {t['job']} / {t['instance']} is down.",
                evidence=[t.get("last_error", "")[:200],
                          "Metrics from this target are stale — do not trust its series."],
            ))

        if report.anomalies:
            report.analysis = self._narrate(report)
        return report

    # ------------------------------------------------------------------
    # Narrative (the only LLM call in this skill)
    # ------------------------------------------------------------------

    def _narrate(self, report: MetricsReport) -> str:
        lines = [
            f"Target: {report.namespace}/{report.target}",
            f"Window: last {report.window_minutes} minutes",
            f"Source: {report.source.value}",
            "",
            "ANOMALIES (already proven arithmetically):",
        ]
        for a in report.anomalies[:25]:
            lines.append(f"  [{a.severity}] {a.kind.value} {a.metric}: {a.description}")
            for e in a.evidence[:2]:
                if e:
                    lines.append(f"      - {e}")

        if report.signals:
            s = report.signals
            lines += ["", "GOLDEN SIGNALS:",
                      f"  rate={s.rate_per_sec} err_ratio={s.error_ratio} "
                      f"p95={s.latency_p95} p99={s.latency_p99}",
                      f"  cpu_sat={s.cpu_saturation} mem_sat={s.mem_saturation} "
                      f"restarts/h={s.restart_rate}"]

        memories = retrieve_context(f"metrics {report.namespace} {report.target} anomaly")
        try:
            response = run_sync(llm.chat(
                messages=[context.user_message("\n".join(lines))],
                system=context.build_system_prompt(_SYSTEM, memories),
                json_mode=True, max_tokens=800,
            ))
            parsed = parse_llm_json(response.content)
        except LLMParseError as exc:
            log.warning("metrics.narrate.parse_failed", error=str(exc.cause))
            return self._fallback_analysis(report)
        except Exception as exc:
            log.warning("metrics.narrate.failed", error=str(exc)[:200])
            return self._fallback_analysis(report)

        cause = parsed.get("likely_cause", "unclear")
        step = parsed.get("suggested_next_step", "")
        return f"{parsed.get('analysis', '')}\n\nLikely cause: {cause}\nNext step: {step}".strip()

    def _fallback_analysis(self, report: MetricsReport) -> str:
        """The anomalies are facts; they survive the LLM being unavailable."""
        crit = sum(1 for a in report.anomalies if a.severity == "critical")
        warn = sum(1 for a in report.anomalies if a.severity == "warning")
        kinds = sorted({a.kind.value for a in report.anomalies})
        return (f"{crit} critical and {warn} warning anomalies detected "
                f"({', '.join(kinds)}). AI narration unavailable — the detections "
                f"above are arithmetic and stand on their own.")

    def _remember(self, report: MetricsReport) -> None:
        if not report.anomalies:
            return
        top = report.anomalies[0]
        remember(
            content=(f"Metrics {report.namespace}/{report.target}: "
                     f"{len(report.anomalies)} anomalies, most severe "
                     f"{top.kind.value} on {top.metric} — {top.description}"),
            source="metrics",
            metadata={"namespace": report.namespace, "target": report.target,
                      "anomaly_count": len(report.anomalies),
                      "top_kind": top.kind.value, "source": report.source.value},
        )
