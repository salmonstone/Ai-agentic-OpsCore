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
