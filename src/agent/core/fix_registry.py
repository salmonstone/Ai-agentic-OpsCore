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
        jenkins_apply_fix, k8s_apply_fix, k8s_crashloop_apply_fix, tls_apply_fix,
    )
    _REGISTRY.update({
        "jenkins_apply_fix":         jenkins_apply_fix,
        "k8s_apply_fix":             k8s_apply_fix,
        "k8s_crashloop_apply_fix":   k8s_crashloop_apply_fix,
        "ingress_apply_fix":         ingress_apply_fix,
        "tls_apply_fix":             tls_apply_fix,
        "aws_apply_fix":             aws_apply_fix,
        "cost_apply_fix":            cost_apply_fix,
    })
    return _REGISTRY


def kinds() -> list[str]:
    return sorted(load())


async def execute(kind: str, params: dict, confirm: bool) -> dict:
    fn = load().get(kind)
    if fn is None:
        return {"applied": False, "message": f"Unknown kind: {kind!r}. Known: {', '.join(kinds())}"}
    return await fn(**params, confirm=confirm)


# ---------------------------------------------------------------------------
# Proposing — the one validated path shared by `agent approvals propose` and
# the propose_fix MCP tool. Proposing never changes infrastructure: it writes
# a pending row and sends a Slack Approve/Reject message; only a human tap
# runs the fix, through execute() above.
# ---------------------------------------------------------------------------

# Kinds a remote MCP client (ChatGPT) may propose. Excludes
# k8s_crashloop_apply_fix: its fix_kind/fix_value params carry a free-form
# kubectl command, which is fine when the daemon generates it but would let
# a remote model put an arbitrary command in front of the approver.
REMOTE_PROPOSABLE = frozenset({
    "test", "jenkins_apply_fix", "k8s_apply_fix", "ingress_apply_fix",
    "tls_apply_fix", "aws_apply_fix", "cost_apply_fix",
})

# Most proposals a remote client may have pending at once, so a runaway
# conversation can't flood the approval channel.
MAX_REMOTE_PENDING = 5
MAX_SUMMARY_CHARS = 300


class ProposalError(ValueError):
    pass


def _coerce(name: str, value, annotation):
    """Convert a CLI string / JSON value to the executor's declared type."""
    import types
    import typing

    origin = typing.get_origin(annotation)
    options = typing.get_args(annotation) if origin in (typing.Union, types.UnionType) else (annotation,)
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "none", "null")):
        if type(None) in options:
            return None
        if value is None:
            raise ProposalError(f"{name}: a value is required")
    for t in options:
        if t is type(None):
            continue
        if t is bool:
            if isinstance(value, bool):
                return value
            s = str(value).strip().lower()
            if s in ("true", "1", "yes"):
                return True
            if s in ("false", "0", "no"):
                return False
            raise ProposalError(f"{name}: expected true/false, got {value!r}")
        if t is int:
            if isinstance(value, bool):
                raise ProposalError(f"{name}: expected an integer, got {value!r}")
            try:
                return int(str(value).strip())
            except ValueError:
                raise ProposalError(f"{name}: expected an integer, got {value!r}") from None
        if t is float:
            try:
                return float(value)
            except (TypeError, ValueError):
                raise ProposalError(f"{name}: expected a number, got {value!r}") from None
        if t is str:
            return str(value)
    return value


def param_spec(kind: str) -> dict[str, dict]:
    """{param: {"type": ..., "required": bool}} for a kind, never `confirm`."""
    import inspect

    fn = load().get(kind)
    if fn is None:
        raise ProposalError(f"Unknown kind {kind!r}. Known: {', '.join(kinds())}")
    spec = {}
    for n, p in inspect.signature(fn, eval_str=True).parameters.items():
        if n == "confirm":
            continue
        spec[n] = {
            "type": getattr(p.annotation, "__name__", str(p.annotation)),
            "required": p.default is inspect.Parameter.empty,
        }
    return spec


def validate_params(kind: str, params: dict) -> dict:
    """Reject unknown or missing params (and any attempt to pass `confirm`),
    and convert values to the declared types."""
    import inspect

    fn = load().get(kind)
    if fn is None:
        raise ProposalError(f"Unknown kind {kind!r}. Known: {', '.join(kinds())}")
    sig = {n: p for n, p in inspect.signature(fn, eval_str=True).parameters.items() if n != "confirm"}
    params = params or {}
    if "confirm" in params:
        raise ProposalError("`confirm` can't be proposed — only a human approval sets it")
    unknown = sorted(set(params) - set(sig))
    if unknown:
        raise ProposalError(f"Unknown param(s) for {kind}: {', '.join(unknown)}. Expected: {', '.join(sig)}")
    missing = sorted(n for n, p in sig.items() if p.default is inspect.Parameter.empty and n not in params)
    if missing:
        raise ProposalError(f"Missing required param(s) for {kind}: {', '.join(missing)}")
    return {n: _coerce(n, v, sig[n].annotation) for n, v in params.items()}


def propose(kind: str, params: dict | None, summary: str, source: str,
            remote: bool = False, ttl_minutes: int | None = None) -> dict:
    """Validate, dedupe, record, and send one proposal to Slack.

    Returns {"id", "status", "expires_at", "duplicate"}. Raises ProposalError
    for anything invalid — and nothing is written or sent in that case.
    """
    from agent.core import approvals
    from agent.integrations import slack as slack_integration

    if remote and kind not in REMOTE_PROPOSABLE:
        raise ProposalError(
            f"{kind!r} can't be proposed remotely. Allowed: {', '.join(sorted(REMOTE_PROPOSABLE))}")
    clean = validate_params(kind, params or {})
    summary = (summary or "").strip()
    if not summary:
        raise ProposalError("A summary is required — it's what the approver reads in Slack")
    summary = summary[:MAX_SUMMARY_CHARS]
    if not slack_integration.is_configured():
        raise ProposalError("Slack isn't configured (SLACK_WEBHOOK_URL) — nowhere to send the approval")

    approvals.expire_stale()
    pending = approvals.list_actions(status="pending", limit=200)
    for a in pending:
        if a.kind == kind and a.params == clean:
            return {"id": a.id, "status": a.status, "expires_at": a.expires_at, "duplicate": True}
    if remote and len(pending) >= MAX_REMOTE_PENDING:
        raise ProposalError(
            f"{len(pending)} proposals are already waiting for approval (limit {MAX_REMOTE_PENDING}). "
            "Approve, reject, or let them expire before proposing more.")

    kwargs = {} if ttl_minutes is None else {"ttl_minutes": ttl_minutes}
    action = approvals.create(kind=kind, summary=f"{summary} — proposed via {source}",
                              params=clean, **kwargs)
    if not slack_integration.send_action_approval_request(action):
        approvals.decide(action.id, "failed", "system", result={"message": "Slack send failed"})
        raise ProposalError("Couldn't send the Slack approval message — check SLACK_WEBHOOK_URL")
    return {"id": action.id, "status": "pending", "expires_at": action.expires_at, "duplicate": False}
