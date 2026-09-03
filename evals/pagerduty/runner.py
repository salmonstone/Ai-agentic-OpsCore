"""On-call paging (PagerDuty / OpsGenie) eval runner.

Mocks httpx so no real HTTP calls are made.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

CASES_FILE = Path(__file__).parent / "cases.jsonl"


def _httpx_resp(status_code: int, text: str = "ok") -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    return r


def _mock_settings(pd_key: str = "", og_key: str = "", og_region: str = "us"):
    """Return a patch for pagerduty.settings with the given keys."""
    s = MagicMock()
    s.pagerduty_routing_key = pd_key
    s.opsgenie_api_key = og_key
    s.opsgenie_region = og_region
    return patch("agent.integrations.pagerduty.settings", s)


def _run_case(case: dict) -> dict:
    cid  = case["id"]
    name = case["name"]

    # Import once, reuse the same module object — no reloads
    from agent.integrations import pagerduty as pd_mod

    # ── pg-01: PagerDuty success ───────────────────────────────────────────
    if cid == "pg-01":
        mock_post = MagicMock(return_value=_httpx_resp(202))
        with _mock_settings(pd_key="abc123routingkey"):
            with patch("httpx.post", mock_post):
                result = pd_mod.page_oncall(
                    title="api crashloop",
                    body="Pod crashed 3 times",
                    severity="critical",
                    service="api",
                )

        passed = (result.get("provider") == "pagerduty"
                  and result.get("success") is True
                  and mock_post.called)
        return {"id": cid, "name": name, "passed": passed,
                "reason": "" if passed else str(result)}

    # ── pg-02: PagerDuty HTTP 400 → failure ───────────────────────────────
    elif cid == "pg-02":
        mock_post = MagicMock(return_value=_httpx_resp(400, "bad routing key"))
        with _mock_settings(pd_key="bad-key"):
            with patch("httpx.post", mock_post):
                result = pd_mod.page_oncall(
                    title="test page",
                    body="test body",
                    severity="critical",
                )

        passed = (result.get("provider") == "pagerduty"
                  and result.get("success") is False)
        return {"id": cid, "name": name, "passed": passed,
                "reason": "" if passed else str(result)}

    # ── pg-03: OpsGenie success (no PD key) ────────────────────────────────
    elif cid == "pg-03":
        mock_post = MagicMock(return_value=_httpx_resp(201))
        with _mock_settings(og_key="og-key-abc"):
            with patch("httpx.post", mock_post):
                result = pd_mod.page_oncall(
                    title="high memory",
                    body="OOM detected",
                    severity="critical",
                    service="worker",
                )

        passed = (result.get("provider") == "opsgenie"
                  and result.get("success") is True
                  and mock_post.called)
        return {"id": cid, "name": name, "passed": passed,
                "reason": "" if passed else str(result)}

    # ── pg-04: no provider → skipped ──────────────────────────────────────
    elif cid == "pg-04":
        mock_post = MagicMock()
        with _mock_settings():
            with patch("httpx.post", mock_post):
                result = pd_mod.page_oncall(title="test", body="test")

        passed = (result.get("skipped") is True
                  and result.get("success") is False
                  and not mock_post.called)
        return {"id": cid, "name": name, "passed": passed,
                "reason": "" if passed else str(result)}

    # ── pg-05: resolve PagerDuty ───────────────────────────────────────────
    elif cid == "pg-05":
        mock_post = MagicMock(return_value=_httpx_resp(200))
        with _mock_settings(pd_key="abc123routingkey"):
            with patch("httpx.post", mock_post):
                ok = pd_mod.resolve_oncall("incident-abc123")

        passed = ok is True and mock_post.called
        return {"id": cid, "name": name, "passed": passed,
                "reason": "" if passed else f"resolved={ok}"}

    # ── pg-06: healer escalation calls page_oncall ─────────────────────────
    elif cid == "pg-06":
        page_calls: list[dict] = []

        def fake_page(**kwargs):
            page_calls.append(kwargs)
            return {"provider": "pagerduty", "success": True, "skipped": False}

        with ExitStack() as stack:
            stack.enter_context(patch("agent.integrations.daemon_db.check_cooldown", return_value=False))
            stack.enter_context(patch("agent.integrations.daemon_db.get_fix_count",  return_value=3))
            stack.enter_context(patch("agent.integrations.daemon_db.log_action",     return_value="x"))
            stack.enter_context(patch("agent.integrations.daemon_db.set_cooldown"))
            stack.enter_context(patch("agent.integrations.slack.send_alert_generic", return_value=True))
            stack.enter_context(patch("agent.skills.incident.open_incident",         return_value="inc-1"))
            stack.enter_context(patch("agent.skills.incident.escalate_incident"))
            stack.enter_context(patch("agent.integrations.pagerduty.page_oncall",    side_effect=fake_page))

            from agent.skills import healer
            import importlib; importlib.reload(healer)
            result = healer._handle_crashloop("api-5f9d-abc", "default", "api", 10)

        passed = len(page_calls) >= 1 and result.get("escalated") is True
        return {"id": cid, "name": name, "passed": passed,
                "reason": "" if passed else f"page_calls={page_calls}, result={result}"}

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
