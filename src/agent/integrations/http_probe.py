"""
Active network probing — DNS, TLS and HTTP. No AI. No business logic.

The other integrations in this package read *declared* state: what the
cluster says is configured, what AWS says exists. This one is the opposite —
it actually sends a request and reports what came back. That difference is
the whole point of the request-path skill: config can be perfect while the
site is still down, and only a real probe distinguishes the two.

Everything here is read-only in the strongest sense: a DNS lookup, a TLS
handshake, and an HTTP GET. Nothing is created, nothing is mutated, and no
credentials are sent — these are the same three things a human would do from
a laptop before opening kubectl.

As with every integration in this project, nothing raises: a failure is a
returned value with an `error` field, because "DNS does not resolve" IS the
answer the caller wants, not an exception to handle.
"""
from __future__ import annotations

import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from agent.observability.logging import get_logger

log = get_logger(__name__)

_DNS_TIMEOUT = 5.0
_TLS_TIMEOUT = 8.0
_HTTP_TIMEOUT = 12.0


def split_url(raw: str) -> tuple[str, str, int, str]:
    """Normalize any of 'example.com', 'https://example.com/x' into
    (url, host, port, scheme). A bare host defaults to https, because that is
    what a browser does and therefore what the user meant."""
    candidate = raw.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    scheme = parsed.scheme or "https"
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    return candidate, host, port, scheme


# ---------------------------------------------------------------------------
# Hop 1 — DNS
# ---------------------------------------------------------------------------

def resolve_dns(host: str) -> dict:
    """Resolve a hostname from THIS machine.

    Deliberately public resolution, not in-cluster: this answers "can a user
    reach it", which is a different question from whether CoreDNS works. The
    in-cluster view is already covered by dns.py.
    """
    if not host:
        return {"resolved": False, "addresses": [], "error": "no hostname given", "ms": 0.0}

    t0 = time.perf_counter()
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(_DNS_TIMEOUT)
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        log.info("probe.dns.nxdomain", host=host, error=str(e))
        return {"resolved": False, "addresses": [], "cname": "",
                "error": f"DNS lookup failed: {e}", "ms": ms}
    except Exception as e:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        log.warning("probe.dns.failed", host=host, error=str(e)[:200])
        return {"resolved": False, "addresses": [], "cname": "",
                "error": str(e)[:200], "ms": ms}
    finally:
        socket.setdefaulttimeout(old)

    ms = round((time.perf_counter() - t0) * 1000, 1)
    addresses = sorted({info[4][0] for info in infos})

    # The canonical name matters: a CNAME to *.elb.amazonaws.com is how we
    # know an AWS load balancer is in the path at all.
    cname = ""
    try:
        cname = socket.getfqdn(host)
        if cname == host:
            cname = ""
    except Exception:
        cname = ""

    return {"resolved": True, "addresses": addresses, "cname": cname, "error": "", "ms": ms}


# ---------------------------------------------------------------------------
# Hop 2 — TLS
# ---------------------------------------------------------------------------

def check_tls(host: str, port: int = 443) -> dict:
    """Complete a real TLS handshake and read the presented certificate.

    This catches what a config check cannot: cert-manager can report a
    Certificate as Ready while the ingress still serves the old or default
    certificate, and only a handshake shows which one is actually on the wire.
    """
    result = {"ok": False, "subject": "", "issuer": "", "not_after": "",
              "days_left": None, "error": "", "ms": 0.0, "self_signed": False}
    if not host:
        result["error"] = "no hostname given"
        return result

    t0 = time.perf_counter()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=_TLS_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
                result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    except ssl.SSLCertVerificationError as e:
        result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        result["error"] = f"certificate verification failed: {e.verify_message or e}"
        result["self_signed"] = "self signed" in str(e).lower()
        log.info("probe.tls.verify_failed", host=host, error=str(e)[:200])
        return result
    except (socket.timeout, TimeoutError):
        result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        result["error"] = f"TLS handshake timed out after {_TLS_TIMEOUT}s"
        return result
    except Exception as e:
        result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        result["error"] = str(e)[:200]
        log.info("probe.tls.failed", host=host, error=str(e)[:200])
        return result

    result["ok"] = True
    result["subject"] = _cert_field(cert, "subject", "commonName")
    result["issuer"] = _cert_field(cert, "issuer", "organizationName") or \
                       _cert_field(cert, "issuer", "commonName")
    not_after = cert.get("notAfter", "") if cert else ""
    result["not_after"] = not_after
    if not_after:
        try:
            expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            result["days_left"] = (expires - datetime.now(timezone.utc)).days
        except ValueError:
            result["days_left"] = None
    return result


def _cert_field(cert: dict | None, section: str, key: str) -> str:
    """Pull one field out of the nested tuple-of-tuples getpeercert() shape."""
    if not cert:
        return ""
    for rdn in cert.get(section, ()):
        for entry in rdn:
            if len(entry) == 2 and entry[0] == key:
                return str(entry[1])
    return ""


# ---------------------------------------------------------------------------
# Hop 3 — HTTP
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: float = _HTTP_TIMEOUT, verify: bool = True) -> dict:
    """One GET, following redirects, reporting status and latency.

    `verify=False` is offered only so the caller can distinguish "TLS is
    broken" from "the app is broken" — never as a default, and the skill
    labels any such result as having bypassed verification.
    """
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, verify=verify, follow_redirects=True) as c:
            r = c.get(url)
    except httpx.TooManyRedirects:
        return {"ok": False, "status": None, "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": "redirect loop", "server": "", "redirects": []}
    except Exception as e:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        log.info("probe.http.failed", url=url, error=str(e)[:200])
        return {"ok": False, "status": None, "ms": ms, "error": str(e)[:200],
                "server": "", "redirects": []}

    ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "ok": r.status_code < 500,
        "status": r.status_code,
        "ms": ms,
        "error": "",
        "server": r.headers.get("server", ""),
        # The ingress controller stamps its own headers; seeing them proves
        # the request reached nginx rather than dying at the load balancer.
        "via_ingress": bool(r.headers.get("x-request-id") or
                            "nginx" in r.headers.get("server", "").lower()),
        "redirects": [str(h.url) for h in r.history][:5],
        "body_bytes": len(r.content),
    }


def tcp_connect(host: str, port: int, timeout: float = 5.0) -> dict:
    """Bare TCP reachability — separates "nothing is listening" from "the
    application returned an error", which look identical from a browser."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "ms": round((time.perf_counter() - t0) * 1000, 1), "error": ""}
    except Exception as e:
        return {"ok": False, "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": str(e)[:200]}


def looks_like_aws_lb(hostname: str) -> bool:
    """True when a hostname is an AWS ELB/ALB/NLB DNS name."""
    h = (hostname or "").lower()
    return h.endswith(".elb.amazonaws.com") or ".elb." in h


def alb_dimension_from_arn(arn: str) -> str:
    """CloudWatch's LoadBalancer dimension is the ARN tail, not the ARN.

        arn:aws:elasticloadbalancing:eu-west-1:123:loadbalancer/app/my-alb/50dc6c49
                                                            -> app/my-alb/50dc6c49
    """
    marker = ":loadbalancer/"
    return arn.split(marker, 1)[1] if marker in arn else ""
