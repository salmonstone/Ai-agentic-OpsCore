"""
Alert deduplication — prevents Slack alert fatigue.

Stores alert history in SQLite. Same fingerprint won't re-fire
until the cooldown expires, UNLESS severity escalates (warning → critical).
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

from agent.observability.logging import get_logger

log = get_logger(__name__)

_DB_PATH = Path("data/alert_dedup.db")

_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _cooldown_seconds() -> int:
    try:
        from agent.config import settings
        return settings.alert_cooldown_minutes * 60
    except Exception:
        return 1800  # 30 min


def _send_resolved() -> bool:
    try:
        from agent.config import settings
        return settings.alert_send_resolved
    except Exception:
        return True


def _get_conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_history (
            fingerprint   TEXT PRIMARY KEY,
            severity      TEXT NOT NULL,
            first_seen    REAL NOT NULL,
            last_seen     REAL NOT NULL,
            count         INTEGER NOT NULL DEFAULT 1,
            resolved      INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def make_fingerprint(pod: str, namespace: str, error_type: str) -> str:
    raw = f"{pod}|{namespace}|{error_type}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class AlertDeduplicator:
    def should_send(self, fingerprint: str, severity: str) -> bool:
        """Return True if this alert should be sent (new or escalated or cooldown expired)."""
        try:
            conn = _get_conn()
            row = conn.execute(
                "SELECT severity, last_seen, resolved FROM alert_history WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            conn.close()

            if row is None:
                return True  # first time

            prev_severity, last_seen, resolved = row

            # If previously resolved, treat as new
            if resolved:
                return True

            # Severity escalation bypasses cooldown
            if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(prev_severity, 0):
                return True

            # Cooldown check
            elapsed = time.time() - last_seen
            return elapsed >= _cooldown_seconds()

        except Exception as exc:
            log.warning("alert_dedup.should_send.error", error=str(exc))
            return True  # fail open — better to over-alert than miss critical

    def record_sent(self, fingerprint: str, severity: str) -> None:
        """Record that an alert was just sent."""
        try:
            now = time.time()
            conn = _get_conn()
            conn.execute("""
                INSERT INTO alert_history (fingerprint, severity, first_seen, last_seen, count, resolved)
                VALUES (?, ?, ?, ?, 1, 0)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    severity  = excluded.severity,
                    last_seen = excluded.last_seen,
                    count     = count + 1,
                    resolved  = 0
            """, (fingerprint, severity, now, now))
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning("alert_dedup.record_sent.error", error=str(exc))

    def mark_resolved(self, fingerprint: str) -> None:
        """Mark an alert fingerprint as resolved (clears cooldown for next occurrence)."""
        try:
            conn = _get_conn()
            conn.execute(
                "UPDATE alert_history SET resolved = 1 WHERE fingerprint = ?",
                (fingerprint,),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning("alert_dedup.mark_resolved.error", error=str(exc))

    def get_active_count(self) -> int:
        """Count alerts currently in cooldown (not resolved)."""
        try:
            conn = _get_conn()
            cutoff = time.time() - _cooldown_seconds()
            count = conn.execute(
                "SELECT COUNT(*) FROM alert_history WHERE resolved = 0 AND last_seen > ?",
                (cutoff,),
            ).fetchone()[0]
            conn.close()
            return count
        except Exception:
            return 0

    def get_stats(self) -> dict:
        """Return stats for the monitor display."""
        try:
            conn = _get_conn()
            cutoff = time.time() - _cooldown_seconds()
            today_cutoff = time.time() - 86400

            firing  = conn.execute(
                "SELECT COUNT(*) FROM alert_history WHERE resolved = 0 AND last_seen > ?",
                (cutoff,),
            ).fetchone()[0]
            today   = conn.execute(
                "SELECT COUNT(*) FROM alert_history WHERE first_seen > ?",
                (today_cutoff,),
            ).fetchone()[0]
            conn.close()
            return {"firing": firing, "sent_today": today}
        except Exception:
            return {"firing": 0, "sent_today": 0}
