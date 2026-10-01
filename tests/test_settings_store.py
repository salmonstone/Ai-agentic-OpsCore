"""Settings page backend: core/settings_store.py and the dashboard endpoints
for settings, first-run protection, and logs.

The OS keychain is faked in memory and .env is a temp file — nothing here
touches the real Credential Manager or the project's .env.
"""
import os

import pytest
from fastapi.testclient import TestClient

from agent.core import secrets as sec
from agent.core import settings_store as ss


class FakeKeyring:
    name = "FakeKeyring"

    def __init__(self):
        self.store = {}

    def get_password(self, service, key):
        return self.store.get((service, key))

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def delete_password(self, service, key):
        self.store.pop((service, key), None)


TOUCHED = ["SLACK_WEBHOOK_URL", "JENKINS_URL", "JENKINS_API_TOKEN", "AWS_REGION", "AWS_AUTH_METHOD",
           "DASHBOARD_TOKEN", "ANTHROPIC_API_KEY"]


@pytest.fixture
def kr(monkeypatch, tmp_path):
    fake = FakeKeyring()
    monkeypatch.setattr(sec, "_keyring", lambda: fake)
    monkeypatch.setattr(ss, "ENV_PATH", tmp_path / ".env")
    for name in TOUCHED:
        # setenv first so monkeypatch records the original and restores it even
        # when code under test sets the variable directly in os.environ
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    from agent.config import settings
    for name in TOUCHED:
        field = name.lower()
        if hasattr(settings, field):
            monkeypatch.setattr(settings, field, getattr(settings, field))
    return fake


# --- .env, one line at a time ------------------------------------------------

def test_set_env_line_keeps_other_lines_and_comments(kr):
    ss.ENV_PATH.write_text("# my comment\nFOO=1\nJENKINS_URL=http://old\nBAR=2\n", encoding="utf-8")
    ss.set_env_line("JENKINS_URL", "http://new:8080")
    assert ss.ENV_PATH.read_text(encoding="utf-8") == "# my comment\nFOO=1\nJENKINS_URL=http://new:8080\nBAR=2\n"


def test_set_env_line_appends_and_removes(kr):
    ss.ENV_PATH.write_text("FOO=1\n", encoding="utf-8")
    ss.set_env_line("AWS_REGION", "ap-south-1")
    assert "AWS_REGION=ap-south-1" in ss.ENV_PATH.read_text(encoding="utf-8")
    ss.set_env_line("AWS_REGION", None)
    assert ss.ENV_PATH.read_text(encoding="utf-8") == "FOO=1\n"


def test_values_with_spaces_are_quoted_and_read_back(kr):
    ss.set_env_line("JENKINS_URL", "a b#c")
    assert ss._env_value("JENKINS_URL") == "a b#c"


# --- describe: never leaks a secret --------------------------------------------

def test_describe_never_returns_secret_values(kr):
    sec.store("JENKINS_API_TOKEN", "super-secret-value")
    ss.set_env_line("JENKINS_URL", "http://jenkins:8080")
    out = ss.describe()
    assert "super-secret-value" not in repr(out)
    jenkins = next(i for i in out["integrations"] if i["id"] == "jenkins")
    token = next(f for f in jenkins["fields"] if f["name"] == "JENKINS_API_TOKEN")
    url = next(f for f in jenkins["fields"] if f["name"] == "JENKINS_URL")
    assert token == {**token, "secret": True, "set": True, "where": "keychain"} and "value" not in token
    assert url["value"] == "http://jenkins:8080"


