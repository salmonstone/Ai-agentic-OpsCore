"""
Registry mapping a pending action's `kind` to the function that actually
runs it — the one place the Slack button dispatcher and the CLI's manual
`agent approvals propose` both look up.

Every real entry is the exact same async function ChatGPT calls over MCP
(imported from agent.mcp_server, not reimplemented) — the Slack button is a
third caller of that one shared path, never a parallel one. Each already
re-diagnoses fresh and only acts when `confirm=True`, so approving here is
identical to typing "y" at the CLI, just from a different caller.

`test` is not a real fix. It exists so the whole pipeline — Slack message,
button, signature check, dispatch, result posted back — can be proven
end-to-end without ever touching real infrastructure.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable

Executor = Callable[..., Awaitable[dict]]

_REGISTRY: dict[str, Executor] = {}


async def _test_action(marker: str = "", confirm: bool = False) -> dict:
    """Safe, no-infrastructure executor for proving the approval pipeline."""
    if not confirm:
        return {"applied": False, "message": "Set confirm=True to run the test action."}
    from agent.memory.retrieval import remember
    remember(
        f"Slack approval test action ran (marker={marker})",
        source="approvals",
        metadata={"kind": "test", "marker": marker},
    )
    return {
        "applied": True,
        "marker": marker,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }


def load() -> dict[str, Executor]:
    """The kind -> executor map, built lazily so importing this module never
    pulls in the full MCP server (and its FastMCP startup cost) unless a fix
    actually needs dispatching."""
    if _REGISTRY:
        return _REGISTRY

    _REGISTRY["test"] = _test_action

    from agent.mcp_server import (
        aws_apply_fix, cost_apply_fix, ingress_apply_fix,
        jenkins_apply_fix, k8s_apply_fix, tls_apply_fix,
    )
    _REGISTRY.update({
        "jenkins_apply_fix": jenkins_apply_fix,
        "k8s_apply_fix":     k8s_apply_fix,
        "ingress_apply_fix": ingress_apply_fix,
        "tls_apply_fix":     tls_apply_fix,
        "aws_apply_fix":     aws_apply_fix,
        "cost_apply_fix":    cost_apply_fix,
    })
    return _REGISTRY


def kinds() -> list[str]:
    return sorted(load())


async def execute(kind: str, params: dict, confirm: bool) -> dict:
    fn = load().get(kind)
    if fn is None:
        return {"applied": False, "message": f"Unknown kind: {kind!r}. Known: {', '.join(kinds())}"}
    return await fn(**params, confirm=confirm)
