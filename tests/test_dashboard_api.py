"""Dashboard API endpoints added for the redesign: status summary, cluster,
approvals, and the assistant's SSE stream — plus the command-dispatch paths
that used to import functions that don't exist.

The approval tests use fix_registry's `test` kind, which exists precisely so
the whole approve -> execute -> record path can run without touching
infrastructure. Everything else is faked.
"""
import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def server():
    """Import the server without leaking its MCP_READONLY default into the
    rest of the test session."""
    before = os.environ.get("MCP_READONLY")
    from agent.dashboard import server as srv
    if before is None:
        os.environ.pop("MCP_READONLY", None)
    else:
        os.environ["MCP_READONLY"] = before
    return srv


@pytest.fixture
def client(server):
    return TestClient(server.app)


@pytest.fixture
def isolated_approvals(tmp_path, monkeypatch):
    from agent.core import approvals
    monkeypatch.setattr(approvals, "_DB_PATH", tmp_path / "approvals.db")
    monkeypatch.setattr("agent.memory.retrieval.remember", lambda *a, **k: None)
    return approvals


# --- approvals ---------------------------------------------------------------

def test_pending_and_history_are_listed(client, isolated_approvals):
    a = isolated_approvals.create("test", "Restart api", {"marker": "x"})
    b = isolated_approvals.create("test", "Scale worker", {"marker": "y"})
    isolated_approvals.decide(b.id, "rejected", "someone")
    out = client.get("/api/approvals").json()
    assert [p["id"] for p in out["pending"]] == [a.id]
    assert [h["id"] for h in out["history"]] == [b.id]


def test_approve_runs_the_fix_and_records_it(client, isolated_approvals):
    a = isolated_approvals.create("test", "Prove the pipeline", {"marker": "dash"})
    r = client.post(f"/api/approvals/{a.id}/decide", json={"approve": True}).json()
    assert r["status"] == "applied" and r["result"]["marker"] == "dash"
    row = isolated_approvals.get(a.id)
    assert row.status == "applied" and row.decided_by == "dashboard"


