"""Record what was done from the dashboard in AtlasOS's audit trail.

Same trail as the CLI and skills (memory.retrieval.remember → memory.db, the
vector index and data/vault/dashboard.md), so the Activity page and
`agent memory` show dashboard actions next to everything else.

Written on a background thread: remember() embeds the text, which can take a
moment, and a dashboard click must not wait for — or fail because of — it.
"""
from __future__ import annotations

import threading

from agent.observability.logging import get_logger

log = get_logger(__name__)

SOURCE = "dashboard"


def _remember(content: str, metadata: dict) -> None:
    from agent.memory.retrieval import remember
    remember(content, source=SOURCE, metadata=metadata)


def record(content: str, **metadata) -> None:
    """Add one line to the audit trail. Never raises."""
    def _run() -> None:
        try:
            _remember(content, {k: str(v)[:300] for k, v in metadata.items()})
        except Exception as exc:
            log.warning("dashboard.audit_failed", error=str(exc)[:200])
    threading.Thread(target=_run, name="dashboard-audit", daemon=True).start()
