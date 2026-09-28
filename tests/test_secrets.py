"""Secrets: .env -> OS keychain migration, loading, and undo.

Uses an in-memory fake keychain; the real Windows Credential Manager is only
touched by one test, with a throwaway name it deletes again.
"""
import os
import uuid

import pytest
from dotenv import dotenv_values

from agent.core import secrets as sec

ENV_TEXT = """\
# AtlasOS config
ANTHROPIC_API_KEY=sk-ant-REAL-VALUE-1
JENKINS_URL=http://jenkins.local:8080
JENKINS_USER=aditya
JENKINS_API_TOKEN="tok en #2 with spaces"
AWS_AUTH_METHOD=access_key
AWS_ACCESS_KEY_ID=AKIAEXAMPLE
AWS_SECRET_ACCESS_KEY='abc/def+ghi'
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T/B/X
MCP_AUTH_TOKEN=IsviHxoM46GO
EMPTY_API_KEY=
LLM_MAX_TOKENS=4096
"""
SECRETS = {"ANTHROPIC_API_KEY", "JENKINS_API_TOKEN", "AWS_ACCESS_KEY_ID",
           "AWS_SECRET_ACCESS_KEY", "SLACK_WEBHOOK_URL", "MCP_AUTH_TOKEN"}


class FakeKeychain:
    name = "fake"

    def __init__(self):
        self.store = {}
        self.corrupt = False

    def get_password(self, service, key):
        v = self.store.get((service, key))
        return (v + "!") if (v and self.corrupt and key != sec.INDEX_KEY) else v

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def delete_password(self, service, key):
        self.store.pop((service, key), None)


@pytest.fixture
def kc(monkeypatch):
    fake = FakeKeychain()
    monkeypatch.setattr(sec, "_keyring", lambda: fake)
    return fake


@pytest.fixture
def env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text(ENV_TEXT, encoding="utf-8")
    return p


# --- which names are secrets ----------------------------------------------

@pytest.mark.parametrize("name", sorted(SECRETS) + [
    "VOYAGE_API_KEY", "GITHUB_WEBHOOK_SECRET", "SLACK_SIGNING_SECRET",
    "PAGERDUTY_ROUTING_KEY", "OPSGENIE_API_KEY", "DB_PASSWORD", "GROQ_API_KEY"])
def test_secret_names(name):
    assert sec.is_secret_name(name)


@pytest.mark.parametrize("name", [
    "JENKINS_URL", "JENKINS_USER", "AWS_REGION", "AWS_AUTH_METHOD", "AWS_PROFILE",
    "LLM_MODEL", "LLM_MAX_TOKENS", "GMAIL_CREDENTIALS_FILE", "SLACK_DEFAULT_CHANNEL",
    "WEBHOOK_HOST", "MCP_PORT", "TOKEN_TTL"])
def test_non_secret_names(name):
    assert not sec.is_secret_name(name)


# --- migrate ----------------------------------------------------------------

def test_migrate_moves_secrets_and_keeps_everything_else(kc, env_file):
    before = dotenv_values(env_file)
    moved = sec.migrate(env_file)

    assert set(moved) == SECRETS
    for name in SECRETS:
        assert sec.get(name) == before[name]
    after_text = env_file.read_text(encoding="utf-8")
    for name in SECRETS:
        assert before[name] not in after_text                  # value gone from disk
        assert sec.MOVED_MARKER.format(name=name) in after_text
    after = dotenv_values(env_file)
    for keep in ("JENKINS_URL", "JENKINS_USER", "AWS_AUTH_METHOD", "LLM_MAX_TOKENS"):
        assert after[keep] == before[keep]
    assert after_text.startswith("# AtlasOS config\n")
    assert "EMPTY_API_KEY=" in after_text                      # empty: nothing to move
    assert set(sec.stored_names()) == SECRETS


def test_migrate_aborts_before_touching_env_if_readback_fails(kc, env_file):
    kc.corrupt = True
    with pytest.raises(sec.SecretsError, match="read-back"):
        sec.migrate(env_file)
    assert env_file.read_text(encoding="utf-8") == ENV_TEXT


def test_migrate_is_idempotent(kc, env_file):
    sec.migrate(env_file)
    text = env_file.read_text(encoding="utf-8")
    assert sec.migrate(env_file) == []
    assert env_file.read_text(encoding="utf-8") == text


