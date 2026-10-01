"""Dashboard request guard (agent/dashboard/security.py).

Replaces CORS allow_origins=["*"]: a page on another site must not be able to
make the operator's browser change anything on the local dashboard, and with
DASHBOARD_TOKEN set nothing under /api or /ws works without logging in.
"""
import os

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture(scope="module")
def server():
    before = os.environ.get("MCP_READONLY")
    from agent.dashboard import server as srv
    if before is None:
        os.environ.pop("MCP_READONLY", None)
    else:
        os.environ["MCP_READONLY"] = before
    return srv


@pytest.fixture
def client(server, monkeypatch, tmp_path):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("DASHBOARD_ALLOWED_ORIGINS", raising=False)
    from agent.core import approvals
    monkeypatch.setattr(approvals, "_DB_PATH", tmp_path / "approvals.db")
    return TestClient(server.app)


DECIDE = "/api/approvals/nope/decide"


# --- cross-site guard ----------------------------------------------------------

def test_cross_site_post_is_blocked(client):
    r = client.post(DECIDE, json={"approve": True}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "Cross-site" in r.json()["detail"]


def test_same_origin_post_passes_the_guard(client):
    r = client.post(DECIDE, json={"approve": True}, headers={"Origin": "http://testserver"})
    assert r.status_code == 404          # reached the endpoint (unknown approval id)


def test_no_origin_post_passes(client):
    """curl / the CLI send no Origin header."""
    assert client.post(DECIDE, json={"approve": True}).status_code == 404


def test_null_origin_is_cross_site(client):
    assert client.post(DECIDE, json={"approve": True}, headers={"Origin": "null"}).status_code == 403


def test_vite_dev_server_origin_is_allowed(client):
    r = client.post(DECIDE, json={"approve": True}, headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 404


def test_extra_allowed_origin(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ALLOWED_ORIGINS", "https://atlas.example.com")
    r = client.post(DECIDE, json={"approve": True}, headers={"Origin": "https://atlas.example.com"})
    assert r.status_code == 404


def test_cross_site_get_is_not_blocked_by_the_guard(client):
    """Reads are protected by the browser (no CORS headers are sent any more)."""
    r = client.get("/api/approvals", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_cross_site_websocket_is_refused(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/execution/x", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_text()


# --- optional login ---------------------------------------------------------------

@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "s3cret-token")
    return "s3cret-token"


def test_without_a_token_configured_no_login_is_needed(client):
    assert client.get("/api/auth/status").json() == {"required": False, "ok": True}
    assert client.get("/api/approvals").status_code == 200


def test_with_a_token_api_needs_login(client, token):
    assert client.get("/api/approvals").status_code == 401
    assert client.get("/api/auth/status").json() == {"required": True, "ok": False}


def test_wrong_token_is_refused(client, token):
    r = client.post("/api/login", json={"token": "nope"})
    assert r.status_code == 401 and "atlas_session" not in r.cookies


def test_login_sets_a_cookie_that_works(client, token):
    r = client.post("/api/login", json={"token": token})
    assert r.status_code == 200 and r.json()["ok"] is True
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie
    assert client.get("/api/approvals").status_code == 200
    assert client.get("/api/auth/status").json()["ok"] is True


def test_bearer_header_works(client, token):
    assert client.get("/api/approvals", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_logout_clears_the_session(client, token):
    client.post("/api/login", json={"token": token})
    client.post("/api/logout")
    assert client.get("/api/approvals").status_code == 401


def test_websocket_needs_login_too(client, token):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/execution/x") as ws:
            ws.receive_text()


# --- spend chart --------------------------------------------------------------------

def test_spend_endpoint_shapes_and_caches(client, server, monkeypatch):
    calls = []

    def fake(days):
        calls.append(days)
        return {"daily_totals": [{"date": "2026-09-28", "amount": 30.0}], "mean_daily": 15.0,
                "anomaly_days": [{"date": "2026-09-28"}]}
    monkeypatch.setattr("agent.integrations.aws_cost.get_spend_anomalies", fake)
    monkeypatch.setitem(server._spend_cache, "data", None)
    out = client.get("/api/spend").json()
    client.get("/api/spend")
    assert out["daily"] == [{"date": "2026-09-28", "amount": 30.0}]
    assert out["mean"] == 15.0 and out["anomalies"] == ["2026-09-28"]
    assert calls == [30]
