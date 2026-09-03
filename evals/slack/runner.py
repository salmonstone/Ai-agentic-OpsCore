"""
Eval runner for Slack alerting — tests deduplication logic and message formatting.
Does NOT send real Slack messages (webhook mocked).

Run: agent eval slack
"""
from __future__ import annotations

import json
import time
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

# ── Bootstrap path ────────────────────────────────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

CASES_PATH = Path(__file__).parent / "cases.jsonl"

_SEVERITY_COLOR = {"critical": "#FF0000", "warning": "#FFA500", "info": "#0000FF"}
_COOLDOWN = 1800  # 30 min in seconds


def _setup_dedup(case: dict, db_path: str) -> None:
    """Pre-populate dedup DB with prior alerts from the test case."""
    from agent.integrations.alert_dedup import AlertDeduplicator, make_fingerprint

    with patch("agent.integrations.alert_dedup._DB_PATH", Path(db_path)):
        dedup = AlertDeduplicator()
        for prior in case.get("prior_alerts", []):
            fp  = make_fingerprint(prior["pod"], prior["namespace"], prior["error_type"])
            sev = prior["severity"]
            # Record it
            dedup.record_sent(fp, sev)
            # If it was recorded N seconds ago, backdating isn't easy without SQLite surgery,
            # so we check elapsed time logic by injecting directly
            if prior.get("seconds_ago", 0) > _COOLDOWN:
                # Force last_seen to be in the past beyond cooldown
                import sqlite3
                conn = sqlite3.connect(db_path)
                past = time.time() - prior["seconds_ago"]
                conn.execute(
                    "UPDATE alert_history SET last_seen = ? WHERE fingerprint = ?",
                    (past, fp),
                )
                conn.commit()
                conn.close()
            if prior.get("resolved"):
                dedup.mark_resolved(fp)


def run_case(case: dict) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        from agent.integrations.alert_dedup import AlertDeduplicator, make_fingerprint

        with patch("agent.integrations.alert_dedup._DB_PATH", Path(db_path)):
            _setup_dedup(case, db_path)
            dedup = AlertDeduplicator()
            fp    = make_fingerprint(case["pod"], case["namespace"], case["error_type"])
            sev   = case["severity"]

            should_send = dedup.should_send(fp, sev)

        # Check message format if expected color given
        color_ok = True
        if case.get("expected_color"):
            from agent.integrations.slack import _SEVERITY_COLOR
            actual_color = _SEVERITY_COLOR.get(sev, "")
            color_ok = actual_color == case["expected_color"]

        # Check resolved flow
        resolved_ok = True
        if case.get("test_resolved"):
            with patch("agent.integrations.alert_dedup._DB_PATH", Path(db_path)):
                with patch("agent.integrations.slack._webhook_url", return_value="https://mock.webhook"):
                    with patch("httpx.post") as mock_post:
                        mock_resp = MagicMock()
                        mock_resp.raise_for_status = MagicMock()
                        mock_post.return_value = mock_resp

                        from agent.integrations.slack import send_resolved_generic
                        sent = send_resolved_generic(f"{case['pod']} recovered", "back to normal")
                        resolved_ok = sent is True

        passed = (should_send == case["expected_should_send"]) and color_ok and resolved_ok

        return {
            "id":           case["id"],
            "scenario":     case["scenario"],
            "passed":       passed,
            "should_send":  should_send,
            "expected":     case["expected_should_send"],
            "color_ok":     color_ok,
            "resolved_ok":  resolved_ok,
            "score":        100 if passed else 0,
        }

    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def main() -> None:
    cases = []
    with open(CASES_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))

    print(f"\n{'-'*60}")
    print(f"  SLACK EVAL -- {len(cases)} cases")
    print(f"{'-'*60}\n")

    results = []
    for case in cases:
        try:
            r = run_case(case)
            results.append(r)
            icon = "PASS" if r["passed"] else "FAIL"
            note = ""
            if not r["passed"]:
                note = f"  should_send={r['should_send']} expected={r['expected']}"
                if not r["color_ok"]:
                    note += " color_mismatch"
            print(f"  [{icon}] {case['id']:8s} {case['scenario'][:50]:<50}{note}")
        except Exception as exc:
            results.append({"id": case["id"], "passed": False, "score": 0})
            print(f"  [FAIL] {case['id']:8s} ERROR: {exc}")

    passed = sum(1 for r in results if r["passed"])
    avg    = sum(r.get("score", 0) for r in results) / len(results)

    print(f"\n{'-'*60}")
    print(f"  RESULT: {passed}/{len(cases)} passed  |  avg score: {avg:.0f}/100")
    print(f"{'-'*60}\n")

    if passed < len(cases):
        sys.exit(1)


if __name__ == "__main__":
    main()
