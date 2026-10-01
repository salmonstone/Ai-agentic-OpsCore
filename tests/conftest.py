"""Shared test setup.

AtlasOS copies OS-keychain secrets into the environment on import
(core/secrets.load_into_environ). On a machine where the dashboard login is
turned on, that puts the real DASHBOARD_TOKEN into every test process and
makes the dashboard tests depend on the developer's keychain. Remove it for
every test; tests that need a login set their own token.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_real_dashboard_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "")      # record the original so it's restored after
    monkeypatch.delenv("DASHBOARD_TOKEN")
    # The dashboard also reads the token live from the keychain; tests must
    # never see the developer's real one. Tests that fake a keychain patch
    # agent.core.secrets._keyring and still get it.
    from agent.dashboard import security
    monkeypatch.setattr(security, "_keychain_token", lambda: _fake_keychain_token())


@pytest.fixture(autouse=True)
def _isolated_notification_inbox(tmp_path, monkeypatch):
    """Every Slack/page/incident alert is now also recorded in the inbox —
    keep tests from writing into the real data/notifications.db."""
    from agent.integrations import notifications
    monkeypatch.setattr(notifications, "_DB_PATH", tmp_path / "notifications.db")


def _fake_keychain_token() -> str:
    """Only a test's in-memory FakeKeyring counts; the real keychain is ignored."""
    from agent.core import secrets as sec
    kr = sec._keyring()
    if type(kr).__name__ != "FakeKeyring":
        return ""
    return (kr.get_password(sec.SERVICE, "DASHBOARD_TOKEN") or "").strip()
