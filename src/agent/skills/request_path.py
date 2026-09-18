"""
Request-path skill — trace one URL from the public internet to the pod, hop
by hop, and name the weakest link.

This is the skill that turns "the site is down" into "it breaks at hop 6".

Nearly every hop already had a skill in this repo — dns, tls, ingress,
network, k8s, workloads, metrics — and that was exactly the problem: a human
had to know which one to reach for, run each separately, and join the results
in their head. Nothing walked the path *in order*, and nothing said which hop
was at fault.

The design decision that makes it useful: **probe first, then explain.**
Hops 1-3 are real network probes from this machine (DNS lookup, TLS
handshake, HTTP GET). If those all pass, the site is genuinely up and the
cluster-side hops are context rather than diagnosis. If one fails, the walk
continues anyway — because knowing that DNS failed AND the Service has zero
endpoints is a different fix from DNS failing while everything inside is
healthy.

Verdict selection is deterministic: the weakest hop is the FIRST failure
along the path, never the worst-sounding one. A failing hop upstream explains
every symptom downstream of it, so reporting hop 7 when hop 2 is broken would
send someone to fix the wrong thing.
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import (
    HopStatus, RequestHop, RequestPathReport,
)
from agent.core.parsing import LLMParseError, parse_llm_json
from agent.integrations import http_probe
from agent.integrations.kubectl import (
    check_ingress_controller, get_all_ingresses, get_cert_manager_certificates,
    get_endpoints, get_services_detail, get_workloads,
)
from agent.memory.retrieval import remember
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SYSTEM = """You are an SRE reading a hop-by-hop trace of one HTTP request,
from the public internet down to the pod.

The hop results below are MEASURED FACTS — real DNS lookups, a real TLS
handshake, a real HTTP request, and live cluster state. Do not dispute them
or re-derive the verdict.

Return JSON only with this exact structure:
{
  "analysis": "string — one short paragraph explaining what the trace shows",
  "root_cause": "string — the single most likely cause, or 'path is healthy'",
  "next_step": "string — the ONE action to take now",
  "ruled_out": "string — what this trace PROVES is not the problem, or empty"
}

Rules:
- The first failing hop explains everything downstream of it. Do not blame a
  later hop for a symptom an earlier failure already accounts for.
- If hops 1-3 all passed, the request genuinely succeeded from the public
  internet. Say the path is healthy even if a later cluster-side hop looks
  imperfect, and describe that hop as a risk rather than an outage.
- "ruled_out" is valuable — an engineer who knows DNS and TLS are fine has
  half the search space gone. Name what the trace eliminates.
