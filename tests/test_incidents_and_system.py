"""Incident Acknowledge/Resolve and the System page.

Resolve is one shared path (skills/incident.resolve_incident) for the
healer, the CLI and the dashboard — and it now also closes the on-call
page, which previously stayed open after the incident was resolved.
Stores are temp files; Slack, PagerDuty and backups are faked or temp.
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.integrations import incident_db, notifications


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_db, "_DB_PATH", tmp_path / "incident.db")
    monkeypatch.setattr("agent.integrations.event_queue._DB_PATH", str(tmp_path / "event_queue.db"))
    monkeypatch.setattr("agent.skills.incident._webhook_url", lambda: "")
    monkeypatch.setenv("ATLASOS_BACKUP_DIR", str(tmp_path / "backups"))
    pages = []
    monkeypatch.setattr("agent.integrations.pagerduty.resolve_oncall", lambda iid: pages.append(iid) or True)
    return pages


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
def client(server):
    return TestClient(server.app)


def _incident(title="api crashlooping"):
    return incident_db.open_incident(title, "critical", "api", "prod", cause="CrashLoopBackOff")


# --- incidents ---------------------------------------------------------------------

def test_acknowledge_adds_a_timeline_event_and_keeps_it_open(client):
    iid = _incident()
    assert client.post(f"/api/incidents/{iid}/ack").json() == {"acknowledged": True}
    inc = client.get(f"/api/incidents/{iid}").json()
    assert inc["status"] == "open"
    assert any(e["event_type"] == "acknowledged" and "dashboard" in e["detail"] for e in inc["events"])


def test_resolve_closes_the_page_and_records_it(client, isolated):
    iid = _incident()
    assert client.post(f"/api/incidents/{iid}/resolve", json={"note": "rotated the db secret"}).json()["resolved"]
    inc = incident_db.get_incident(iid)
    assert inc["status"] == "resolved" and inc["auto_fixed"] == 0 and "rotated" in inc["notes"]
    assert isolated == [iid]                                     # on-call page closed with the same key
    assert any(e["event_type"] == "page_resolved" for e in inc["events"])
    assert notifications.list_recent()["items"][0]["title"].startswith("Resolved:")


def test_resolving_twice_is_harmless(client, isolated):
    iid = _incident()
    client.post(f"/api/incidents/{iid}/resolve", json={})
    assert client.post(f"/api/incidents/{iid}/resolve", json={}).json() == {"resolved": True, "already": True}
    assert isolated == [iid]


def test_cannot_acknowledge_a_resolved_or_unknown_incident(client):
    iid = _incident()
    client.post(f"/api/incidents/{iid}/resolve", json={})
    assert client.post(f"/api/incidents/{iid}/ack").status_code == 409
    assert client.post("/api/incidents/nope/ack").status_code == 409
    assert client.get("/api/incidents/nope").status_code == 404


def test_the_healer_path_still_marks_auto_fixed(isolated):
    from agent.skills.incident import resolve_incident
    iid = _incident()
    resolve_incident(iid, cause="auto-healed")
    assert incident_db.get_incident(iid)["auto_fixed"] == 1 and isolated == [iid]


# --- system -------------------------------------------------------------------------

def test_system_reports_services_queue_and_backups(client, monkeypatch):
    monkeypatch.setattr("agent.core.supervisor.running_supervisor_pid", lambda *a, **k: None)
    from agent.integrations import event_queue as eq
    eq.enqueue("deploy.push", {"repo": "o/r"})
    out = client.get("/api/system").json()
    assert out["supervisor"]["running"] is False and out["supervisor"]["services"] == {}
    assert out["queue"]["stats"]["pending"] == 1 and out["queue"]["oldest_pending"]
    assert out["backups"]["items"] == [] and out["dashboard"]["pid"] == os.getpid()


def test_backup_now_and_verify(client):
    r = client.post("/api/system/backup")
    assert r.status_code == 200
    name = r.json()["name"]
    assert [b["name"] for b in client.get("/api/system").json()["backups"]["items"]] == [name]
    assert client.post("/api/system/backup/verify", json={"name": name}).json()["ok"] is True


def test_verify_only_accepts_listed_backups_not_paths(client, tmp_path):
    secret = tmp_path / "not-a-backup.txt"
    secret.write_text("x", encoding="utf-8")
    assert client.post("/api/system/backup/verify", json={"name": str(secret)}).status_code == 404
    assert client.post("/api/system/backup/verify", json={"name": "../../.env"}).status_code == 404


def test_retry_dead_event(client):
    from agent.integrations import event_queue as eq
    eid = eq.enqueue("deploy.push", {"repo": "o/r"})
    eq.fail(eid, "boom", retry=False)
    assert client.get("/api/system").json()["queue"]["stats"]["dead"] == 1
    assert client.post(f"/api/system/events/{eid}/retry").json() == {"requeued": True}
    assert client.post("/api/system/events/nope/retry").status_code == 404
