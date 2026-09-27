"""Tests for the MCP server's network-facing security controls.

These guard the one path by which an internet client reaches real
Jenkins/EKS/AWS credentials, so a regression here is the most expensive kind.
"""
import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent import mcp_server
from agent.mcp_server import _BearerAuthMiddleware

TOKEN = "test-token-abc123"
REPO_ROOT = Path(__file__).resolve().parent.parent


# --- bearer auth middleware ----------------------------------------------

def _request(headers=None, query=b"", scope_type="http"):
    """Drive the middleware once; return (status, inner_app_was_called)."""
    called = {"inner": False}
    sent = []

    async def inner(scope, receive, send):
        called["inner"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": scope_type, "headers": headers or [], "query_string": query}
    asyncio.run(_BearerAuthMiddleware(inner, TOKEN)(scope, receive, send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    return status, called["inner"]


def test_missing_token_is_rejected():
    assert _request() == (401, False)


def test_wrong_bearer_token_is_rejected():
    assert _request(headers=[(b"authorization", b"Bearer nope")]) == (401, False)


def test_correct_bearer_token_passes():
    assert _request(headers=[(b"authorization", f"Bearer {TOKEN}".encode())]) == (200, True)


def test_header_name_is_case_insensitive():
    assert _request(headers=[(b"Authorization", f"Bearer {TOKEN}".encode())]) == (200, True)


def test_token_without_bearer_prefix_is_rejected():
    assert _request(headers=[(b"authorization", TOKEN.encode())]) == (401, False)


def test_correct_query_token_passes():
    assert _request(query=f"token={TOKEN}".encode()) == (200, True)


def test_wrong_query_token_is_rejected():
    assert _request(query=b"token=nope") == (401, False)


def test_prefix_of_real_token_is_rejected():
    assert _request(query=f"token={TOKEN[:-1]}".encode()) == (401, False)


def test_rejection_carries_www_authenticate_and_no_body_leak():
    sent = []

    async def inner(scope, receive, send):
        raise AssertionError("inner app must not run on a rejected request")

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    asyncio.run(_BearerAuthMiddleware(inner, TOKEN)(
        {"type": "http", "headers": [], "query_string": b""}, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert (b"www-authenticate", b"Bearer") in start["headers"]
    assert TOKEN.encode() not in body


def test_non_http_scopes_pass_through():
    # lifespan events carry no headers; gating them would break server startup.
    _, called = _request(scope_type="lifespan")
    assert called


# --- DNS-rebinding host allowlist ----------------------------------------

def test_no_allowed_hosts_keeps_library_default(monkeypatch):
    monkeypatch.setattr(mcp_server, "_ALLOWED_HOSTS", [])
    assert mcp_server._transport_security() is None


def test_allowed_hosts_widen_but_never_disable_protection(monkeypatch):
    monkeypatch.setattr(mcp_server, "_ALLOWED_HOSTS", ["demo.ngrok-free.dev"])
    settings = mcp_server._transport_security()
    assert settings.enable_dns_rebinding_protection is True
    assert "demo.ngrok-free.dev" in settings.allowed_hosts
    assert "localhost" in settings.allowed_hosts
    assert "https://demo.ngrok-free.dev" in settings.allowed_origins


def test_ngrok_hostname_discovery(monkeypatch):
    payload = {"tunnels": [
        {"public_url": "https://abc.ngrok-free.dev"},
        {"public_url": "https://abc.ngrok-free.dev"},   # duplicate collapsed
        {"public_url": "http://other.ngrok.app"},
    ]}

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(mcp_server.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(json.dumps(payload).encode()))
    assert mcp_server._ngrok_hostnames() == ["abc.ngrok-free.dev", "other.ngrok.app"]


def test_ngrok_api_url_is_overridable(monkeypatch):
    # In a container, ngrok runs on the host, not at the container's 127.0.0.1.
    seen = []

    def fake_urlopen(url, timeout):
        seen.append(url)
        raise OSError("unreachable")
    monkeypatch.setenv("NGROK_API_URL", "http://host.docker.internal:4040/")
    monkeypatch.setattr(mcp_server.urllib.request, "urlopen", fake_urlopen)
    mcp_server._ngrok_hostnames()
    assert seen == ["http://host.docker.internal:4040/api/tunnels"]


def test_ngrok_absent_returns_empty_instead_of_raising(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(mcp_server.urllib.request, "urlopen", boom)
    assert mcp_server._ngrok_hostnames() == []


# --- readonly tool tiering -----------------------------------------------

# Every tool with a side effect. Adding a new @_mutating_tool() makes
# test_modes_differ_by_exactly_the_withheld_set fail until it's listed here —
# deliberately, so exposing a new mutator over the network is never silent.
WITHHELD = {
    "jenkins_trigger_build", "jenkins_apply_fix", "k8s_apply_fix",
    "tls_apply_fix", "ingress_apply_fix", "aws_apply_fix", "cost_apply_fix",
    "dns_scan",            # creates a probe pod in the cluster
    "security_drift",      # writes memory snapshots
    "incident_correlate",  # opens incidents, can fire Slack
}


def _tool_names(transport):
    env = {k: v for k, v in os.environ.items()
           if k not in ("MCP_TRANSPORT", "MCP_READONLY", "MCP_ALLOWED_HOSTS")}
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    if transport:
        env["MCP_TRANSPORT"] = transport
        env["MCP_ALLOWED_HOSTS"] = "test.invalid"  # skip the live ngrok lookup
    code = ("import asyncio,json;from agent.mcp_server import mcp;"
            "print(json.dumps(sorted(t.name for t in asyncio.run(mcp.list_tools()))))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


@pytest.fixture(scope="module")
def tool_sets():
    return {"stdio": _tool_names(None), "http": _tool_names("http")}


def test_http_mode_exposes_no_mutating_tool(tool_sets):
    assert not (WITHHELD & tool_sets["http"])


def test_stdio_mode_keeps_the_full_tool_set(tool_sets):
    assert WITHHELD <= tool_sets["stdio"]


def test_modes_differ_by_exactly_the_withheld_set(tool_sets):
    assert tool_sets["stdio"] - tool_sets["http"] == WITHHELD
    assert tool_sets["http"] <= tool_sets["stdio"]


def test_http_mode_refuses_to_start_without_a_token():
    env = {k: v for k, v in os.environ.items() if k != "MCP_AUTH_TOKEN"}
    env.update(PYTHONPATH=str(REPO_ROOT / "src"), MCP_TRANSPORT="http",
               MCP_ALLOWED_HOSTS="test.invalid",
               # load_dotenv() won't override a var that's already set, so this
               # stops .env from silently supplying the real token. Whitespace
               # rather than "" because Windows drops empty-valued env vars
               # from a child's environment — and it exercises the .strip().
               MCP_AUTH_TOKEN="   ")
    out = subprocess.run([sys.executable, "-m", "agent.mcp_server"], cwd=REPO_ROOT,
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode != 0
    assert "MCP_AUTH_TOKEN is required" in out.stderr + out.stdout
