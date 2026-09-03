"""Incident Auto-Creation eval runner.

Uses a single shared in-memory SQLite connection (so data persists between
incident_db calls) and mocks all Slack network calls. Skill functions are
called directly — nothing touches a real cluster or webhook.
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


def _fresh_shared_conn(incident_db):
    """Build one in-memory connection with the schema, shared across calls.

    SQLite ':memory:' is per-connection, so we make _conn() always return the
    same object and neutralise .close() so it survives the whole test.
    """
    con = sqlite3.connect(":memory:", check_same_thread=False, factory=_SharedConn)
    con.row_factory = sqlite3.Row
    con.executescript(incident_db._DDL)
    return con


def _patches(stack: ExitStack):
    """Apply shared-DB + Slack-silencing patches; return the incident_db module."""
    from agent.integrations import incident_db

    shared = _fresh_shared_conn(incident_db)
    stack.enter_context(patch.object(incident_db, "_conn", lambda: shared))
    # Silence Slack — incident.py reads the webhook url lazily.
    stack.enter_context(patch("agent.skills.incident._webhook_url", return_value=""))
    return incident_db


def _run_case(case: dict) -> dict:
    cid  = case["id"]
    name = case["name"]

    with ExitStack() as stack:
        incident_db = _patches(stack)
        from agent.skills import incident as inc_skill

        # ── inc-01: open new incident ────────────────────────────────────
        if cid == "inc-01":
            iid = inc_skill.open_incident(
                "CrashLoopBackOff: api-1", "critical", "api", "default",
                cause="crashed 5x",
            )
            row = incident_db.get_incident(iid)
            passed = row is not None and row["status"] == "open"
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else "incident not created"}

        # ── inc-02: dedup into existing ──────────────────────────────────
        elif cid == "inc-02":
            first  = inc_skill.open_incident(
                "CrashLoopBackOff: api-1", "warning", "api", "default", cause="c1")
            second = inc_skill.open_incident(
                "CrashLoopBackOff: api-1", "warning", "api", "default", cause="c2")
            all_inc = incident_db.list_incidents(limit=100)
            row = incident_db.get_incident(first)
            # same id returned, no second row, and a new detected event added
            detected = [e for e in row["events"] if e["event_type"] == "detected"]
            passed = (
                first == second
                and len(all_inc) == 1
                and len(detected) >= 2
            )
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else
                    f"first={first} second={second} count={len(all_inc)}"}

        # ── inc-03: resolve on recovery ──────────────────────────────────
        elif cid == "inc-03":
            iid = inc_skill.open_incident(
                "OOMKilled: worker-1", "warning", "worker", "default", cause="oom")
            inc_skill.add_fix_attempt(iid, "bump memory", success=True)
            inc_skill.resolve_incident(iid, cause="auto-healed")
            row = incident_db.get_incident(iid)
            passed = row is not None and row["status"] == "resolved"
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else f"status={row['status'] if row else None}"}

        # ── inc-04: escalate after max attempts ──────────────────────────
        elif cid == "inc-04":
            iid = inc_skill.open_incident(
                "CrashLoopBackOff: api-1", "critical", "api", "default", cause="c")
            for _ in range(3):
                inc_skill.add_fix_attempt(iid, "rolling restart", success=False)
            inc_skill.escalate_incident(iid, "exceeded max fix attempts")
            row = incident_db.get_incident(iid)
            escalated = [e for e in row["events"] if e["event_type"] == "escalated"]
            passed = len(escalated) == 1 and row["fix_attempts"] == 3
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else
                    f"escalated={len(escalated)} attempts={row['fix_attempts']}"}

        # ── inc-05: MTTR calculation ─────────────────────────────────────
        elif cid == "inc-05":
            from datetime import datetime, timedelta, timezone
            iid = inc_skill.open_incident(
                "CrashLoopBackOff: api-1", "warning", "api", "default", cause="c")
            # Backdate opened_at by 10 minutes so the resolved duration is > 0.
            past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            con = incident_db._conn()
            con.execute("UPDATE incidents SET opened_at = ? WHERE id = ?", (past, iid))
            con.commit()
            inc_skill.resolve_incident(iid, cause="auto-healed")
            mttr = incident_db.get_mttr_hours(days=30)
            passed = mttr > 0
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else f"mttr={mttr}"}

        # ── inc-06: stats accuracy ───────────────────────────────────────
        elif cid == "inc-06":
            open_id = inc_skill.open_incident(
                "CrashLoopBackOff: api-1", "critical", "api", "default", cause="c")
            res_id = inc_skill.open_incident(
                "OOMKilled: worker-1", "warning", "worker", "default", cause="c")
            inc_skill.resolve_incident(res_id, cause="auto-healed")
            s = incident_db.get_stats(days=30)
            passed = (
                s["total"] == 2
                and s["open"] == 1
                and s["resolved"] == 1
                and s["critical_count"] == 1
            )
            return {"id": cid, "name": name, "passed": passed,
                    "reason": "" if passed else str(s)}

    return {"id": cid, "name": name, "passed": False, "reason": "unknown case"}


def main() -> bool:
    import sys
    try:  # ensure unicode arrows print on legacy Windows code pages
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
            print(f"         → {result['reason']}")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n  {passed}/{len(results)} passed")
    return passed == len(results)


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
