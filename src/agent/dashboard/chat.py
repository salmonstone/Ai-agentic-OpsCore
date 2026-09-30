"""
Dashboard assistant — the chat bubble in the AtlasOS dashboard.

A third front end on the shared skill layer; the CLI and the MCP server are
the other two. Claude decides which AtlasOS tool answers a question, and this
module runs it through the exact FastMCP registry mcp_server.py serves to
ChatGPT: in-process, via mcp.call_tool(). So every tool's own checks, audit
trail and confirm=True gate apply here too. Nothing is reimplemented.

Tools declared with @_mutating_tool() never run on the model's say-so. The
loop stops at that tool call and the UI shows a Confirm card; only a human
click resumes it, with confirm=True added here. The model never sees the
confirm parameters (they're stripped from every schema and from its
arguments), so it can't set them itself. confirm_destructive_name is never
added by this module: a job whose name looks destructive still refuses,
exactly as the tool documents.

Everything streams as small JSON events (see _loop) that the server sends
as Server-Sent Events. Conversation state lives in memory, per session.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator

from agent.observability.logging import get_logger

log = get_logger(__name__)

MAX_STEPS = 10                  # model <-> tool round trips per user message
MAX_TOKENS = 2048
TOOL_TIMEOUT_S = 180
RESULT_TO_MODEL_CHARS = 16_000
RESULT_TO_UI_CHARS = 4_000
MAX_SESSIONS = 20
MAX_HISTORY = 40                # messages kept per session, cut at a user turn
_CONFIRM_PARAMS = ("confirm", "confirm_destructive_name")
_FAIL_FLAGS = ("applied", "triggered", "success", "ok")


@dataclass
class _Pending:
    tool_uses: list[dict]                   # every tool_use of the paused turn, in order
    results: dict[str, dict]                # tool_use_id -> tool_result block
    decisions: dict[str, bool | None]       # mutating tool_use_id -> approve / reject / undecided


@dataclass
class _Session:
    id: str
    messages: list[dict] = field(default_factory=list)
    pending: _Pending | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_SESSIONS: OrderedDict[str, _Session] = OrderedDict()
_tools_cache: list[dict] | None = None
_tool_props: dict[str, set[str]] = {}


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def _mutating() -> set[str]:
    from agent.mcp_server import MUTATING_TOOLS
    return MUTATING_TOOLS


async def _load_tools() -> list[dict]:
    """Anthropic tool specs built from the live MCP registry, confirm params removed."""
    global _tools_cache
    if _tools_cache is not None:
        return _tools_cache
    from agent.mcp_server import mcp

    mutating = _mutating()
    specs = []
    for t in await mcp.list_tools():
        schema = json.loads(json.dumps(t.inputSchema or {"type": "object", "properties": {}}))
        props = schema.setdefault("properties", {})
        _tool_props[t.name] = set(props)
        for p in _CONFIRM_PARAMS:
            props.pop(p, None)
        if "required" in schema:
            schema["required"] = [r for r in schema["required"] if r not in _CONFIRM_PARAMS]
        desc = (t.description or t.name).strip()
        if t.name in mutating:
            desc += ("\n\nThis changes infrastructure. When the user wants the change, call it: "
                     "the dashboard shows them a Confirm card and only runs it after they click. "
                     "Never pass confirm arguments.")
        specs.append({"name": t.name, "description": desc, "input_schema": schema})
    _tools_cache = specs
    return specs


async def tools_info() -> dict:
    """What the assistant can do, for the UI: every tool and whether it asks first."""
    specs = await _load_tools()
    mutating = _mutating()
    tools = [{"name": s["name"], "description": s["description"].split("\n")[0][:200],
              "mutating": s["name"] in mutating} for s in specs]
    return {"mutations_enabled": any(t["mutating"] for t in tools), "tools": tools}


def _content_text(out) -> str:
    if isinstance(out, tuple):          # (content blocks, structured) on newer SDKs
        out = out[0]
    if isinstance(out, (list, tuple)):
        return "\n".join(getattr(b, "text", None) or str(b) for b in out)
    return out if isinstance(out, str) else json.dumps(out, default=str)


def _outcome(ok: bool, text: str) -> tuple[bool, str]:
    """Did a mutating call actually do its job? A tool can return normally with
    {"applied": false, "message": ...} — that's a failure for the Confirm card."""
    if not ok:
        return False, text[:300]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return True, text[:300]
    if isinstance(data, dict):
        if any(data.get(k) is False for k in _FAIL_FLAGS):
            return False, str(data.get("message") or data.get("error") or text)[:300]
        return True, str(data.get("message") or "")[:300]
    return True, ""


