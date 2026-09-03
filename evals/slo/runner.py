"""SLO / Error Budget eval runner.

Uses a single shared in-memory SQLite connection (so data persists between
slo_db calls) and mocks all Slack network calls. Skill and DB functions are
called directly - nothing touches a real cluster or webhook. No DB mocking:
the real SQLite budget math is exercised end to end.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

CASES_FILE = Path(__file__).parent / "cases.jsonl"


class _SharedConn(sqlite3.Connection):
    """In-memory connection whose close() is a no-op so it survives all calls."""

    def close(self) -> None:  # type: ignore[override]
        pass

    def real_close(self) -> None:
        super().close()


def _fresh_shared_conn(slo_db):
    """Build one in-memory connection with the schema, shared across calls."""
    con = sqlite3.connect(":memory:", check_same_thread=False, factory=_SharedConn)
    con.row_factory = sqlite3.Row
    con.executescript(slo_db._DDL)
    return con


def _patches(stack: ExitStack):
    """Apply shared-DB + Slack-silencing patches; return the slo_db module."""
    from agent.integrations import slo_db

    shared = _fresh_shared_conn(slo_db)
    stack.enter_context(patch.object(slo_db, "_conn", lambda: shared))
    # Silence Slack everywhere it might be reached.
    stack.enter_context(
        patch("agent.integrations.slack.send_alert_generic", return_value=True)
    )
    return slo_db


def _add_burn(slo_db, slo_id: str, minutes: float, cause: str = "outage") -> None:
    """Directly insert a completed burn of `minutes` within the window."""
    from datetime import datetime, timezone
    import uuid
    con = slo_db._conn()
    con.execute(
        """
        INSERT INTO slo_burns
        (id, slo_id, started_at, ended_at, duration_sec, cause, incident_id)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            uuid.uuid4().hex, slo_id,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
            int(minutes * 60), cause, "",
        ),
    )
    con.commit()


def _run_case(case: dict) -> dict:
    cid  = case["id"]
    name = case["name"]
    exp  = case.get("expected", {})

    with ExitStack() as stack:
        slo_db = _patches(stack)

        # ── slo-01: create + budget math ─────────────────────────────────
        if cid == "slo-01":
            sid = slo_db.create_slo("api uptime", "api-server", "production",
                                    target_pct=99.9, window_days=30)
            st  = slo_db.get_budget_status(sid)
            passed = (
                abs(st["allowed_downtime_min"] - exp["allowed_min"]) < 0.1
                and st["status"] == exp["status"]
            )
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else
                    f"allowed={st['allowed_downtime_min']} status={st['status']}"}

        # ── slo-02: ~50% burned -> warning ───────────────────────────────
        elif cid == "slo-02":
            sid = slo_db.create_slo("api uptime", "api-server", "production",
                                    target_pct=99.9, window_days=30)
            # allowed = 43.2 min; burn ~22 min -> ~49% remaining -> warning
            _add_burn(slo_db, sid, 22.0)
            st = slo_db.get_budget_status(sid)
            passed = (
                st["status"] == exp["status"]
                and st["budget_pct_remaining"] < exp["pct_remaining_lt"]
            )
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else
                    f"status={st['status']} pct={st['budget_pct_remaining']}"}

        # ── slo-03: ~85% burned -> critical + deploy blocked ─────────────
        elif cid == "slo-03":
            sid = slo_db.create_slo("api uptime", "api-server", "production",
                                    target_pct=99.9, window_days=30)
            # allowed = 43.2 min; burn ~37 min -> ~14% remaining -> critical
            _add_burn(slo_db, sid, 37.0)
            st      = slo_db.get_budget_status(sid)
            blocked = slo_db.is_deploy_blocked("api-server", "production")
            passed  = st["status"] == exp["status"] and blocked == exp["deploy_blocked"]
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else
                    f"status={st['status']} blocked={blocked}"}

        # ── slo-04: 100%+ burned -> exhausted + deploy blocked ───────────
        elif cid == "slo-04":
            sid = slo_db.create_slo("api uptime", "api-server", "production",
                                    target_pct=99.9, window_days=30)
            # allowed = 43.2 min; burn 50 min -> fully exhausted
            _add_burn(slo_db, sid, 50.0)
            st      = slo_db.get_budget_status(sid)
            blocked = slo_db.is_deploy_blocked("api-server", "production")
            passed  = st["status"] == exp["status"] and blocked == exp["deploy_blocked"]
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else
                    f"status={st['status']} blocked={blocked}"}

        # ── slo-05: full incident burn lifecycle ─────────────────────────
        elif cid == "slo-05":
            from agent.skills import slo as slo_skill
            sid = slo_db.create_slo("api uptime", "api-server", "production",
                                    target_pct=99.9, window_days=30)
            before = slo_db.get_budget_status(sid)["remaining_min"]
            burn_id = slo_skill.track_incident_burn(
                "inc-xyz", "api-server", "production", cause="CrashLoopBackOff")
            burn_recorded = burn_id is not None
            slo_skill.end_incident_burn(burn_id, 600)  # 10 min outage
            after  = slo_db.get_budget_status(sid)["remaining_min"]
            budget_reduced = after < before
            passed = (
                burn_recorded == exp["burn_recorded"]
                and budget_reduced == exp["budget_reduced"]
            )
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else
                    f"recorded={burn_recorded} before={before} after={after}"}

        # ── slo-06: no SLO -> never blocked ──────────────────────────────
        elif cid == "slo-06":
            from agent.skills import slo as slo_skill
            blocked = slo_db.is_deploy_blocked("ghost-service", "production")
            allowed, _reason = slo_skill.check_deploy_allowed("ghost-service", "production")
            passed = blocked == exp["deploy_blocked"] and allowed is True
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else f"blocked={blocked} allowed={allowed}"}

    return {"id": cid, "name": name, "passed": False, "reason": "unknown case"}


def main() -> bool:
    import sys
    try:  # ensure unicode prints on legacy Windows code pages
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cases   = [json.loads(l) for l in CASES_FILE.read_text().splitlines() if l.strip()]
    results = []
    for case in cases:
        try:
            result = _run_case(case)
        except Exception as exc:  # pragma: no cover - surface failures
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