def test_migrate_without_keychain_fails_and_changes_nothing(monkeypatch, env_file):
    monkeypatch.setattr(sec, "_keyring", lambda: None)
    with pytest.raises(sec.SecretsError, match="no OS keychain"):
        sec.migrate(env_file)
    assert env_file.read_text(encoding="utf-8") == ENV_TEXT


# --- restore (undo) -------------------------------------------------------

def test_restore_round_trips_exact_values(kc, env_file):
    original = dotenv_values(env_file)
    sec.migrate(env_file)
    restored = sec.restore_to_env(env_file)
    assert set(restored) == SECRETS
    assert dotenv_values(env_file) == original
    assert sec.stored_names() == []


def test_restore_quotes_awkward_values(kc, tmp_path):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    awkward = {"A_TOKEN": "it's # tricky \"quoted\" \\ value", "B_API_KEY": "has space #hash"}
    for k, v in awkward.items():
        sec.store(k, v)
    sec.restore_to_env(env)
    assert dotenv_values(env) == awkward


# --- loading into the environment -----------------------------------------

def test_load_fills_missing_but_never_overrides(kc):
    sec.store("MCP_AUTH_TOKEN", "from-keychain")
    sec.store("JENKINS_API_TOKEN", "from-keychain-2")
    env = {"MCP_AUTH_TOKEN": "explicit"}
    loaded = sec.load_into_environ(env)
    assert env == {"MCP_AUTH_TOKEN": "explicit", "JENKINS_API_TOKEN": "from-keychain-2"}
    assert loaded == ["JENKINS_API_TOKEN"]


def test_load_never_raises(monkeypatch):
    class Broken:
        def get_password(self, *a):
            raise RuntimeError("keychain exploded")
    monkeypatch.setattr(sec, "_keyring", lambda: Broken())
    assert sec.load_into_environ({}) == []


def test_load_without_keychain_is_a_noop(monkeypatch):
    monkeypatch.setattr(sec, "_keyring", lambda: None)
    env = {}
    assert sec.load_into_environ(env) == [] and env == {}


def test_opt_out_env_var(monkeypatch):
    monkeypatch.setenv("ATLASOS_NO_KEYRING", "1")
    assert sec._keyring() is None


# --- store / delete ---------------------------------------------------------

def test_store_rejects_empty_and_oversized(kc):
    with pytest.raises(sec.SecretsError, match="empty"):
        sec.store("X_TOKEN", "")
    with pytest.raises(sec.SecretsError, match="too long"):
        sec.store("X_TOKEN", "a" * (sec.MAX_VALUE_CHARS + 1))


def test_delete_updates_index(kc):
    sec.store("A_TOKEN", "1")
    sec.store("B_TOKEN", "2")
    assert sec.delete("A_TOKEN") is True
    assert sec.stored_names() == ["B_TOKEN"] and sec.get("A_TOKEN") is None
    assert sec.delete("A_TOKEN") is False


def test_generated_tokens_are_long_and_unique():
    a, b = sec.generate_token(), sec.generate_token()
    assert a != b and len(a) >= 40


# --- CLI never prints values -----------------------------------------------

def test_status_output_contains_names_not_values(kc, env_file, monkeypatch):
    from typer.testing import CliRunner
    from agent import cli
    monkeypatch.setattr(cli, "_ENV_PATH", env_file)
    sec.store("VOYAGE_API_KEY", "pa-SUPER-SECRET")
    out = CliRunner().invoke(cli.app, ["secrets", "status"]).output
    assert "ANTHROPIC_API_KEY" in out and "VOYAGE_API_KEY" in out
    for leaked in ("sk-ant-REAL-VALUE-1", "pa-SUPER-SECRET", "IsviHxoM46GO", "AKIAEXAMPLE"):
        assert leaked not in out


# --- the real Windows Credential Manager -----------------------------------

@pytest.mark.skipif(os.name != "nt", reason="Windows Credential Manager only")
def test_real_windows_credential_manager_round_trip():
    backend = sec._WinCred()
    key = f"TEST_{uuid.uuid4().hex}"
    value = "ünïcödé 'quote' \"dq\" " + "x" * 500
    try:
        backend.set_password("atlasos-test", key, value)
        assert backend.get_password("atlasos-test", key) == value
    finally:
        backend.delete_password("atlasos-test", key)
    assert backend.get_password("atlasos-test", key) is None
    backend.delete_password("atlasos-test", key)       # deleting a missing one is fine