"""


def _hop(index: int, name: str, layer: str, status: HopStatus, summary: str,
         latency_ms: float | None = None, evidence: list[str] | None = None,
         fix: str = "", fix_command: str | None = None) -> RequestHop:
    return RequestHop(
        index=index, name=name, layer=layer, status=status, summary=summary,
        latency_ms=latency_ms, evidence=evidence or [], fix=fix, fix_command=fix_command,
    )


class RequestPathSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "request-path"

    @property
    def description(self) -> str:
        return ("Trace one URL from the internet through DNS, TLS, load balancer, "
                "ingress, service, endpoints and pods, and name the weakest hop.")

    def execute(self, input_data: dict) -> dict:
        return self.trace(
            input_data["url"],
            input_data.get("namespace", ""),
        ).model_dump()

    # ------------------------------------------------------------------
    # The walk
    # ------------------------------------------------------------------

    def trace(self, url: str, namespace: str = "", use_llm: bool = True) -> RequestPathReport:
        """Walk every hop for one URL, in order."""
        full_url, host, port, scheme = http_probe.split_url(url)
        log.info("request_path.trace.start", url=full_url, host=host)

        report = RequestPathReport(
            url=full_url, host=host, namespace=namespace,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        # ---- hops 1-3: real probes from this machine -------------------
        dns = self._hop_dns(host, report)
        self._hop_tls(host, port, scheme, report)
        self._hop_http(full_url, report)

        # ---- hop 4: is an AWS load balancer in the path? ---------------
        self._hop_load_balancer(dns, report)

        # ---- hops 5-9: cluster side ------------------------------------
        ingress = self._hop_ingress_resource(host, report)
        self._hop_ingress_controller(report)
        svc_name, svc_ns = self._hop_service(ingress, report)
        self._hop_endpoints(svc_name, svc_ns, report)
        self._hop_workload(svc_name, svc_ns, report)

        self._finalize(report)

        if use_llm and report.weakest is not None:
            report.analysis = self._narrate(report)
        elif not report.analysis:
            report.analysis = report.verdict

        self._remember(report)
        log.info("request_path.trace.done", host=host, hops=len(report.hops),
                 reachable=report.reachable,
                 weakest=report.weakest.name if report.weakest else "none")
        return report

    # ------------------------------------------------------------------
    # Hop 1 — DNS
    # ------------------------------------------------------------------

    def _hop_dns(self, host: str, report: RequestPathReport) -> dict:
        dns = http_probe.resolve_dns(host)
        if not dns["resolved"]:
            report.hops.append(_hop(
                1, "DNS", "internet", HopStatus.FAIL,
                f"{host} does not resolve from this machine.",
                latency_ms=dns["ms"], evidence=[dns["error"]],
                fix=("Check the public DNS record exists and has propagated. If external-dns "
                     "manages it, check that controller."),
                fix_command=f"nslookup {host}",
            ))
            return dns

        evidence = [f"A/AAAA: {', '.join(dns['addresses'][:4])}"]
        if dns.get("cname"):
            evidence.append(f"canonical: {dns['cname']}")
        report.hops.append(_hop(
            1, "DNS", "internet", HopStatus.OK,
            f"{host} resolves to {len(dns['addresses'])} address(es).",
            latency_ms=dns["ms"], evidence=evidence,
        ))
        return dns

    # ------------------------------------------------------------------
    # Hop 2 — TLS
    # ------------------------------------------------------------------

    def _hop_tls(self, host: str, port: int, scheme: str, report: RequestPathReport) -> None:
        if scheme != "https":
            report.hops.append(_hop(
                2, "TLS", "internet", HopStatus.SKIP,
                "Plain http:// requested — no TLS in this path.",
            ))
            return

        tls = http_probe.check_tls(host, port)
        if not tls["ok"]:
            report.hops.append(_hop(
                2, "TLS", "internet", HopStatus.FAIL,
                f"TLS handshake with {host} failed.",
                latency_ms=tls["ms"], evidence=[tls["error"]],
                fix=("Check the cert-manager Certificate and the secret the Ingress "
                     "references. A handshake failure with a Ready Certificate usually "
                     "means the Ingress points at the wrong secret."),
                fix_command=f"agent tls scan",
            ))
            return

        days = tls["days_left"]
        evidence = [f"issuer: {tls['issuer'] or 'unknown'}",
                    f"subject: {tls['subject'] or 'unknown'}",
                    f"expires: {tls['not_after']}"]

        if days is not None and days < 0:
            status, summary = HopStatus.FAIL, f"Certificate EXPIRED {abs(days)} days ago."
        elif days is not None and days <= 14:
            status, summary = HopStatus.WARN, f"Certificate expires in {days} days."
        else:
            status = HopStatus.OK
            summary = (f"Valid certificate, {days} days remaining."
                       if days is not None else "Valid certificate.")

        report.hops.append(_hop(
            2, "TLS", "internet", status, summary,
            latency_ms=tls["ms"], evidence=evidence,
            fix=("Renew before expiry." if status != HopStatus.OK else ""),
            fix_command=(f"agent tls scan" if status != HopStatus.OK else None),
        ))

    # ------------------------------------------------------------------
    # Hop 3 — HTTP
    # ------------------------------------------------------------------

    def _hop_http(self, url: str, report: RequestPathReport) -> None:
        res = http_probe.http_get(url)
        report.total_latency_ms = res["ms"]
        report.http_status = res["status"]

        if res["status"] is None:
            report.hops.append(_hop(
                3, "HTTP", "internet", HopStatus.FAIL,
                "No HTTP response — the request never completed.",
                latency_ms=res["ms"], evidence=[res["error"]],
                fix="Check the load balancer and ingress controller hops below.",
            ))
            return

        status = res["status"]
        evidence = [f"status {status}", f"server: {res.get('server') or 'unset'}"]
        if res.get("redirects"):
            evidence.append(f"redirects: {' -> '.join(res['redirects'])}")
        if res.get("via_ingress"):
            evidence.append("response carries ingress-controller headers — nginx was reached")

        if status >= 500:
            hop_status = HopStatus.FAIL
            summary = f"HTTP {status} — the request reached a server and it failed."
            fix = ("The edge is fine; the fault is behind it. Check the endpoints and "
                   "workload hops below.")
        elif status >= 400:
            hop_status = HopStatus.WARN
            summary = f"HTTP {status} — reached the app, which rejected the request."
            fix = "A 4xx is usually routing or auth, not an outage. Confirm the path is correct."
        else:
            hop_status = HopStatus.OK
            summary = f"HTTP {status} in {res['ms']:.0f}ms."
            fix = ""
            report.reachable = True

        report.hops.append(_hop(
            3, "HTTP", "internet", hop_status, summary,
            latency_ms=res["ms"], evidence=evidence, fix=fix,
        ))

    # ------------------------------------------------------------------
    # Hop 4 — AWS load balancer
    # ------------------------------------------------------------------

    def _hop_load_balancer(self, dns: dict, report: RequestPathReport) -> None:
        """Only meaningful when an AWS LB is actually in the path.

        Detection is from the resolved canonical name, so a cluster fronted by
        something else (Cloudflare, a bare NodePort, a different cloud) gets an
        honest SKIP instead of an invented AWS hop.
        """
        cname = dns.get("cname", "") if dns else ""
        candidates = [cname] + list(dns.get("addresses", []) if dns else [])
        is_aws = any(http_probe.looks_like_aws_lb(c) for c in candidates if c)

        if not is_aws:
            report.hops.append(_hop(
                4, "Load balancer", "aws", HopStatus.SKIP,
                "No AWS load balancer detected in the DNS path.",
                evidence=[f"canonical name: {cname or '(none)'}"],
            ))
            return

        try:
            from agent.config import settings
            from agent.integrations.aws import get_all_load_balancers
            lbs = get_all_load_balancers(settings.aws_region)
        except Exception as e:
            report.hops.append(_hop(
                4, "Load balancer", "aws", HopStatus.UNKNOWN,
                "An AWS load balancer is in the path but its health could not be read.",
                evidence=[str(e)[:200], "Check AWS auth with `agent aws auth-status`."],
            ))
            report.notes.append("AWS load balancer health unavailable — AWS credentials or permissions.")
            return

        match = next((lb for lb in lbs if lb.get("dns_name") and lb["dns_name"].lower() in
                      [c.lower() for c in candidates if c]), None)
        if match is None:
            report.hops.append(_hop(
                4, "Load balancer", "aws", HopStatus.UNKNOWN,
                "An AWS load balancer is in the path but no matching LB was found in this account/region.",
                evidence=[f"resolved: {cname or candidates[:2]}",
                          f"searched {len(lbs)} load balancers in {report.namespace or 'the configured region'}",
                          "It may live in another region or another AWS account."],
            ))
            return

        healthy = match.get("healthy_targets", 0)
        total = match.get("total_targets", 0)
        evidence = [f"{match['type']} {match['name']} ({match['state']})",
                    f"targets healthy: {healthy}/{total}"]

        if match.get("state") != "active":
            status = HopStatus.FAIL
            summary = f"Load balancer {match['name']} is {match['state']}, not active."
        elif total > 0 and healthy == 0:
            status = HopStatus.FAIL
            summary = f"Load balancer {match['name']} has 0 of {total} targets healthy."
        elif total > 0 and healthy < total:
            status = HopStatus.WARN
            summary = f"Load balancer {match['name']}: {healthy}/{total} targets healthy."
        else:
            status = HopStatus.OK
            summary = f"Load balancer {match['name']} active, {healthy}/{total} targets healthy."

        report.hops.append(_hop(
            4, "Load balancer", "aws", status, summary, evidence=evidence,
            fix=("Failing targets are almost always the ingress-controller pods — "
                 "check the next hop." if status != HopStatus.OK else ""),
        ))

    # ------------------------------------------------------------------
    # Hop 5 — Ingress resource
    # ------------------------------------------------------------------

    def _hop_ingress_resource(self, host: str, report: RequestPathReport):
        """Find the Ingress whose rule matches this hostname."""
        try:
            ingresses = get_all_ingresses()
        except Exception as e:
            report.hops.append(_hop(
                5, "Ingress rule", "cluster", HopStatus.UNKNOWN,
                "Could not list Ingress resources.", evidence=[str(e)[:200]]))
            return None

        match = next((i for i in ingresses if i.domain and i.domain.lower() == host.lower()), None)
        if match is None:
            # A wildcard rule is a legitimate way to serve this host.
            wildcard = next((i for i in ingresses if i.domain.startswith("*.")
                             and host.lower().endswith(i.domain[1:].lower())), None)
            match = wildcard

        if match is None:
            report.hops.append(_hop(
                5, "Ingress rule", "cluster", HopStatus.FAIL,
                f"No Ingress in this cluster has a rule for {host}.",
                evidence=[f"{len(ingresses)} ingresses checked",
                          "Either the host is served elsewhere, or the rule is missing."],
                fix="Add or correct the Ingress rule host.",
                fix_command="kubectl get ingress -A",
            ))
            return None

        evidence = [f"{match.namespace}/{match.name}",
                    f"backend service: {match.backend_service or '(none)'}",
                    f"address: {match.address or '(none assigned)'}"]
        if match.tls_enabled:
            evidence.append(f"TLS secret: {match.tls_secret}")
            self._check_cert(match, report)

        if not match.address:
            status = HopStatus.WARN
            summary = f"Ingress {match.namespace}/{match.name} matches, but has no address assigned."
        elif not match.backend_service:
            status = HopStatus.FAIL
            summary = f"Ingress {match.namespace}/{match.name} matches but names no backend service."
        else:
            status = HopStatus.OK
            summary = f"Ingress {match.namespace}/{match.name} routes {host} to {match.backend_service}."

        report.hops.append(_hop(
            5, "Ingress rule", "cluster", status, summary, evidence=evidence,
            fix=("An Ingress with no address means the controller has not programmed it."
                 if status == HopStatus.WARN else ""),
        ))
        if not report.namespace:
            report.namespace = match.namespace
        return match

    def _check_cert(self, ingress, report: RequestPathReport) -> None:
        """Cross-check the cert-manager Certificate behind the Ingress secret.

        Worth doing even when the TLS handshake passed: a Certificate stuck
        in a not-Ready state while an old cert is still being served is a
        ticking clock, not a current outage.
        """
        try:
            certs = get_cert_manager_certificates()
        except Exception:
            return
        cert = next((c for c in certs
                     if c.secret_name == ingress.tls_secret and c.namespace == ingress.namespace), None)
        if cert is None:
            return
        if not cert.ready:
            report.notes.append(
                f"cert-manager Certificate {cert.namespace}/{cert.name} is NOT ready "
                f"({cert.status}: {cert.message[:120]}) — the served certificate may be stale."
            )

    # ------------------------------------------------------------------
    # Hop 6 — Ingress controller
    # ------------------------------------------------------------------

    def _hop_ingress_controller(self, report: RequestPathReport) -> None:
        """Controller health, graded against what the probe already proved.

        check_ingress_controller() reports "degraded" when SOME controller
        pods are ready and others are not. That is a real risk but not an
        outage, and calling it a failure while the site is serving 200s would
        be plainly wrong — so the probe result outranks the pod census here.
        """
        try:
            info = check_ingress_controller()
        except Exception as e:
            report.hops.append(_hop(
                6, "Ingress controller", "cluster", HopStatus.UNKNOWN,
                "Could not determine the ingress controller.", evidence=[str(e)[:200]]))
            return

        if not info.get("found"):
            report.hops.append(_hop(
                6, "Ingress controller", "cluster", HopStatus.WARN,
                "No ingress controller detected in the usual namespaces.",
                evidence=["Searched nginx, traefik and haproxy selectors.",
                          "Traffic may be served by a controller this check does not know about."],
            ))
            return

        ctype = info.get("type", "unknown")
        ns = info.get("namespace", "?")
        ready = info.get("pods_ok", []) or []
        unready = info.get("pods_bad", []) or []
        state = info.get("status", "unknown")

        evidence = [f"type: {ctype}", f"namespace: {ns}",
                    f"ready pods: {len(ready)} ({', '.join(ready[:3]) or 'none'})"]
        if unready:
            evidence.append(f"NOT ready: {len(unready)} ({', '.join(unready[:3])})")

        if state == "running":
            status = HopStatus.OK
            summary = f"{ctype} controller in {ns}: {len(ready)} pod(s) ready."
            fix = ""
        elif state == "degraded":
            # Reduced capacity, not an outage — and the probe usually proves it.
            status = HopStatus.WARN
            summary = (f"{ctype} controller in {ns} is degraded: "
                       f"{len(ready)} ready, {len(unready)} not ready.")
            fix = ("Capacity is reduced; a further pod failure takes the edge down. "
                   "Investigate the unready controller pods.")
            if report.reachable:
                evidence.append("This trace received a live HTTP response, so the "
                                "healthy pods are serving traffic right now.")
        else:   # "down" — no ready pods at all
            status = HopStatus.FAIL
            summary = f"{ctype} controller in {ns} has NO ready pods."
            fix = "Every host served by this controller is down until a pod becomes ready."
            if report.reachable:
                # Contradiction: something answered. Do not assert an outage.
                status = HopStatus.UNKNOWN
                summary = (f"{ctype} controller in {ns} reports no ready pods, yet this "
                           f"trace received a live HTTP response.")
                evidence.append("Contradiction — the controller check may be looking at "
                                "the wrong namespace or label selector, or another "
                                "controller is serving this host.")
                fix = "Confirm which controller actually serves this host before acting."

        report.hops.append(_hop(
            6, "Ingress controller", "cluster", status, summary,
            evidence=evidence, fix=fix,
            fix_command=("agent ingress scan" if status != HopStatus.OK else None),
        ))

    # ------------------------------------------------------------------
    # Hop 7 — Service
    # ------------------------------------------------------------------

    def _hop_service(self, ingress, report: RequestPathReport) -> tuple[str, str]:
        if ingress is None or not ingress.backend_service:
            report.hops.append(_hop(
                7, "Service", "cluster", HopStatus.SKIP,
                "No backend service to check — the Ingress hop did not resolve one.",
            ))
            return "", ""

        name, ns = ingress.backend_service, ingress.namespace
        try:
            services = get_services_detail(ns)
        except Exception as e:
            report.hops.append(_hop(
                7, "Service", "cluster", HopStatus.UNKNOWN,
                "Could not list services.", evidence=[str(e)[:200]]))
            return name, ns

        svc = next((s for s in services if s["name"] == name), None)
        if svc is None:
            report.hops.append(_hop(
                7, "Service", "cluster", HopStatus.FAIL,
                f"Ingress points at service {ns}/{name}, which does not exist.",
                evidence=[f"{len(services)} services in {ns}"],
                fix="Correct the Ingress backend, or create the missing Service.",
                fix_command=f"kubectl get svc -n {ns}",
            ))
            return name, ns

        evidence = [f"type: {svc['type']}", f"ports: {', '.join(svc['ports']) or 'none'}",
                    f"selector: {svc['selector'] or '(none)'}"]
        if not svc["selector"]:
            report.hops.append(_hop(
                7, "Service", "cluster", HopStatus.WARN,
                f"Service {ns}/{name} has no selector — endpoints are managed externally.",
                evidence=evidence,
            ))
            return name, ns

        report.hops.append(_hop(
            7, "Service", "cluster", HopStatus.OK,
            f"Service {ns}/{name} ({svc['type']}) selects {svc['selector']}.",
            evidence=evidence,
        ))
        return name, ns

    # ------------------------------------------------------------------
    # Hop 8 — Endpoints (the single most common real failure)
    # ------------------------------------------------------------------

    def _hop_endpoints(self, name: str, ns: str, report: RequestPathReport) -> None:
        if not name:
            report.hops.append(_hop(
                8, "Endpoints", "cluster", HopStatus.SKIP,
                "No service resolved — nothing to check endpoints for."))
            return

        try:
            endpoints = get_endpoints(ns)
        except Exception as e:
            report.hops.append(_hop(
                8, "Endpoints", "cluster", HopStatus.UNKNOWN,
                "Could not read endpoints.", evidence=[str(e)[:200]]))
            return

        ep = next((e for e in endpoints if e["name"] == name), None)
        ready = int(ep.get("ready", 0)) if ep else 0
        not_ready = int(ep.get("not_ready", 0)) if ep else 0

        if ready == 0:
            report.hops.append(_hop(
                8, "Endpoints", "cluster", HopStatus.FAIL,
                f"Service {ns}/{name} has ZERO ready endpoints — every request to it returns 503.",
                evidence=[f"ready: 0", f"not ready: {not_ready}",
                          "This is the most common cause of a 503 behind a healthy-looking Ingress.",
                          "Either no pods match the selector, or they are failing readiness."],
                fix="Check whether pods exist and are passing their readiness probe.",
                fix_command=f"kubectl get pods -n {ns} -o wide",
            ))
            return

        status = HopStatus.WARN if not_ready else HopStatus.OK
        report.hops.append(_hop(
            8, "Endpoints", "cluster", status,
            f"{ready} ready endpoint(s)" + (f", {not_ready} not ready." if not_ready else "."),
            evidence=[f"pods: {', '.join(ep.get('pods', [])[:5]) or 'n/a'}"],
            fix=("Some replicas are failing readiness — capacity is reduced."
                 if not_ready else ""),
        ))

    # ------------------------------------------------------------------
    # Hop 9 — Workload
    # ------------------------------------------------------------------

    def _hop_workload(self, svc_name: str, ns: str, report: RequestPathReport) -> None:
        if not ns:
            report.hops.append(_hop(
                9, "Workload", "app", HopStatus.SKIP,
                "No namespace resolved — nothing to check."))
            return

        try:
            workloads = []
            for kind in ("deployment", "statefulset"):
                workloads.extend(get_workloads(kind, ns))
        except Exception as e:
            report.hops.append(_hop(
                9, "Workload", "app", HopStatus.UNKNOWN,
                "Could not read workloads.", evidence=[str(e)[:200]]))
            return

        if not workloads:
            report.hops.append(_hop(
                9, "Workload", "app", HopStatus.WARN,
                f"No Deployments or StatefulSets found in {ns}.",
                evidence=["The pods behind this service may be unmanaged."]))
            return

        degraded = [w for w in workloads if w.desired > 0 and w.ready < w.desired]
        zeroed = [w for w in workloads if w.desired == 0]

        if degraded:
            worst = min(degraded, key=lambda w: (w.ready / w.desired) if w.desired else 0)
            report.hops.append(_hop(
                9, "Workload", "app", HopStatus.FAIL if worst.ready == 0 else HopStatus.WARN,
                f"{worst.kind.value} {ns}/{worst.name} is {worst.ready}/{worst.desired} ready.",
                evidence=[f"{len(degraded)} degraded workload(s) in {ns}",
                          f"images: {', '.join(worst.images[:2])}"],
                fix="Diagnose the controller for the reason replicas are unavailable.",
                fix_command=f"agent workloads diagnose {worst.kind.value.lower()} {worst.name} -n {ns}",
            ))
            return

        if zeroed:
            report.hops.append(_hop(
                9, "Workload", "app", HopStatus.WARN,
                f"{len(zeroed)} workload(s) in {ns} are scaled to zero.",
                evidence=[w.name for w in zeroed[:5]],
                fix="Scale up if that was not deliberate.",
            ))
            return

        total_ready = sum(w.ready for w in workloads)
        report.hops.append(_hop(
            9, "Workload", "app", HopStatus.OK,
            f"All {len(workloads)} workload(s) in {ns} at full replicas ({total_ready} pods ready).",
            evidence=[f"{w.name}: {w.ready}/{w.desired}" for w in workloads[:5]],
        ))

    # ------------------------------------------------------------------
    # Verdict
    # ------------------------------------------------------------------

    def _finalize(self, report: RequestPathReport) -> None:
        """Pick the weakest hop and write the verdict.

        The FIRST failure wins, not the worst-sounding one: an upstream
        failure explains everything downstream of it, so reporting a later
        hop would send someone to fix a symptom.
        """
        failures = [h for h in report.hops if h.status == HopStatus.FAIL]
        warnings = [h for h in report.hops if h.status == HopStatus.WARN]

        if failures and not report.reachable:
            report.weakest = failures[0]
            extra = (f" {len(failures) - 1} later hop(s) also failing, likely downstream of this."
                     if len(failures) > 1 else "")
            report.verdict = (f"Breaks at hop {report.weakest.index} "
                              f"({report.weakest.name}): {report.weakest.summary}{extra}")
        elif failures and report.reachable:
            # The probe succeeded, so this is NOT an outage. Saying "breaks at
            # hop N" while the site returns 200 would send someone to fix a
            # site that is up — the measured request outranks the inventory.
            report.weakest = failures[0]
            report.verdict = (
                f"Path is SERVING (HTTP {report.http_status}), but hop "
                f"{report.weakest.index} ({report.weakest.name}) reports a failure that "
                f"contradicts the successful request: {report.weakest.summary} "
                f"Treat this as a risk or a detection gap, not an outage.")
        elif warnings:
            report.weakest = warnings[0]
            served = (f"Path is serving (HTTP {report.http_status}). "
                      if report.reachable else "")
            report.verdict = (f"{served}Weakest link is hop {report.weakest.index} "
                              f"({report.weakest.name}): {report.weakest.summary}")
        else:
            report.weakest = None
            report.verdict = (f"All {len(report.hops)} hops healthy — "
                              f"HTTP {report.http_status} in "
                              f"{report.total_latency_ms:.0f}ms."
                              if report.total_latency_ms is not None
                              else "All hops healthy.")

        if not report.reachable and not failures:
            report.notes.append(
                "The request did not complete from this machine, but no hop failed outright — "
                "check egress from where this is running before blaming the cluster."
            )

    # ------------------------------------------------------------------
    # Narrative
    # ------------------------------------------------------------------

    def _narrate(self, report: RequestPathReport) -> str:
        lines = [f"URL: {report.url}", f"Verdict: {report.verdict}", "",
                 "HOPS (measured):"]
        for h in report.hops:
            latency = f" [{h.latency_ms:.0f}ms]" if h.latency_ms is not None else ""
            lines.append(f"  {h.index}. {h.name} ({h.layer}) = {h.status.value}{latency}: {h.summary}")
            for e in h.evidence[:3]:
                if e:
                    lines.append(f"       - {e}")
        if report.notes:
            lines += ["", "NOTES:", *[f"  - {n}" for n in report.notes]]

        try:
            response = run_sync(llm.chat(
                messages=[context.user_message("\n".join(lines))],
                system=_SYSTEM, json_mode=True, max_tokens=800,
            ))
            parsed = parse_llm_json(response.content)
        except LLMParseError as exc:
            log.warning("request_path.narrate.parse_failed", error=str(exc.cause))
            return report.verdict
        except Exception as exc:
            log.warning("request_path.narrate.failed", error=str(exc)[:200])
            return report.verdict

        parts = [parsed.get("analysis", "")]
        if parsed.get("root_cause"):
            parts.append(f"Root cause: {parsed['root_cause']}")
        if parsed.get("next_step"):
            parts.append(f"Next step: {parsed['next_step']}")
        if parsed.get("ruled_out"):
            parts.append(f"Ruled out: {parsed['ruled_out']}")
        return "\n\n".join(p for p in parts if p).strip()

    def _remember(self, report: RequestPathReport) -> None:
        remember(
            content=(f"Request path {report.url}: {report.verdict}"),
            source="request-path",
            metadata={"host": report.host, "namespace": report.namespace,
                      "reachable": report.reachable,
                      "http_status": report.http_status or 0,
                      "weakest_hop": report.weakest.name if report.weakest else "none"},
        )
