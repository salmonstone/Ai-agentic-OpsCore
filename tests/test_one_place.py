"""Everything-in-one-place endpoints: setup checklist, SLOs, token rotation,
GitHub deploy webhook, runbooks, scaling, domains, databases, daily summary,
activity and search.

Every store is a temp file; cluster, AWS, Slack and the skills that act on
infrastructure are faked. The audit trail is collected by conftest's
`audit_log` fixture.
"""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.integrations import autoscale_db, incident_db, runbook_db, slo_db


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                       # .env, data/*.yaml land here
    monkeypatch.setattr(slo_db, "_DB_PATH", tmp_path / "slo.db")
    monkeypatch.setattr(autoscale_db, "_DB_PATH", tmp_path / "autoscale.db")
    monkeypatch.setattr(runbook_db, "_DB_PATH", tmp_path / "runbook.db")
    monkeypatch.setattr(incident_db, "_DB_PATH", tmp_path / "incident.db")
    monkeypatch.setattr("agent.integrations.mapping_loader._MAPPINGS_FILE", tmp_path / "mappings.yaml")
    from agent.integrations.mapping_loader import get_loader
    monkeypatch.setattr(get_loader(), "_cache", None, raising=False)
    from agent.config import settings
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "memory.db"))
    monkeypatch.setattr("agent.core.supervisor.ngrok_hosts", lambda: ["abc.ngrok-free.app"])
    monkeypatch.setattr("agent.skills.runbook._RUNBOOKS_PATH", tmp_path / "runbooks.yaml")
    (tmp_path / "runbooks.yaml").write_text(
        "runbooks:\n"
        "  - id: restart-it\n    name: Restart it\n    description: restart a deployment\n"
        "    steps:\n"
        "      - name: restart\n        type: kubectl\n        command: rollout restart deployment/{deployment_name} -n {namespace}\n"
        "      - name: tell\n        type: slack\n        message: restarted {deployment_name}\n",
        encoding="utf-8")


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