async def _run_tool(tu: dict, confirmed: bool) -> tuple[dict, dict, bool, str]:
    """Run one tool call. Returns (tool_result block, tool_end event, ok, text)."""
    from agent.mcp_server import mcp

    args = {k: v for k, v in (tu.get("input") or {}).items() if k not in _CONFIRM_PARAMS}
    if confirmed and "confirm" in _tool_props.get(tu["name"], ()):
        args["confirm"] = True
    t0 = time.perf_counter()
    try:
        out = await asyncio.wait_for(mcp.call_tool(tu["name"], args), TOOL_TIMEOUT_S)
        text, ok = _content_text(out), True
    except asyncio.TimeoutError:
        text, ok = f"{tu['name']} timed out after {TOOL_TIMEOUT_S}s", False
    except Exception as exc:
        text, ok = str(exc), False
    ms = int((time.perf_counter() - t0) * 1000)
    log.info("dashboard_chat.tool", tool=tu["name"], ok=ok, ms=ms, confirmed=confirmed)

    for_model = text if len(text) <= RESULT_TO_MODEL_CHARS else (
        text[:RESULT_TO_MODEL_CHARS] + f"\n…[truncated {len(text) - RESULT_TO_MODEL_CHARS} chars]")
    block = {"type": "tool_result", "tool_use_id": tu["id"], "content": for_model, "is_error": not ok}
    event = {"type": "tool_end", "id": tu["id"], "name": tu["name"], "ok": ok, "ms": ms,
             "result": text[:RESULT_TO_UI_CHARS]}
    return block, event, ok, text


def _cancelled(tu: dict, why: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tu["id"], "content": why, "is_error": True}


def _confirm_title(tu: dict) -> str:
    args = ", ".join(f"{k}={v}" for k, v in (tu.get("input") or {}).items()
                     if k not in _CONFIRM_PARAMS)
    return f"Run {tu['name']}" + (f" · {args}" if args else "")


def _audit(tu: dict, applied: bool, message: str) -> None:
    try:
        from agent.memory.retrieval import remember
        args = {k: v for k, v in (tu.get("input") or {}).items() if k not in _CONFIRM_PARAMS}
        remember(f"Dashboard assistant: user confirmed {tu['name']}({args}) -> "
                 f"{'APPLIED' if applied else 'FAILED'} {message}".strip(),
                 source="dashboard_chat", metadata={"tool": tu["name"], "applied": applied})
    except Exception as exc:
        log.warning("dashboard_chat.audit_failed", error=str(exc))


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

def _session(session_id: str) -> _Session:
    s = _SESSIONS.get(session_id)
    if s is None:
        s = _SESSIONS[session_id] = _Session(session_id)
        while len(_SESSIONS) > MAX_SESSIONS:
            _SESSIONS.popitem(last=False)
    _SESSIONS.move_to_end(session_id)
    return s


def clear(session_id: str) -> None:
    _SESSIONS.pop(session_id, None)


def _append_user(s: _Session, blocks: list[dict]) -> None:
    if s.messages and s.messages[-1]["role"] == "user":
        s.messages[-1]["content"].extend(blocks)
    else:
        s.messages.append({"role": "user", "content": blocks})


