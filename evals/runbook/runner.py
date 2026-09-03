"""Runbook automation eval runner.

Mocks kubectl, slack, and runbook_db so nothing touches a real cluster.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, call, patch

CASES_FILE = Path(__file__).parent / "cases.jsonl"


def _kubectl_ok(output: str = "") -> MagicMock:
    r = MagicMock()
    r.success = True
    r.output = output
    r.error = ""
    return r


def _kubectl_fail(error: str = "kubectl error") -> MagicMock:
    r = MagicMock()
    r.success = False
    r.output = ""
    r.error = error
    return r


def _mock_db_patches() -> list:
    """Patches that neutralise all runbook_db writes."""
    return [
        patch("agent.integrations.runbook_db.start_run",  return_value="run-fake-id"),
        patch("agent.integrations.runbook_db.finish_run"),
        patch("agent.integrations.runbook_db.log_step",   return_value="step-fake-id"),
        patch("agent.integrations.runbook_db.update_step"),
    ]


def _run_case(case: dict) -> dict:
    cid  = case["id"]
    name = case["name"]

    # ── rb-01: disk-full runbook succeeds ─────────────────────────────────
    if cid == "rb-01":
        slack_mock = MagicMock(return_value=True)
        kubectl_mock = MagicMock(return_value=_kubectl_ok("done"))
        with ExitStack() as stack:
            for p in _mock_db_patches():
                stack.enter_context(p)
            stack.enter_context(patch("agent.integrations.slack.send_alert_generic", slack_mock))
            stack.enter_context(patch("agent.integrations.kubectl.run_kubectl", kubectl_mock))

            from agent.skills.runbook import run_runbook
            result = run_runbook("disk-full", context={
                "pod_name": "web-abc",
                "namespace": "default",
                "pvc_name": "pvc-data",
                "usage_pct": "88",
            })

        passed = result.get("status") == "success"
        return {"id": cid, "name": name, "passed": passed,
                "reason": "" if passed else str(result)}

    # ── rb-02: on_failure=continue → partial ──────────────────────────────
    elif cid == "rb-02":
        # Use high-memory runbook: restart_pod step fails (on_failure=escalate
        # by default), so build a synthetic runbook to test continue path.
        # We directly test run_runbook with the disk-full runbook, making the
        # "clean_docker_images" step fail (it has on_failure=continue).
        call_count = {"n": 0}
        def kubectl_side_effect(tokens):
            call_count["n"] += 1
            # first kubectl call (clean_docker_images) → fail
            if call_count["n"] == 1:
                return _kubectl_fail("docker not found")
            return _kubectl_ok("ok")

        with ExitStack() as stack:
            for p in _mock_db_patches():
                stack.enter_context(p)
            stack.enter_context(patch("agent.integrations.slack.send_alert_generic",
                                      MagicMock(return_value=True)))
            stack.enter_context(patch("agent.integrations.kubectl.run_kubectl",
                                      side_effect=kubectl_side_effect))

            from agent.skills import runbook as rb_module
            # Reload to avoid caching
            import importlib
            importlib.reload(rb_module)

            result = rb_module.run_runbook("disk-full", context={
                "pod_name": "web-abc",
                "namespace": "default",
                "pvc_name": "pvc-data",
                "usage_pct": "88",
            })

        passed = result.get("status") == "partial"
        return {"id": cid, "name": name, "passed": passed,
                "reason": "" if passed else str(result)}

    # ── rb-03: on_failure=escalate → failed, Slack alert ──────────────────
    elif cid == "rb-03":
        slack_mock = MagicMock(return_value=True)

        def kubectl_fail_all(tokens):
            return _kubectl_fail("node unreachable")

        with ExitStack() as stack:
            for p in _mock_db_patches():
                stack.enter_context(p)
            stack.enter_context(patch("agent.integrations.slack.send_alert_generic", slack_mock))
            stack.enter_context(patch("agent.integrations.kubectl.run_kubectl",
                                      side_effect=kubectl_fail_all))

            from agent.skills import runbook as rb_module
            import importlib
            importlib.reload(rb_module)

            result = rb_module.run_runbook("node-not-ready", context={
                "node_name": "node-1",
            })

        escalated = slack_mock.called
        passed = result.get("status") == "failed" and escalated
        return {"id": cid, "name": name, "passed": passed,
                "reason": "" if passed else str(result)}

    # ── rb-04: template resolution ─────────────────────────────────────────
    elif cid == "rb-04":
        captured_cmds: list[list] = []

        def kubectl_capture(tokens):
            captured_cmds.append(list(tokens))
            return _kubectl_ok("ok")

        with ExitStack() as stack:
            for p in _mock_db_patches():
                stack.enter_context(p)
            stack.enter_context(patch("agent.integrations.slack.send_alert_generic",
                                      MagicMock(return_value=True)))
            stack.enter_context(patch("agent.integrations.kubectl.run_kubectl",
                                      side_effect=kubectl_capture))

            from agent.skills import runbook as rb_module
            import importlib
            importlib.reload(rb_module)

            rb_module.run_runbook("disk-full", context={
                "pod_name": "test-pod",
                "namespace": "default",
                "pvc_name": "pvc-data",
                "usage_pct": "90",
            })

        # Check that "test-pod" appeared in at least one kubectl call arg
        all_tokens = [t for cmd in captured_cmds for t in cmd]
        passed = "test-pod" in all_tokens or any("test-pod" in t for t in all_tokens)
        return {"id": cid, "name": name, "passed": passed,
                "reason": "" if passed else f"captured commands: {captured_cmds}"}

    # ── rb-05: unknown runbook ─────────────────────────────────────────────
    elif cid == "rb-05":
        with ExitStack() as stack:
            for p in _mock_db_patches():
                stack.enter_context(p)

            from agent.skills import runbook as rb_module
            import importlib
            importlib.reload(rb_module)

            result = rb_module.run_runbook("nonexistent-runbook-xyz")

        passed = result.get("success") is False
        return {"id": cid, "name": name, "passed": passed,
                "reason": "" if passed else str(result)}

    # ── rb-06: list_runbooks ───────────────────────────────────────────────
    elif cid == "rb-06":
        from agent.skills import runbook as rb_module
        import importlib
        importlib.reload(rb_module)

        items = rb_module.list_runbooks()
        passed = len(items) >= 3
        return {"id": cid, "name": name, "passed": passed,
                "reason": "" if passed else f"only {len(items)} runbooks found"}

    return {"id": cid, "name": name, "passed": False, "reason": "unknown case"}


def main() -> bool:
    cases   = [json.loads(l) for l in CASES_FILE.read_text().splitlines() if l.strip()]
    results = []
    for case in cases:
        result = _run_case(case)
        results.append(result)
        status = "  [PASS]" if result["passed"] else "  [FAIL]"
        print(f"{status}  {case['id']}  {case['name']}")
        if not result["passed"]:
            print(f"         -> {result['reason']}")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n  {passed}/{len(results)} passed")
    return passed == len(results)


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
