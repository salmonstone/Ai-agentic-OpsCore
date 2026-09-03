"""Autonomous Healing Daemon eval runner.

Mocks kubectl, daemon_db, Slack, and Claude so nothing touches a real cluster.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

CASES_FILE = Path(__file__).parent / "cases.jsonl"


def _kubectl_ok(output: str = "") -> MagicMock:
    r = MagicMock()
    r.success = True
    r.output  = output
    r.error   = ""
    return r


def _run_case(case: dict) -> dict:
    cid  = case["id"]
    name = case["name"]

    # Patches applied to every case
    always = [
        patch("agent.integrations.daemon_db.log_action",      return_value="fake-id"),
        patch("agent.integrations.daemon_db.set_cooldown"),
        patch("agent.integrations.slack.send_alert_generic",  return_value=True),
    ]

    with ExitStack() as stack:
        for p in always:
            stack.enter_context(p)

        # ── dmn-01: CrashLoopBackOff ─────────────────────────────────────
        if cid == "dmn-01":
            llm_resp         = MagicMock()
            llm_resp.content = (
                "CAUSE: missing config map\n"
                "FIX: kubectl rollout restart deployment/api -n default"
            )
            stack.enter_context(patch("agent.integrations.daemon_db.check_cooldown", return_value=False))
            stack.enter_context(patch("agent.integrations.daemon_db.get_fix_count",  return_value=0))
            stack.enter_context(patch("agent.integrations.kubectl.run_kubectl",       return_value=_kubectl_ok("logs")))
            stack.enter_context(patch("agent.core.async_utils.run_sync",             return_value=llm_resp))
            stack.enter_context(patch("agent.skills.healer._verify_running",         return_value=True))

            from agent.skills import healer
            result = healer._handle_crashloop("api-5f9d-abc", "default", "api", 5)
            passed = result.get("detected") is True and result.get("action") == "heal_crashloop"
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else str(result)}

        # ── dmn-02: OOMKilled ─────────────────────────────────────────────
        elif cid == "dmn-02":
            stack.enter_context(patch("agent.integrations.daemon_db.check_cooldown", return_value=False))
            stack.enter_context(patch("agent.integrations.daemon_db.get_fix_count",  return_value=0))
            stack.enter_context(patch("agent.skills.healer._get_mem_limit_mb",       return_value=256))
            stack.enter_context(patch("agent.integrations.kubectl.run_kubectl",       return_value=_kubectl_ok()))
            stack.enter_context(patch("agent.skills.healer._verify_running",         return_value=True))

            from agent.skills import healer
            result = healer._handle_oom("api-5f9d-abc", "default", "api", 256)
            passed = (
                result.get("detected") is True
                and result.get("limit_increased") is True
                and result.get("new_limit_mb", 0) > result.get("old_limit_mb", 256)
            )
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else str(result)}

        # ── dmn-03: Cooldown respected ────────────────────────────────────
        elif cid == "dmn-03":
            stack.enter_context(patch("agent.integrations.daemon_db.check_cooldown", return_value=True))

            from agent.skills import healer
            result = healer._handle_crashloop("api-5f9d-abc", "default", "api", 3)
            passed = result.get("skipped") is True
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else str(result)}

        # ── dmn-04: Escalate after 3 fixes ───────────────────────────────
        elif cid == "dmn-04":
            stack.enter_context(patch("agent.integrations.daemon_db.check_cooldown", return_value=False))
            stack.enter_context(patch("agent.integrations.daemon_db.get_fix_count",  return_value=3))

            from agent.skills import healer
            result = healer._handle_crashloop("api-5f9d-abc", "default", "api", 10)
            passed = result.get("escalated") is True and result.get("action") == "slack_alert"
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else str(result)}

        # ── dmn-05: High CPU → autoscale ──────────────────────────────────
        elif cid == "dmn-05":
            stack.enter_context(patch("agent.integrations.daemon_db.check_cooldown", return_value=False))
            stack.enter_context(patch("agent.skills.healer._get_replicas",           return_value=2))
            stack.enter_context(patch("agent.integrations.kubectl.run_kubectl",       return_value=_kubectl_ok()))

            from agent.skills import healer
            result = healer._handle_high_cpu("api-5f9d-abc", "default", 87.0, 3)
            passed = result.get("scaled") is True and result.get("replicas_increased") is True
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else str(result)}

        # ── dmn-06: Bad deploy → rollback ─────────────────────────────────
        elif cid == "dmn-06":
            stack.enter_context(patch("agent.skills.healer._is_recent_deploy", return_value=True))
            stack.enter_context(patch("agent.integrations.kubectl.run_kubectl", return_value=_kubectl_ok()))
            stack.enter_context(patch("agent.skills.healer._mark_rolled_back"))

            from agent.skills import healer
            result = healer._handle_bad_deploy("api", "default", restart_count=5)
            passed = result.get("rolled_back") is True
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else str(result)}

        # ── dmn-07: ImagePullBackOff — alert only ─────────────────────────
        elif cid == "dmn-07":
            from agent.skills import healer
            result = healer._handle_imagepull("api-5f9d-abc", "default", "myrepo/api:bad-tag")
            passed = result.get("action") == "slack_alert" and result.get("alerted") is True
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else str(result)}

        # ── dmn-08: kube-system grace < 10 min — skip ────────────────────
        elif cid == "dmn-08":
            import time
            from agent.core.daemon import HealingDaemon
            d = HealingDaemon()
            d._broken_since["coredns-abc"] = time.time()  # just now → not ready yet
            ready  = d._kube_system_ready("coredns-abc", "CrashLoopBackOff")
            passed = ready is False
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else "kube-system grace not respected"}

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
            print(f"         → {result['reason']}")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n  {passed}/{len(results)} passed")
    return passed == len(results)


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