def _repair(s: _Session) -> None:
    """A stream cut off mid-tool-call (tab closed) can leave an assistant
    tool_use with no tool_result, which the API rejects. Close it out."""
    if s.pending or not s.messages or s.messages[-1]["role"] != "assistant":
        return
    orphans = [b for b in s.messages[-1]["content"] if b["type"] == "tool_use"]
    if orphans:
        _append_user(s, [_cancelled(tu, "Interrupted before it ran. Nothing was changed.") for tu in orphans])


def _close_pending(s: _Session, why: str) -> list[dict]:
    p, s.pending = s.pending, None
    return [p.results.get(tu["id"]) or _cancelled(tu, why) for tu in p.tool_uses]


def _is_turn_start(m: dict) -> bool:
    return m["role"] == "user" and not any(b.get("type") == "tool_result" for b in m["content"])


def _trim(s: _Session) -> None:
    if len(s.messages) <= MAX_HISTORY:
        return
    for i in range(len(s.messages) - MAX_HISTORY, len(s.messages)):
        if _is_turn_start(s.messages[i]):
            s.messages = s.messages[i:]
            return


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

def _system_prompt() -> str:
    try:
        from agent.integrations.kubectl import get_current_context
        ctx = get_current_context() or "none"
    except Exception:
        ctx = "unknown"
    return (
        "You are the AtlasOS Assistant, inside the AtlasOS dashboard. AtlasOS is one engineer's "
        "DevOps agent: it watches their Kubernetes cluster, Jenkins, GitHub Actions and AWS.\n\n"
        "- Answer by calling AtlasOS tools. Never guess the state of the cluster, builds or spend.\n"
        "- If a tool fails, times out, or reports the cluster/service unreachable, say you couldn't "
        "check it. Never present that as healthy or as 'no problems'.\n"
        "- Tools that change infrastructure pause for the user's Confirm click in the dashboard. "
        "When the user wants a change, call the tool; the Confirm card is the question, so don't ask "
        "'shall I?' first. Never pass confirm arguments. Never say something was applied until a tool "
        "result says so.\n"
        "- Plain text only: no markdown headers, tables or bold. Short lines and simple lists are fine. "
        "Use exact names (pods, namespaces, jobs, build numbers). Be concise.\n\n"
        f"Current kube context: {ctx}. Today: {datetime.now(timezone.utc):%Y-%m-%d} (UTC)."
    )


def _log_usage(model: str, final, t0: float) -> None:
    try:
        from agent.observability.costs import log_usage
        log_usage(model=model, input_tokens=final.usage.input_tokens,
                  output_tokens=final.usage.output_tokens,
                  latency_ms=(time.perf_counter() - t0) * 1000)
    except Exception:
        pass


