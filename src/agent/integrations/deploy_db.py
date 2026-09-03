"""
SQLite persistence for pending deploys and deploy reports.

Tables:
  pending_deploys  — deploys waiting for approval or in-flight
  deploy_reports   — completed deploy audit log
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from agent.core.models import DeployReport, PendingDeploy

_DB_PATH = Path("data/deploy.db")
_lock    = threading.Lock()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS pending_deploys (
    id               TEXT PRIMARY KEY,
    repo             TEXT NOT NULL,
    branch           TEXT NOT NULL,
    deployment       TEXT NOT NULL,
    namespace        TEXT NOT NULL,
    new_image        TEXT NOT NULL,
    old_image        TEXT NOT NULL DEFAULT '',
    risk_score       INTEGER NOT NULL DEFAULT 0,
    risk_label       TEXT NOT NULL DEFAULT 'low',
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TEXT NOT NULL,
    approved_at      TEXT,
    deployed_at      TEXT,
    commit_sha       TEXT NOT NULL DEFAULT '',
    author           TEXT NOT NULL DEFAULT '',
    commit_message   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS deploy_reports (
    id                  TEXT PRIMARY KEY,
    deployment          TEXT NOT NULL,
    namespace           TEXT NOT NULL,
    status              TEXT NOT NULL,
    old_image           TEXT NOT NULL DEFAULT '',
    new_image           TEXT NOT NULL DEFAULT '',
    risk_level          TEXT NOT NULL DEFAULT 'low',
    duration_seconds    INTEGER NOT NULL DEFAULT 0,
    pods_healthy        INTEGER NOT NULL DEFAULT 0,
    errors_detected     INTEGER NOT NULL DEFAULT 0,
    rollback_triggered  INTEGER NOT NULL DEFAULT 0,
    claude_summary      TEXT NOT NULL DEFAULT '',
    timestamp           TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(_DDL)
    return con


# ---------------------------------------------------------------------------
# Pending deploys
# ---------------------------------------------------------------------------

def save_pending(p: PendingDeploy) -> None:
    with _lock:
        con = _conn()
        con.execute(
            """
            INSERT OR REPLACE INTO pending_deploys
            (id, repo, branch, deployment, namespace, new_image, old_image,
             risk_score, risk_label, status, created_at, approved_at, deployed_at,
             commit_sha, author, commit_message)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                p.id, p.repo, p.branch, p.deployment, p.namespace,
                p.new_image, p.old_image, p.risk_score, p.risk_label,
                p.status, p.created_at, p.approved_at, p.deployed_at,
                p.commit_sha, p.author, p.commit_message,
            ),
        )
        con.commit()
        con.close()


def get_pending(deploy_id: str) -> PendingDeploy | None:
    con = _conn()
    row = con.execute(
        "SELECT * FROM pending_deploys WHERE id = ?", (deploy_id,)
    ).fetchone()
    con.close()
    return _row_to_pending(row) if row else None


def list_pending(status: str = "pending") -> list[PendingDeploy]:
    con = _conn()
    rows = con.execute(
        "SELECT * FROM pending_deploys WHERE status = ? ORDER BY created_at DESC",
        (status,),
    ).fetchall()
    con.close()
    return [_row_to_pending(r) for r in rows]


def update_pending_status(
    deploy_id: str,
    status: str,
    approved_at: str | None = None,
    deployed_at: str | None = None,
) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE pending_deploys SET status=?, approved_at=?, deployed_at=? WHERE id=?",
            (status, approved_at, deployed_at, deploy_id),
        )
        con.commit()
        con.close()


def expire_old_pending(expiry_hours: int = 1) -> int:
    """Mark pending deploys older than expiry_hours as expired. Returns count."""
    from datetime import timedelta
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=expiry_hours)
    ).isoformat()
    with _lock:
        con = _conn()
        cur = con.execute(
            "UPDATE pending_deploys SET status='expired' "
            "WHERE status='pending' AND created_at < ?",
            (cutoff,),
        )
        count = cur.rowcount
        con.commit()
        con.close()
    return count


def _row_to_pending(row: sqlite3.Row) -> PendingDeploy:
    return PendingDeploy(
        id             = row["id"],
        repo           = row["repo"],
        branch         = row["branch"],
        deployment     = row["deployment"],
        namespace      = row["namespace"],
        new_image      = row["new_image"],
        old_image      = row["old_image"],
        risk_score     = row["risk_score"],
        risk_label     = row["risk_label"],
        status         = row["status"],
        created_at     = row["created_at"],
        approved_at    = row["approved_at"],
        deployed_at    = row["deployed_at"],
        commit_sha     = row["commit_sha"],
        author         = row["author"],
        commit_message = row["commit_message"],
    )


# ---------------------------------------------------------------------------
# Deploy reports
# ---------------------------------------------------------------------------

def save_report(r: DeployReport) -> None:
    with _lock:
        con = _conn()
        con.execute(
            """
            INSERT OR REPLACE INTO deploy_reports
            (id, deployment, namespace, status, old_image, new_image,
             risk_level, duration_seconds, pods_healthy, errors_detected,
             rollback_triggered, claude_summary, timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                r.id, r.deployment, r.namespace, r.status,
                r.old_image, r.new_image, r.risk_level,
                r.duration_seconds, r.pods_healthy, r.errors_detected,
                int(r.rollback_triggered), r.claude_summary, r.timestamp,
            ),
        )
        con.commit()
        con.close()


def list_reports(limit: int = 10) -> list[DeployReport]:
    con = _conn()
    rows = con.execute(
        "SELECT * FROM deploy_reports ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    con.close()
    return [_row_to_report(r) for r in rows]


def _row_to_report(row: sqlite3.Row) -> DeployReport:
    return DeployReport(
        id                 = row["id"],
        deployment         = row["deployment"],
        namespace          = row["namespace"],
        status             = row["status"],
        old_image          = row["old_image"],
        new_image          = row["new_image"],
        risk_level         = row["risk_level"],
        duration_seconds   = row["duration_seconds"],
        pods_healthy       = row["pods_healthy"],
        errors_detected    = row["errors_detected"],
        rollback_triggered = bool(row["rollback_triggered"]),
        claude_summary     = row["claude_summary"],
        timestamp          = row["timestamp"],
    )
