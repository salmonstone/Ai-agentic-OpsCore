"""Auto-scaling policy eval runner.

Exercises the autoscale skill in isolation: kubectl, the events DB writer and
Slack are all mocked, and the module-level clock is patched so scheduled cases
are deterministic. Nothing touches a real cluster or webhook.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

CASES_FILE = Path(__file__).parent / "cases.jsonl"


class _FrozenDatetime(datetime):
    """datetime subclass whose now()/utcnow() return a fixed instant."""

    _fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)

    @classmethod
    def utcnow(cls):  # type: ignore[override]
        return cls._fixed.replace(tzinfo=None)


def _frozen_at(hh, mm):
    cls = type("_FrozenAt", (_FrozenDatetime,), {})
    cls._fixed = datetime(2026, 1, 1, hh, mm, 0, tzinfo=timezone.utc)
    return cls


def _kubectl_result(output="", success=True):
    return SimpleNamespace(output=output, error="", success=success,
                           command=[], duration_ms=0.0)


def _run_case(case):
    cid = case["id"]
    name = case["name"]
    exp = case.get("expected", {})

    from agent.skills import autoscale

    with ExitStack() as stack:
        stack.enter_context(patch.object(autoscale.autoscale_db, "log_event",
                                         return_value="evt"))
        stack.enter_context(patch("agent.integrations.slack.send_alert_generic",
                                  return_value=True))
        scale_mock = MagicMock(return_value=True)
        stack.enter_context(patch.object(autoscale, "_set_replicas", scale_mock))

        if cid == "sc-01":
            stack.enter_context(patch.object(autoscale, "datetime", _frozen_at(17, 30)))
            stack.enter_context(patch.object(autoscale, "_get_current_replicas",
                                             return_value=3))
            policy = {"id": "p1", "deployment": "api", "namespace": "default",
                      "schedule_down_utc": "17:30", "schedule_up_utc": "03:30",
                      "down_replicas": 1, "up_replicas": 3}
            res = autoscale.apply_schedule(policy)
            passed = (res["action"] == exp["action"]
                      and res["new_replicas"] == exp["new_replicas"]
                      and scale_mock.called)
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else f"got {res}"}

        if cid == "sc-02":
            stack.enter_context(patch.object(autoscale, "datetime", _frozen_at(3, 30)))
            stack.enter_context(patch.object(autoscale, "_get_current_replicas",
                                             return_value=1))
            policy = {"id": "p2", "deployment": "api", "namespace": "default",
                      "schedule_down_utc": "17:30", "schedule_up_utc": "03:30",
                      "down_replicas": 1, "up_replicas": 3}
            res = autoscale.apply_schedule(policy)
            passed = (res["action"] == exp["action"]
                      and res["new_replicas"] == exp["new_replicas"]
                      and scale_mock.called)
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else f"got {res}"}

        if cid == "sc-03":
            stack.enter_context(patch.object(autoscale, "datetime", _frozen_at(12, 0)))
            stack.enter_context(patch.object(autoscale, "_get_current_replicas",
                                             return_value=3))
            policy = {"id": "p3", "deployment": "api", "namespace": "default",
                      "schedule_down_utc": "17:30", "schedule_up_utc": "03:30",
                      "down_replicas": 1, "up_replicas": 3}
            res = autoscale.apply_schedule(policy)
            passed = res["action"] == exp["action"] and not scale_mock.called
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else f"got {res}"}

        if cid == "sc-04":
            stack.enter_context(patch.object(autoscale, "datetime", _frozen_at(17, 30)))
            stack.enter_context(patch.object(autoscale, "_get_current_replicas",
                                             return_value=1))
            policy = {"id": "p4", "deployment": "api", "namespace": "default",
                      "schedule_down_utc": "17:30", "schedule_up_utc": "03:30",
                      "down_replicas": 1, "up_replicas": 3}
            res = autoscale.apply_schedule(policy)
            passed = (res["action"] == exp["action"]
                      and res.get("reason") == exp["reason"]
                      and not scale_mock.called)
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else f"got {res}"}

        if cid == "sc-05":
            stack.enter_context(patch.object(autoscale, "_find_policy",
                return_value={"id": "p5", "deployment": "api",
                              "namespace": "default", "max_replicas": 10}))
            res = autoscale.scale_for_cpu("api", "default", 90.0, 2)
            passed = (res["action"] == exp["action"]
                      and res["new_replicas"] == exp["new_replicas"]
                      and scale_mock.called)
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else f"got {res}"}

        if cid == "sc-06":
            stack.enter_context(patch.object(autoscale, "_find_policy",
                return_value={"id": "p6", "deployment": "api",
                              "namespace": "default", "max_replicas": 10}))
            res = autoscale.scale_for_cpu("api", "default", 90.0, 10)
            passed = (res["action"] == exp["action"]
                      and res.get("reason") == exp["reason"]
                      and not scale_mock.called)
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else f"got {res}"}

    return {"id": cid, "name": name, "passed": False, "reason": "unknown case"}


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cases = [json.loads(l) for l in CASES_FILE.read_text().splitlines() if l.strip()]
    results = []
    for case in cases:
        try:
            result = _run_case(case)
        except Exception as exc:
            result = {"id": case["id"], "name": case["name"],
                      "passed": False, "reason": f"exception: {exc}"}
        results.append(result)
        status = "  [PASS]" if result["passed"] else "  [FAIL]"
        print(f"{status}  {result['id']}  {result['name']}")
        if not result["passed"]:
            print(f"         -> {result['reason']}")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n  {passed}/{len(results)} passed")
    return passed == len(results)


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