def test_plaintext_secret_in_env_is_flagged(kr):
    ss.set_env_line("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    slack = next(i for i in ss.describe()["integrations"] if i["id"] == "slack")
    f = next(f for f in slack["fields"] if f["name"] == "SLACK_WEBHOOK_URL")
    assert f["where"] == ".env" and "plaintext" in f["warning"]


# --- save -------------------------------------------------------------------------

def test_save_puts_secrets_in_keychain_and_settings_in_env(kr):
    ss.save({"JENKINS_API_TOKEN": "tok-123", "JENKINS_URL": "http://j:8080"})
    assert sec.get("JENKINS_API_TOKEN") == "tok-123"
    assert "tok-123" not in (ss.ENV_PATH.read_text(encoding="utf-8") if ss.ENV_PATH.exists() else "")
    assert ss._env_value("JENKINS_URL") == "http://j:8080"
    from agent.config import settings
    assert settings.jenkins_url == "http://j:8080" and os.environ["JENKINS_API_TOKEN"] == "tok-123"


def test_saving_a_secret_removes_its_plaintext_copy(kr):
    ss.set_env_line("SLACK_WEBHOOK_URL", "https://old")
    notes = ss.save({"SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/new"})
    assert ss._env_value("SLACK_WEBHOOK_URL") is None
    assert sec.get("SLACK_WEBHOOK_URL") == "https://hooks.slack.com/services/new"
    assert any("plaintext" in n for n in notes)


def test_empty_value_means_unchanged(kr):
    sec.store("JENKINS_API_TOKEN", "keep-me")
    ss.save({"JENKINS_API_TOKEN": ""})
    assert sec.get("JENKINS_API_TOKEN") == "keep-me"


def test_clear_removes_from_keychain_and_env(kr):
    ss.save({"JENKINS_API_TOKEN": "x", "AWS_REGION": "eu-west-1"})
    ss.save({}, clear=["JENKINS_API_TOKEN", "AWS_REGION"])
    assert sec.get("JENKINS_API_TOKEN") is None and ss._env_value("AWS_REGION") is None
    assert "JENKINS_API_TOKEN" not in os.environ


def test_only_known_settings_can_be_written(kr):
    with pytest.raises(ss.SettingsError, match="Not an AtlasOS setting"):
        ss.save({"PATH": "C:\\evil"})
    with pytest.raises(ss.SettingsError):
        ss.save({}, clear=["DASHBOARD_TOKEN"])


def test_newlines_are_rejected(kr):
    with pytest.raises(ss.SettingsError, match="newline"):
        ss.save({"JENKINS_URL": "http://x\nEVIL=1"})


def test_aws_auth_method_is_validated(kr):
    with pytest.raises(ss.SettingsError):
        ss.save({"AWS_AUTH_METHOD": "magic"})
    ss.save({"AWS_AUTH_METHOD": "sso_profile"})
    assert ss._env_value("AWS_AUTH_METHOD") == "sso_profile"


def test_check_reports_off_when_not_configured(kr):
    assert ss.check("anthropic")["status"] == "off"
    assert ss.check("paging")["status"] == "off"
    with pytest.raises(ss.SettingsError):
        ss.check("nope")


# --- endpoints ------------------------------------------------------------------

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
def client(server, kr):
    return TestClient(server.app)


def test_saving_is_refused_until_the_dashboard_is_protected(client, kr):
    r = client.post("/api/settings", json={"values": {"JENKINS_URL": "http://x"}})
    assert r.status_code == 403 and "Protect" in r.json()["detail"]
    assert ss._env_value("JENKINS_URL") is None


def test_protect_creates_token_logs_in_and_unlocks_saving(client, kr):
    r = client.post("/api/settings/protect")
    assert r.status_code == 200
    token = r.json()["token"]
    assert sec.get("DASHBOARD_TOKEN") == token and "atlas_session" in r.headers["set-cookie"]
    # this client now carries the cookie, so saving works
    assert client.post("/api/settings", json={"values": {"JENKINS_URL": "http://j"}}).status_code == 200
    # a second protect is refused — it can't be used to take over the token
    assert client.post("/api/settings/protect").status_code == 409


def test_without_the_cookie_settings_are_locked_once_protected(server, kr):
    TestClient(server.app).post("/api/settings/protect")
    stranger = TestClient(server.app)
    assert stranger.get("/api/settings").status_code == 401
    assert stranger.post("/api/settings", json={"values": {"JENKINS_URL": "http://evil"}}).status_code == 401


def test_get_settings_has_no_secret_values(client, kr):
    sec.store("ANTHROPIC_API_KEY", "sk-ant-should-never-appear")
    r = client.get("/api/settings")
    assert r.status_code == 200 and "sk-ant-should-never-appear" not in r.text and r.json()["protected"] is False


def test_system_logs_are_whitelisted_and_redacted(client, server, kr, tmp_path, monkeypatch):
    log = tmp_path / "daemon.log"
    log.write_text("\x1b[2mstart\x1b[0m\nusing token=abc123secret and sk-ant-api03-ABCDEFGHIJKL\nAKIAABCDEFGHIJKLMNOP ok\n", encoding="utf-8")
    monkeypatch.setitem(server._SYSTEM_LOGS, "daemon", ("Daemon", log))
    listing = client.get("/api/logs/system").json()["logs"]
    assert any(item["name"] == "daemon" and item["exists"] for item in listing)
    text = client.get("/api/logs/system?name=daemon").json()["text"]
    assert "abc123secret" not in text and "ABCDEFGHIJKL" not in text and "AKIAABCDEFGHIJKLMNOP" not in text
    assert text.startswith("start\n") and "\x1b" not in text      # terminal colour codes stripped
    assert client.get("/api/logs/system?name=../../.env").status_code == 404


def test_pod_logs_report_an_unreachable_cluster(client, monkeypatch):
    monkeypatch.setattr("agent.integrations.kubectl.is_cluster_available", lambda: False)
    r = client.get("/api/logs/pod?namespace=prod&pod=api")
    assert r.status_code == 503 and "unreachable" in r.json()["error"]