def test_reject_runs_nothing(client, isolated_approvals, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("a rejected fix must not execute")
    monkeypatch.setattr("agent.core.fix_registry.execute", boom)
    a = isolated_approvals.create("test", "Nope", {"marker": "x"})
    r = client.post(f"/api/approvals/{a.id}/decide", json={"approve": False}).json()
    assert r["status"] == "rejected"
    assert isolated_approvals.get(a.id).status == "rejected"


def test_already_decided_is_not_run_twice(client, isolated_approvals):
    a = isolated_approvals.create("test", "Once", {"marker": "x"})
    client.post(f"/api/approvals/{a.id}/decide", json={"approve": True})
    r = client.post(f"/api/approvals/{a.id}/decide", json={"approve": True}).json()
    assert r["status"] == "applied" and "Already" in r["message"]


def test_expired_proposal_cannot_be_approved(client, isolated_approvals):
    a = isolated_approvals.create("test", "Old", {"marker": "x"}, ttl_minutes=-1)
    r = client.post(f"/api/approvals/{a.id}/decide", json={"approve": True}).json()
    assert r["status"] == "expired"


def test_unknown_approval_is_404(client, isolated_approvals):
    assert client.post("/api/approvals/nope/decide", json={"approve": True}).status_code == 404


# --- status summary ----------------------------------------------------------

def test_summary_is_cached_until_forced(client, server, monkeypatch):
    from agent.skills import daily_summary as ds
    calls = []

    def fake_build(now=None):
        calls.append(1)
        return [ds.Section("Cluster", "error", ["Couldn't reach the cluster."])]
    monkeypatch.setattr(ds, "build_summary", fake_build)
    monkeypatch.setitem(server._summary_cache, "data", None)

    first = client.get("/api/summary").json()
    client.get("/api/summary")
    assert len(calls) == 1
    assert first["sections"][0]["status"] == "error"
    assert first["headline"] == "Couldn't check: Cluster"
    client.get("/api/summary?force=true")
    assert len(calls) == 2


# --- digest (the Overview "Briefing" card: what's new, not what's current) ---

def test_digest_defaults_to_24h_and_reports_all_quiet(client, server, isolated_approvals, monkeypatch):
    server._digest_cache.clear()
    monkeypatch.setattr("agent.integrations.incident_db.list_incidents", lambda **k: [])
    monkeypatch.setattr("agent.memory.store.list_memories", lambda **k: [])
    out = client.get("/api/digest").json()
    assert out["had_previous"] is False
    assert out["headline"] == "All quiet — nothing new."
    assert out["incidents"] == {"total": 0, "open": 0, "resolved": 0, "items": []}
    assert out["approvals"] == {"pending_now": 0, "new": 0}


def test_digest_counts_whats_new(client, server, isolated_approvals, monkeypatch):
    server._digest_cache.clear()
    incidents = [
        {"id": "i1", "title": "pod crashlooping", "service": "api", "namespace": "prod", "status": "open"},
        {"id": "i2", "title": "disk full", "service": "db", "namespace": "prod", "status": "resolved"},
    ]
    monkeypatch.setattr("agent.integrations.incident_db.list_incidents", lambda **k: incidents)

    def fake_list_memories(sources=None, **k):
        if sources == ["deployment"]:
            return [SimpleNamespace(metadata={"success": True}), SimpleNamespace(metadata={"success": False})]
        return [SimpleNamespace(metadata={})] * 4   # the fixes/auto-heal sources
    monkeypatch.setattr("agent.memory.store.list_memories", fake_list_memories)
    isolated_approvals.create("test", "Restart api", {})

    out = client.get("/api/digest").json()
    assert out["incidents"] == {"total": 2, "open": 1, "resolved": 1,
                                "items": [{"id": "i1", "title": "pod crashlooping", "service": "api", "namespace": "prod", "status": "open"}]}
    assert out["deploys"] == {"total": 2, "failed": 1, "rollbacks": 0}
    assert out["fixes"] == {"total": 4}
    assert out["approvals"] == {"pending_now": 1, "new": 1}
    assert "2 incidents (1 auto-resolved)" in out["headline"]
    assert "2 deploys, 1 failed" in out["headline"]


def test_digest_isolates_a_failing_subsystem(client, server, isolated_approvals, monkeypatch):
    server._digest_cache.clear()
    def boom(**k):
        raise RuntimeError("incident db is locked")
    monkeypatch.setattr("agent.integrations.incident_db.list_incidents", boom)
    monkeypatch.setattr("agent.memory.store.list_memories", lambda **k: [])
    out = client.get("/api/digest").json()
    assert out["incidents"] == {"error": "incident db is locked"}
    assert out["deploys"] == {"total": 0, "failed": 0, "rollbacks": 0}   # one bad section doesn't zero out the rest


def test_digest_since_param_is_echoed_and_marks_had_previous(client, isolated_approvals, monkeypatch):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr("agent.integrations.incident_db.list_incidents", lambda **k: [])
    monkeypatch.setattr("agent.memory.store.list_memories", lambda **k: [])
    since = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    out = client.get("/api/digest", params={"since": since}).json()
    assert out["had_previous"] is True
    assert out["since"] == since


def test_digest_is_cached_per_since_value(client, server, isolated_approvals, monkeypatch):
    server._digest_cache.clear()
    calls = []

    def counting(**k):
        calls.append(1)
        return []
    monkeypatch.setattr("agent.integrations.incident_db.list_incidents", counting)
    monkeypatch.setattr("agent.memory.store.list_memories", lambda **k: [])

    client.get("/api/digest")
    client.get("/api/digest")   # same (empty) `since` — served from cache, no second scan
    assert len(calls) == 1

    from datetime import datetime, timedelta, timezone
    other_since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    client.get("/api/digest", params={"since": other_since})   # a different tab's window — not a cache hit
    assert len(calls) == 2


def test_digest_caps_a_stale_since_at_seven_days(client, isolated_approvals, monkeypatch):
    from datetime import datetime, timedelta, timezone
    seen_hours = {}

    def capture(since_hours=0, **k):
        seen_hours["v"] = since_hours
        return []
    monkeypatch.setattr("agent.integrations.incident_db.list_incidents", capture)
    monkeypatch.setattr("agent.memory.store.list_memories", lambda **k: [])
    ancient = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    client.get("/api/digest", params={"since": ancient})
    assert seen_hours["v"] <= 168.01


# --- cluster -----------------------------------------------------------------

def test_unreachable_cluster_is_reported_as_unreachable(client, monkeypatch):
    k = "agent.integrations.kubectl."
    monkeypatch.setattr(k + "get_current_context", lambda: "eks-prod")
    monkeypatch.setattr(k + "is_cluster_available", lambda: False)
    out = client.get("/api/cluster").json()
    assert out["reachable"] is False and out["context"] == "eks-prod"
    assert out["error"] and out["nodes"] == [] and out["problem_pods"] == []


def test_reachable_but_no_nodes_is_not_healthy(client, monkeypatch):
    k = "agent.integrations.kubectl."
    monkeypatch.setattr(k + "get_current_context", lambda: "eks-prod")
    monkeypatch.setattr(k + "is_cluster_available", lambda: True)
    monkeypatch.setattr(k + "get_nodes_detail", lambda: [])
    monkeypatch.setattr(k + "check_cluster_auth", lambda: (False, "Forbidden: cannot list nodes"))
    out = client.get("/api/cluster").json()
    assert out["reachable"] is False and "Forbidden" in out["error"]


# --- assistant SSE -----------------------------------------------------------

def test_chat_streams_events_as_sse(client, monkeypatch):
    from agent.dashboard import chat

    async def fake_send(session_id, text):
        yield {"type": "text", "delta": f"echo: {text}"}
        yield {"type": "done", "stop": "end_turn"}
    monkeypatch.setattr(chat, "send", fake_send)
    r = client.post("/api/chat", json={"session_id": "s1", "message": "hi"})
    assert r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert events == [{"type": "text", "delta": "echo: hi"}, {"type": "done", "stop": "end_turn"}]


def test_chat_stream_error_becomes_an_error_event(client, monkeypatch):
    from agent.dashboard import chat

    async def broken(session_id, text):
        yield {"type": "text", "delta": "partial"}
        raise RuntimeError("boom")
    monkeypatch.setattr(chat, "send", broken)
    r = client.post("/api/chat", json={"session_id": "s1", "message": "hi"})
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[-1]["type"] == "error" and "boom" in events[-1]["detail"]


def test_empty_chat_message_is_rejected(client):
    assert client.post("/api/chat", json={"session_id": "s", "message": "   "}).status_code == 400


# --- command dispatch fixes --------------------------------------------------

def test_k8s_scan_dispatches_to_the_real_skill(server, monkeypatch):
    monkeypatch.setattr("agent.integrations.kubectl.is_cluster_available", lambda: True)
    monkeypatch.setattr("agent.skills.k8s.K8sSkill.full_cluster_scan",
                        lambda self, ns="all": [{"resource": "api", "severity": "critical"}])
    assert server._execute_skill("k8s scan", {}) == {"issues": [{"resource": "api", "severity": "critical"}]}


@pytest.mark.parametrize("cmd", ["cost scan", "resources scan"])
def test_formerly_broken_commands_fall_through_to_the_cli(server, monkeypatch, cmd):
    ran = []
    monkeypatch.setattr("agent.integrations.kubectl.is_cluster_available", lambda: True)
    monkeypatch.setattr(server.subprocess, "run",
                        lambda parts, **k: ran.append(parts) or SimpleNamespace(stdout="ok", stderr=""))
    assert server._execute_skill(cmd, {}) == {"output": "ok"}
    assert ran[0][-2:] == cmd.split()
