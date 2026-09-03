"""SQLite persistence for runbook execution history."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path("data/runbook.db")
_lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS runbook_runs (
    id           TEXT PRIMARY KEY,
    runbook_id   TEXT NOT NULL,
    trigger      TEXT NOT NULL DEFAULT 'manual',
    status       TEXT NOT NULL DEFAULT 'running',
    steps_total  INTEGER NOT NULL DEFAULT 0,
    steps_done   INTEGER NOT NULL DEFAULT 0,
    steps_failed INTEGER NOT NULL DEFAULT 0,
    context      TEXT DEFAULT '{}',
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS runbook_steps (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    runbook_id  TEXT NOT NULL,
    step_name   TEXT NOT NULL,
    step_type   TEXT NOT NULL,
    command     TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    output      TEXT DEFAULT '',
    error       TEXT DEFAULT '',
    started_at  TEXT,
    finished_at TEXT
);
"""


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(_DDL)
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_run(
    runbook_id: str,
    trigger: str,
    steps_total: int,
    context_dict: dict | None = None,
) -> str:
    run_id = uuid.uuid4().hex
    ctx = json.dumps(context_dict or {})
    with _lock:
        con = _conn()
        con.execute(
            """
            INSERT INTO runbook_runs
            (id, runbook_id, trigger, status, steps_total, steps_done,
             steps_failed, context, started_at, finished_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (run_id, runbook_id, trigger, "running", steps_total,
             0, 0, ctx, _now(), None),
        )
        con.commit()
        con.close()
    return run_id


def finish_run(run_id: str, status: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE runbook_runs SET status=?, finished_at=? WHERE id=?",
            (status, _now(), run_id),
        )
        con.commit()
        con.close()


def log_step(
    run_id: str,
    runbook_id: str,
    step_name: str,
    step_type: str,
    command: str,
    status: str,
    output: str = "",
    error: str = "",
) -> str:
    step_id = uuid.uuid4().hex
    with _lock:
        con = _conn()
        con.execute(
            """
            INSERT INTO runbook_steps
            (id, run_id, runbook_id, step_name, step_type, command,
             status, output, error, started_at, finished_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (step_id, run_id, runbook_id, step_name, step_type, command,
             status, output, error, _now(), None),
        )
        con.execute(
            "UPDATE runbook_runs SET steps_done = steps_done + 1 WHERE id=?",
            (run_id,),
        )
        con.commit()
        con.close()
    return step_id


def update_step(
    step_id: str,
    status: str,
    output: str = "",
    error: str = "",
) -> None:
    with _lock:
        con = _conn()
        con.execute(
            """
            UPDATE runbook_steps
            SET status=?, output=?, error=?, finished_at=?
            WHERE id=?
            """,
            (status, output, error, _now(), step_id),
        )
        if status == "failed":
            run_row = con.execute(
                "SELECT run_id FROM runbook_steps WHERE id=?", (step_id,)
            ).fetchone()
            if run_row:
                con.execute(
                    "UPDATE runbook_runs SET steps_failed = steps_failed + 1 WHERE id=?",
                    (run_row["run_id"],),
                )
        con.commit()
        con.close()


def get_run(run_id: str) -> dict | None:
    con = _conn()
    row = con.execute(
        "SELECT * FROM runbook_runs WHERE id=?", (run_id,)
    ).fetchone()
    con.close()
    return dict(row) if row else None


def get_steps(run_id: str) -> list[dict]:
    con = _conn()
    rows = con.execute(
        "SELECT * FROM runbook_steps WHERE run_id=? ORDER BY started_at ASC",
        (run_id,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def list_runs(runbook_id: str | None = None, limit: int = 20) -> list[dict]:
    con = _conn()
    if runbook_id:
        rows = con.execute(
            "SELECT * FROM runbook_runs WHERE runbook_id=? ORDER BY started_at DESC LIMIT ?",
            (runbook_id, limit),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM runbook_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    con = _conn()
    row = con.execute(
        """
        SELECT
            COUNT(*) AS total_runs,
            SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes,
            AVG(steps_total) AS avg_steps
        FROM runbook_runs
        """
    ).fetchone()
    con.close()
    total = int(row["total_runs"] or 0)
    successes = int(row["successes"] or 0)
    return {
        "total_runs": total,
        "success_rate_pct": round(100.0 * successes / total, 1) if total else 0.0,
        "avg_steps": round(float(row["avg_steps"] or 0), 1),
    }