async def _loop(s: _Session) -> AsyncIterator[dict]:
    """Model <-> tools until an answer, a Confirm pause, or an error.

    Events: text {delta} · tool_start {id,name,input} · tool_end {id,name,ok,ms,result}
            · confirm {id,name,title,input} · error {message,detail} · done {stop}
    """
    from agent.config import settings
    from agent.core.llm import client

    try:
        tools = await _load_tools()
        system = await asyncio.to_thread(_system_prompt)
    except Exception as exc:
        yield {"type": "error", "message": "The assistant couldn't load AtlasOS tools.", "detail": str(exc)[:300]}
        return
    mutating = _mutating()

    for _ in range(MAX_STEPS):
        t0 = time.perf_counter()
        try:
            async with client().messages.stream(
                model=settings.llm_model, max_tokens=MAX_TOKENS, system=system,
                tools=tools, messages=s.messages,
            ) as stream:
                async for delta in stream.text_stream:
                    yield {"type": "text", "delta": delta}
                final = await stream.get_final_message()
        except Exception as exc:
            log.warning("dashboard_chat.llm_failed", error=str(exc))
            yield {"type": "error", "message": "The assistant couldn't reach Claude.", "detail": str(exc)[:300]}
            return
        _log_usage(settings.llm_model, final, t0)

        content = []
        for b in final.content:
            if b.type == "text" and b.text:
                content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input or {}})
        s.messages.append({"role": "assistant", "content": content or [{"type": "text", "text": "…"}]})

        tool_uses = [c for c in content if c["type"] == "tool_use"]
        if final.stop_reason != "tool_use" or not tool_uses:
            yield {"type": "done", "stop": "end_turn"}
            return

        results: dict[str, dict] = {}
        paused = []
        for tu in tool_uses:
            if tu["name"] in mutating:
                paused.append(tu)
                continue
            yield {"type": "tool_start", "id": tu["id"], "name": tu["name"], "input": tu["input"]}
            block, event, _, _ = await _run_tool(tu, confirmed=False)
            results[tu["id"]] = block
            yield event

        if paused:
            s.pending = _Pending(tool_uses, results, {tu["id"]: None for tu in paused})
            for tu in paused:
                yield {"type": "confirm", "id": tu["id"], "name": tu["name"],
                       "title": _confirm_title(tu), "input": tu["input"]}
            yield {"type": "done", "stop": "awaiting_confirm"}
            return

        _append_user(s, [results[tu["id"]] for tu in tool_uses])

    yield {"type": "error", "message": f"Stopped after {MAX_STEPS} tool rounds without an answer.", "detail": ""}


async def send(session_id: str, text: str) -> AsyncIterator[dict]:
    """A new user message. Abandons any action still waiting for a Confirm."""
    s = _session(session_id)
    async with s.lock:
        _repair(s)
        blocks: list[dict] = []
        if s.pending:
            undecided = [i for i, d in s.pending.decisions.items() if d is None]
            blocks = _close_pending(s, "The user moved on without confirming. Nothing was changed.")
            for i in undecided:
                yield {"type": "confirm_result", "id": i, "status": "cancelled", "message": ""}
        blocks.append({"type": "text", "text": text})
        _append_user(s, blocks)
        _trim(s)
        async for ev in _loop(s):
            yield ev


async def decide(session_id: str, tool_use_id: str, approve: bool) -> AsyncIterator[dict]:
    """A Confirm or Cancel click on one paused action. Once every paused action
    in the turn is decided, runs the confirmed ones and lets Claude continue."""
    s = _SESSIONS.get(session_id)
    if s is None or s.pending is None or tool_use_id not in s.pending.decisions:
        yield {"type": "error", "message": "That action is no longer waiting for you.", "detail": ""}
        return
    async with s.lock:
        p = s.pending
        if p is None or p.decisions.get(tool_use_id) is not None:
            yield {"type": "done", "stop": "noop"}
            return
        p.decisions[tool_use_id] = approve
        if any(d is None for d in p.decisions.values()):
            yield {"type": "done", "stop": "awaiting_confirm"}
            return

        for tu in p.tool_uses:
            if tu["id"] not in p.decisions:
                continue
            if not p.decisions[tu["id"]]:
                p.results[tu["id"]] = _cancelled(tu, "The user clicked Cancel. Nothing was changed.")
                yield {"type": "confirm_result", "id": tu["id"], "status": "cancelled", "message": ""}
                continue
            yield {"type": "confirm_result", "id": tu["id"], "status": "applying", "message": ""}
            yield {"type": "tool_start", "id": tu["id"], "name": tu["name"], "input": tu["input"]}
            block, event, ok, text = await _run_tool(tu, confirmed=True)
            p.results[tu["id"]] = block
            yield event
            applied, message = _outcome(ok, text)
            _audit(tu, applied, message)
            yield {"type": "confirm_result", "id": tu["id"],
                   "status": "applied" if applied else "failed", "message": message}

        _append_user(s, _close_pending(s, "Nothing was changed."))
        async for ev in _loop(s):
            yield ev