@pytest.fixture
def logged_in(monkeypatch, client):
    """Turn the login on with a known token and log this client in."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "t0ken")
    from agent.dashboard import security
    monkeypatch.setattr(security, "_keychain_token", lambda: "")
    client.cookies.set(security.COOKIE, "t0ken")
    return client


# --- 1. setup checklist ------------------------------------------------------------

def test_setup_lists_steps_and_tracks_what_is_done(client):
    steps = {s["id"]: s for s in client.get("/api/setup").json()["steps"]}
    assert {"protect", "claude", "slack", "ci", "aws", "webhook", "slo", "daemon"} <= set(steps)
    assert steps["slo"]["done"] is False and steps["slo"]["optional"] is True
    client.post("/api/slos", json={"name": "api up", "service": "api"})
    assert {s["id"]: s for s in client.get("/api/setup").json()["steps"]}["slo"]["done"] is True


# --- 3. SLOs -------------------------------------------------------------------------

def test_slo_create_list_delete(client, audit_log):
    r = client.post("/api/slos", json={"name": "API up", "service": "api", "namespace": "prod", "target_pct": 99.5, "window_days": 30})
    sid = r.json()["id"]
    [s] = client.get("/api/slos").json()
    assert s["service"] == "api" and s["target_pct"] == 99.5 and s["budget_pct"] == 100
    assert client.post("/api/slos", json={"name": "dup", "service": "api", "namespace": "prod"}).status_code == 409
    assert client.delete(f"/api/slos/{sid}").json() == {"deleted": True}
    assert client.get("/api/slos").json() == []
    assert client.delete(f"/api/slos/{sid}").status_code == 404
    assert any("created SLO" in l for l in audit_log) and any("deleted SLO" in l for l in audit_log)


@pytest.mark.parametrize("body", [
    {"name": "", "service": "api"},
    {"name": "x", "service": "API Gateway"},
    {"name": "x", "service": "api", "target_pct": 100},
    {"name": "x", "service": "api", "window_days": 365},
])
def test_slo_rejects_bad_input(client, body):
    assert client.post("/api/slos", json=body).status_code == 400


# --- 4. token rotation -----------------------------------------------------------------

def test_rotate_needs_an_existing_login(client):
    assert client.post("/api/settings/rotate-token").status_code == 409


def test_rotate_replaces_the_token_and_keeps_this_browser_in(logged_in, monkeypatch, audit_log):
    from agent.core import secrets as sec
    stored = {}
    monkeypatch.setattr(sec, "store", lambda name, value: stored.update({name: value}))
    r = logged_in.post("/api/settings/rotate-token")
    new = r.json()["token"]
    assert new and new != "t0ken" and stored == {"DASHBOARD_TOKEN": new}
    assert os.environ["DASHBOARD_TOKEN"] == new
    assert logged_in.get("/api/auth/status").json() == {"required": True, "ok": True}   # new cookie set
    other = TestClient(logged_in.app)
    other.cookies.set("atlas_session", "t0ken")
    assert other.get("/api/setup").status_code == 401                                    # old token is dead
    assert any("rotated" in l for l in audit_log)


def test_rotate_requires_being_logged_in(monkeypatch, client):
    monkeypatch.setenv("DASHBOARD_TOKEN", "t0ken")
    from agent.dashboard import security
    monkeypatch.setattr(security, "_keychain_token", lambda: "")
    assert client.post("/api/settings/rotate-token").status_code == 401


# --- 2. deploys from GitHub ------------------------------------------------------------

def test_deploy_setup_shows_the_public_webhook_url(client):
    d = client.get("/api/deploys/setup").json()
    assert d["urls"] == ["https://abc.ngrok-free.app/webhook/github"]
    assert d["mappings"] == [] and "secret_set" in d


def test_webhook_secret_needs_protection_then_is_shown_once(client, logged_in, monkeypatch):
    from agent.core import settings_store
    saved = {}
    monkeypatch.setattr(settings_store, "save", lambda values, clear=None: saved.update(values) or [])
    have = {"set": False}
    monkeypatch.setattr(settings_store, "_where", lambda name: ".env" if have["set"] and name == "GITHUB_WEBHOOK_SECRET" else None)
    monkeypatch.delenv("DASHBOARD_TOKEN")
    assert client.post("/api/deploys/secret", json={}).status_code == 403
    monkeypatch.setenv("DASHBOARD_TOKEN", "t0ken")
    r = logged_in.post("/api/deploys/secret", json={})
    assert r.status_code == 200 and saved["GITHUB_WEBHOOK_SECRET"] == r.json()["secret"]
    have["set"] = True                                    # an existing secret isn't silently replaced
    assert logged_in.post("/api/deploys/secret", json={}).status_code == 409
    assert logged_in.post("/api/deploys/secret", json={"replace": True}).status_code == 200


def test_add_and_remove_mapping(client, monkeypatch, audit_log):
    from agent.integrations.mapping_loader import ValidationResult, get_loader
    monkeypatch.setattr(get_loader(), "validate_mapping", lambda m: ValidationResult(False, ["deployment api not found"]))
    r = client.post("/api/deploys/mappings", json={"repo": "o/r", "deployment": "api", "namespace": "prod"})
    assert r.json() == {"saved": True, "warnings": ["deployment api not found"]}
    assert client.get("/api/deploys/setup").json()["mappings"][0]["deployment"] == "api"
    assert client.request("DELETE", "/api/deploys/mappings", params={"repo": "o/r", "branch": "main"}).json() == {"removed": True}
    assert client.get("/api/deploys/setup").json()["mappings"] == []
    assert len([l for l in audit_log if "mapping" in l]) == 2


@pytest.mark.parametrize("body", [
    {"repo": "not-a-repo", "deployment": "api"},
    {"repo": "o/r", "deployment": "Api Server"},
    {"repo": "o/r", "deployment": "api", "image_prefix": "x; rm -rf /"},
])
def test_mapping_rejects_bad_input(client, body):
    assert client.post("/api/deploys/mappings", json=body).status_code == 400


# --- 5. runbooks -------------------------------------------------------------------------

def test_runbook_catalog_lists_steps_and_variables(client):
    [rb] = client.get("/api/runbooks/catalog").json()["runbooks"]
    assert rb["id"] == "restart-it" and rb["vars"] == ["deployment_name", "namespace"]
    assert [s["type"] for s in rb["steps"]] == ["kubectl", "slack"]


def test_runbook_run_passes_only_its_own_plain_variables(client, monkeypatch, audit_log):
    seen = {}
    monkeypatch.setattr("agent.skills.runbook.run_runbook",
                        lambda rid, context, trigger: seen.update(rid=rid, ctx=context, trigger=trigger) or {"status": "success", "steps": []})
    r = client.post("/api/runbooks/restart-it/run", json={"context": {"deployment_name": "api", "namespace": "prod"}})
    assert r.json()["status"] == "success"
    assert seen == {"rid": "restart-it", "ctx": {"deployment_name": "api", "namespace": "prod"}, "trigger": "dashboard"}
    assert any("ran runbook restart-it" in l for l in audit_log)
    # a value with a space could smuggle extra kubectl arguments
    bad = client.post("/api/runbooks/restart-it/run", json={"context": {"deployment_name": "api --all-namespaces"}})
    assert bad.status_code == 400
    assert client.post("/api/runbooks/restart-it/run", json={"context": {"pvc_name": "x"}}).status_code == 400
    assert client.post("/api/runbooks/nope/run", json={}).status_code == 404


# --- 6. scaling ----------------------------------------------------------------------------

def test_scale_policy_add_disable_enable(client, audit_log):
    pid = client.post("/api/scale", json={"deployment": "api", "namespace": "prod", "down_time": "18:00", "up_time": "04:00",
                                          "down_replicas": 1, "up_replicas": 4}).json()["id"]
    [p] = client.get("/api/scale").json()["policies"]
    assert p["schedule_down_utc"] == "18:00" and p["up_replicas"] == 4 and p["enabled"] == 1
    assert client.post(f"/api/scale/{pid}/disable").json() == {"enabled": False}
    assert client.get("/api/scale").json()["policies"][0]["enabled"] == 0
    client.post(f"/api/scale/{pid}/enable")
    assert client.get("/api/scale").json()["policies"][0]["enabled"] == 1
    assert client.post(f"/api/scale/{pid}/delete").status_code == 404
    assert len([l for l in audit_log if "scaling policy" in l]) == 3


@pytest.mark.parametrize("body", [
    {"deployment": "api", "down_time": "25:00"},
    {"deployment": "api", "up_replicas": 0},
    {"deployment": "api", "cpu_pct": 5},
])
def test_scale_rejects_bad_input(client, body):
    assert client.post("/api/scale", json=body).status_code == 400


# --- 7. domains -------------------------------------------------------------------------------

def test_domains_say_unreachable_instead_of_nothing(client, monkeypatch):
    monkeypatch.setattr("agent.integrations.kubectl.is_cluster_available", lambda: False)
    assert client.get("/api/domains").json() == {"reachable": False, "results": []}


def test_domain_results_are_made_json_safe(client, monkeypatch):
    monkeypatch.setattr("agent.integrations.kubectl.is_cluster_available", lambda: True)
    ing = SimpleNamespace(name="web", namespace="prod", domain="app.example.com")
    monkeypatch.setattr("agent.skills.domain.DomainSkill.live",
                        lambda self, d=None, ns=None: [{"found": True, "domain": "app.example.com", "ingress": ing, "checks": {}, "diagnosis": {}}])
    [r] = client.get("/api/domains").json()["results"]
    assert r["ingress"]["name"] == "web"


@pytest.mark.parametrize("body", [
    {"domain": "not a domain", "email": "a@b.co"},
    {"domain": "app.example.com", "email": "nope"},
    {"domain": "app.example.com", "email": "a@b.co", "namespace": "Prod NS"},
])
def test_encrypt_rejects_bad_input(client, body):
    assert client.post("/api/domains/encrypt", json=body).status_code == 400


def test_encrypt_runs_the_skill(client, monkeypatch, audit_log):
    monkeypatch.setattr("agent.integrations.kubectl.is_cluster_available", lambda: True)
    got = {}
    monkeypatch.setattr("agent.skills.domain.DomainSkill.encrypt",
                        lambda self, **kw: got.update(kw) or {"ok": True, "steps": [{"label": "cert", "ok": True}]})
    r = client.post("/api/domains/encrypt", json={"domain": "App.Example.com", "email": "me@example.com", "namespace": "prod"})
    assert r.json()["ok"] is True and got["domain"] == "app.example.com" and got["staging"] is False
    assert any("Let's Encrypt" in l for l in audit_log)


# --- 8. databases --------------------------------------------------------------------------------

def test_databases_unreachable_is_not_no_databases(client, server, monkeypatch):
    server._db_cache.update(data=None, at=0.0)
    def boom(*a, **k):
        raise RuntimeError("ExpiredToken")
    monkeypatch.setattr("agent.integrations.aws.get_aws_client", boom)
    d = client.get("/api/databases").json()
    assert d["reachable"] is False and d["error"]


def test_databases_scan(client, server, monkeypatch):
    server._db_cache.update(data=None, at=0.0)
    monkeypatch.setattr("agent.integrations.aws.get_aws_client",
                        lambda *a, **k: SimpleNamespace(get_caller_identity=lambda: {"Account": "1"}))
    monkeypatch.setattr("agent.skills.db_health.scan_all_databases",
                        lambda region: [{"instance": {"id": "prod-pg"}, "metrics": {}, "issues": [], "status": "healthy", "incident_id": "", "cluster": None}])
    d = client.get("/api/databases").json()
    assert d["reachable"] and d["databases"][0]["instance"]["id"] == "prod-pg"


# --- 9. daily summary --------------------------------------------------------------------------------

def test_summary_send_now(client, monkeypatch, audit_log):
    calls = []
    monkeypatch.setattr("agent.skills.daily_summary.send", lambda force=False: calls.append(force) or {"sent": True, "headline": "All clear."})
    assert client.post("/api/summary/send", json={"force": True}).json()["sent"] is True
    assert calls == [True] and any("daily summary sent" in l for l in audit_log)


def test_summary_info_has_history_from_the_inbox(client):
    from agent.integrations import notifications
    notifications.record("☀️ AtlasOS daily summary — Wed", "", severity="info", kind="summary")
    notifications.record("CrashLoopBackOff", "x", severity="critical", kind="alert")
    d = client.get("/api/summary/info").json()
    assert [h["title"] for h in d["history"]] == ["☀️ AtlasOS daily summary — Wed"]
    assert "available" in d["schedule"]


# --- 10. activity --------------------------------------------------------------------------------------

def test_activity_shows_actions_by_default_and_everything_on_request(client):
    from agent.memory import store
    store.save_memory("Approval 12 approved by aditya", source="approvals")
    store.save_memory("Scanned 40 pods, all fine", source="k8s-diagnose")
    store.save_memory("key=sk-ant-api03-SECRETSECRETSECRET", source="dashboard")
    acts = client.get("/api/activity").json()
    assert {i["source"] for i in acts["items"]} == {"approvals", "dashboard"}
    assert "SECRETSECRET" not in str(acts)                                   # redacted
    assert len(client.get("/api/activity", params={"view": "all"}).json()["items"]) == 3
    assert [i["source"] for i in client.get("/api/activity", params={"view": "all", "q": "40 pods"}).json()["items"]] == ["k8s-diagnose"]
    assert acts["sources"]["approvals"] == 1


# --- 12. search ------------------------------------------------------------------------------------------

def test_search_finds_records_across_stores(client, monkeypatch, server):
    incident_db.open_incident("payments crashlooping", "critical", "payments", "prod", cause="x")
    slo_db.create_slo("payments up", "payments", "prod")
    client.post("/api/scale", json={"deployment": "payments", "namespace": "prod"})
    monkeypatch.setitem(server._aws_cache, "data", {"inventory": {"ec2": [{"id": "i-1", "name": "payments-box", "instance_type": "t3", "state": "running"}]}})
    kinds = {r["kind"] for r in client.get("/api/search", params={"q": "payments"}).json()["results"]}
    assert {"Incident", "SLO", "Scaling policy", "EC2"} <= kinds
    assert client.get("/api/search", params={"q": "p"}).json()["results"] == []
