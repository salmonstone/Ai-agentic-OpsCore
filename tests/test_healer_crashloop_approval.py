"""_handle_crashloop's split between a bounded, automatic restart and an
AI-guessed fix that must be proposed to Slack instead of applied.

Everything the handler shells out to (kubectl, Claude, daemon_db, incidents,
Slack) is mocked — these tests are about which branch runs and, above all,
that the AI-guessed fix is never applied directly from this function.
"""
from unittest.mock import MagicMock, patch

import pytest

from agent.skills import healer


@pytest.fixture(autouse=True)
def no_cooldown_no_incident(monkeypatch):
    """Isolate from data/daemon.db and the incident store — this file tests
    branching logic, not persistence."""
    monkeypatch.setattr(healer.daemon_db, "check_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(healer.daemon_db, "set_cooldown", lambda *a, **k: None)
    monkeypatch.setattr(healer.daemon_db, "get_fix_count", lambda *a, **k: 0)
    monkeypatch.setattr(healer.daemon_db, "log_action", lambda *a, **k: None)
    monkeypatch.setattr("agent.skills.incident.open_incident", lambda **k: "inc-1")
    monkeypatch.setattr("agent.skills.incident.add_fix_attempt", lambda *a, **k: None)
    monkeypatch.setattr("agent.skills.incident.resolve_incident", lambda *a, **k: None)
    monkeypatch.setattr(healer, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(healer, "run_kubectl", lambda *a, **k: MagicMock(success=True, output="", error=""))


def _ok(**over):
    d = dict(success=True, output="", error="")
    d.update(over)
    return MagicMock(**d)


def test_restart_alone_recovering_never_proposes_or_calls_the_registry(monkeypatch):
    monkeypatch.setattr(healer, "_verify_running", lambda *a, **k: True)
    create = MagicMock()
    monkeypatch.setattr("agent.core.approvals.create", create)

    result = healer._handle_crashloop("api-7d8-xkp", "default", "api", restart_count=2)

    assert result["recovered"] is True and result["fixed"] is True
    create.assert_not_called()


def test_unrecovered_command_crash_proposes_a_patch_command_fix_and_does_not_apply_it(monkeypatch):
    monkeypatch.setattr(healer, "_verify_running", lambda *a, **k: False)
    monkeypatch.setattr(healer, "_get_previous_logs", lambda *a, **k: "boom")
    monkeypatch.setattr(healer, "_get_exit_code", lambda *a, **k: 1)
    monkeypatch.setattr(healer, "_get_container_command", lambda *a, **k: ["myapp"])
    monkeypatch.setattr(healer, "_is_command_crash", lambda *a, **k: True)
    monkeypatch.setattr(healer, "_diagnose_command_crash",
                        lambda *a, **k: ("wrong entrypoint", ["/bin/sh", "-c", "sleep 3600"]))

    patch_fn = MagicMock(return_value=True)
    monkeypatch.setattr(healer, "_patch_deployment_command", patch_fn)

    created = {}

    def fake_create(**kw):
        created.update(kw)
        return MagicMock(id="abc123", **kw)
    monkeypatch.setattr("agent.core.approvals.create", fake_create)
    monkeypatch.setattr("agent.integrations.slack.send_action_approval_request", lambda a: True)

    result = healer._handle_crashloop("api-7d8-xkp", "default", "api", restart_count=2)

    patch_fn.assert_not_called()                          # the fix itself must NOT run here
    assert created["kind"] == "k8s_crashloop_apply_fix"
    assert created["params"]["fix_kind"] == "patch_command"
    import json
    assert json.loads(created["params"]["fix_value"]) == ["/bin/sh", "-c", "sleep 3600"]
    assert result["recovered"] is False and result["proposed"] is True


def test_unrecovered_generic_crash_proposes_a_kubectl_fix_and_does_not_apply_it(monkeypatch):
    monkeypatch.setattr(healer, "_verify_running", lambda *a, **k: False)
    monkeypatch.setattr(healer, "_get_previous_logs", lambda *a, **k: "boom")
    monkeypatch.setattr(healer, "_get_exit_code", lambda *a, **k: -1)
    monkeypatch.setattr(healer, "_get_container_command", lambda *a, **k: ["server"])
    monkeypatch.setattr(healer, "_is_command_crash", lambda *a, **k: False)
    monkeypatch.setattr(healer, "_diagnose_crashloop",
                        lambda *a, **k: ("bad config map", "kubectl rollout restart deployment/api -n default"))

    ran = []
    monkeypatch.setattr(healer, "run_kubectl", lambda cmd: ran.append(cmd) or _ok())

    created = {}
    monkeypatch.setattr("agent.core.approvals.create",
                        lambda **kw: created.update(kw) or MagicMock(id="x", **kw))
    monkeypatch.setattr("agent.integrations.slack.send_action_approval_request", lambda a: True)

    healer._handle_crashloop("api-7d8-xkp", "default", "api", restart_count=1)

    # only the Step-1 rolling restart may have run kubectl — never the proposed fix command
    assert all(c[:2] == ["rollout", "restart"] for c in ran)
    assert created["params"]["fix_kind"] == "kubectl"
    assert created["params"]["fix_value"] == "kubectl rollout restart deployment/api -n default"


def test_an_unsafe_suggested_command_is_never_proposed(monkeypatch):
    monkeypatch.setattr(healer, "_verify_running", lambda *a, **k: False)
    monkeypatch.setattr(healer, "_get_previous_logs", lambda *a, **k: "boom")
    monkeypatch.setattr(healer, "_get_exit_code", lambda *a, **k: -1)
    monkeypatch.setattr(healer, "_get_container_command", lambda *a, **k: ["server"])
    monkeypatch.setattr(healer, "_is_command_crash", lambda *a, **k: False)
    monkeypatch.setattr(healer, "_diagnose_crashloop",
                        lambda *a, **k: ("wants a hard reset", "kubectl delete pod api-x -n default --force"))

    create = MagicMock()
    monkeypatch.setattr("agent.core.approvals.create", create)

    result = healer._handle_crashloop("api-7d8-xkp", "default", "api", restart_count=1)

    create.assert_not_called()
    assert result["proposed"] is False


def test_no_fix_found_falls_back_to_the_original_retry_message(monkeypatch):
    monkeypatch.setattr(healer, "_verify_running", lambda *a, **k: False)
    monkeypatch.setattr(healer, "_get_previous_logs", lambda *a, **k: "boom")
    monkeypatch.setattr(healer, "_get_exit_code", lambda *a, **k: -1)
    monkeypatch.setattr(healer, "_get_container_command", lambda *a, **k: ["server"])
    monkeypatch.setattr(healer, "_is_command_crash", lambda *a, **k: False)
    monkeypatch.setattr(healer, "_diagnose_crashloop", lambda *a, **k: ("unknown", "ESCALATE"))

    create = MagicMock()
    monkeypatch.setattr("agent.core.approvals.create", create)

    result = healer._handle_crashloop("api-7d8-xkp", "default", "api", restart_count=1)

    create.assert_not_called()
    assert result["proposed"] is False and result["recovered"] is False


def test_a_slack_send_failure_falls_back_to_a_plain_alert_and_still_does_not_apply(monkeypatch):
    monkeypatch.setattr(healer, "_verify_running", lambda *a, **k: False)
    monkeypatch.setattr(healer, "_get_previous_logs", lambda *a, **k: "boom")
    monkeypatch.setattr(healer, "_get_exit_code", lambda *a, **k: -1)
    monkeypatch.setattr(healer, "_get_container_command", lambda *a, **k: ["server"])
    monkeypatch.setattr(healer, "_is_command_crash", lambda *a, **k: False)
    monkeypatch.setattr(healer, "_diagnose_crashloop", lambda *a, **k: ("x", "kubectl get pods"))
    monkeypatch.setattr("agent.core.approvals.create", lambda **kw: MagicMock(id="x", **kw))
    monkeypatch.setattr("agent.integrations.slack.send_action_approval_request", lambda a: False)

    patch_fn = MagicMock()
    monkeypatch.setattr(healer, "_patch_deployment_command", patch_fn)
    notified = []
    monkeypatch.setattr(healer, "_notify", lambda *a, **k: notified.append(a))

    healer._handle_crashloop("api-7d8-xkp", "default", "api", restart_count=1)

    patch_fn.assert_not_called()
    assert any("couldn't be sent to Slack" in msg for _, msg, *_ in notified)


# --- apply_crashloop_fix: the executor side, run only after approval -------

def test_apply_patch_command_fix_calls_patch_and_reports_recovery(monkeypatch):
    monkeypatch.setattr(healer.daemon_db, "log_action", lambda *a, **k: None)
    patch_fn = MagicMock(return_value=True)
    monkeypatch.setattr(healer, "_patch_deployment_command", patch_fn)
    monkeypatch.setattr(healer, "_verify_running", lambda *a, **k: True)

    result = healer.apply_crashloop_fix(
        "api-7d8-xkp", "default", "api", "api",
        fix_kind="patch_command", fix_value='["/bin/sh", "-c", "sleep 3600"]',
    )

    patch_fn.assert_called_once_with("api", "default", ["/bin/sh", "-c", "sleep 3600"])
    assert result == {"applied": True, "recovered": True, "fix": "patch command → ['/bin/sh', '-c', 'sleep 3600']"}


def test_apply_kubectl_fix_runs_the_stored_command(monkeypatch):
    monkeypatch.setattr(healer.daemon_db, "log_action", lambda *a, **k: None)
    ran = []
    monkeypatch.setattr(healer, "run_kubectl", lambda cmd: ran.append(cmd) or _ok(success=True))
    monkeypatch.setattr(healer, "_verify_running", lambda *a, **k: False)

    result = healer.apply_crashloop_fix(
        "api-7d8-xkp", "default", "api", "api",
        fix_kind="kubectl", fix_value="kubectl rollout restart deployment/api -n default",
    )

    assert ran == [["kubectl", "rollout", "restart", "deployment/api", "-n", "default"]]
    assert result["applied"] is True and result["recovered"] is False


def test_apply_unknown_fix_kind_fails_closed():
    result = healer.apply_crashloop_fix("p", "ns", "d", "c", fix_kind="rm -rf", fix_value="x")
    assert result["applied"] is False
