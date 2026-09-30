"""Dashboard assistant (agent/dashboard/chat.py): the tool-use loop, and above
all the rule that a mutating tool never runs without a human Confirm click.

Claude and the MCP registry are both faked — no network, no cluster. Each
fake Claude "turn" is what the model would return for one API call; the test
asserts on the events the UI receives and on what reaches the tools.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.dashboard import chat

MUTATING = "k8s_crashloop_apply_fix"


# --- fakes -------------------------------------------------------------------

def text(t):
    return SimpleNamespace(type="text", text=t)


def use(id_, name, **inp):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


def turn(*blocks, stop="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


class FakeStream:
    def __init__(self, t):
        self.t = t

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for b in self.t.content:
                if b.type == "text":
                    yield b.text
        return gen()

    async def get_final_message(self):
        return self.t


class FakeClaude:
    def __init__(self):
        self.turns, self.requests = [], []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kw):
        self.requests.append(json.loads(json.dumps({"messages": kw["messages"], "tools": kw["tools"]})))
        nxt = self.turns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeStream(nxt)


class FakeMCP:
    def __init__(self):
        self.calls = []
        self.fail = set()

    async def list_tools(self):
        return [
            SimpleNamespace(name="k8s_scan", description="Scan the cluster.",
                            inputSchema={"type": "object", "properties": {"namespace": {"type": "string"}}}),
            SimpleNamespace(name=MUTATING, description="Restart a crashlooping pod.", inputSchema={
                "type": "object", "required": ["pod", "confirm"],
                "properties": {"pod": {"type": "string"},
                               "confirm": {"type": "boolean", "default": False},
                               "confirm_destructive_name": {"type": "boolean", "default": False}}}),
        ]

    async def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        if name in self.fail:
            raise RuntimeError("dial tcp 10.0.12.4:443: i/o timeout")
        if name == "k8s_scan":
            return [SimpleNamespace(text='[{"pod": "api-7f9c", "status": "CrashLoopBackOff"}]')]
        return [SimpleNamespace(text=json.dumps(
            {"applied": args.get("confirm") is True, "message": "restarted api-7f9c"}))]


@pytest.fixture
def env(monkeypatch):
    claude, mcp = FakeClaude(), FakeMCP()
    monkeypatch.setattr("agent.mcp_server.mcp", mcp)
    monkeypatch.setattr("agent.mcp_server.MUTATING_TOOLS", {MUTATING})
    monkeypatch.setattr("agent.core.llm.client", lambda: claude)
    monkeypatch.setattr(chat, "_system_prompt", lambda: "system")
    monkeypatch.setattr(chat, "_tools_cache", None)
    monkeypatch.setattr("agent.memory.retrieval.remember", lambda *a, **k: None)
    chat._SESSIONS.clear()
    return SimpleNamespace(claude=claude, mcp=mcp)


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


def types(events):
    return [e["type"] for e in events]


# --- read-only questions -----------------------------------------------------

def test_read_tool_runs_and_the_answer_streams(env):
    env.claude.turns = [turn(use("t1", "k8s_scan"), stop="tool_use"),
                        turn(text("api-7f9c is crashlooping."))]
    ev = run(chat.send("s", "What's broken?"))
    assert types(ev) == ["tool_start", "tool_end", "text", "done"]
    assert ev[1]["ok"] is True and "CrashLoopBackOff" in ev[1]["result"]
    assert ev[-1]["stop"] == "end_turn"
    # the tool's output went back to the model as the next user turn
    last = env.claude.requests[1]["messages"][-1]
    assert last["role"] == "user" and last["content"][0]["tool_use_id"] == "t1"


def test_model_never_sees_confirm_parameters(env):
    env.claude.turns = [turn(text("hi"))]
    run(chat.send("s", "hello"))
    tools = {t["name"]: t for t in env.claude.requests[0]["tools"]}
    schema = tools[MUTATING]["input_schema"]
    assert set(schema["properties"]) == {"pod"}
    assert schema["required"] == ["pod"]
    assert "Confirm" in tools[MUTATING]["description"]


def test_a_failing_tool_is_reported_not_hidden(env):
    env.mcp.fail.add("k8s_scan")
    env.claude.turns = [turn(use("t1", "k8s_scan"), stop="tool_use"),
                        turn(text("I couldn't check the cluster."))]
    ev = run(chat.send("s", "scan"))
    end = next(e for e in ev if e["type"] == "tool_end")
    assert end["ok"] is False and "i/o timeout" in end["result"]
    result = env.claude.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True


# --- mutating tools need a human ---------------------------------------------

def test_mutating_call_pauses_and_nothing_runs(env):
    env.claude.turns = [turn(text("Restarting it."), use("m1", MUTATING, pod="api-7f9c"), stop="tool_use")]
    ev = run(chat.send("s", "fix the api pod"))
    assert types(ev) == ["text", "confirm", "done"]
    assert ev[1]["name"] == MUTATING and ev[1]["input"] == {"pod": "api-7f9c"}
    assert ev[-1]["stop"] == "awaiting_confirm"
    assert env.mcp.calls == []


def test_confirm_runs_it_with_confirm_true_then_claude_continues(env):
    env.claude.turns = [turn(use("m1", MUTATING, pod="api-7f9c"), stop="tool_use"),
                        turn(text("Done, it's Running."))]
    run(chat.send("s", "fix it"))
    ev = run(chat.decide("s", "m1", approve=True))
    assert env.mcp.calls == [(MUTATING, {"pod": "api-7f9c", "confirm": True})]
    results = [e for e in ev if e["type"] == "confirm_result"]
    assert [r["status"] for r in results] == ["applying", "applied"]
    assert types(ev)[-2:] == ["text", "done"]


def test_cancel_runs_nothing_and_tells_claude(env):
    env.claude.turns = [turn(use("m1", MUTATING, pod="api-7f9c"), stop="tool_use"),
                        turn(text("OK, left it alone."))]
    run(chat.send("s", "fix it"))
    ev = run(chat.decide("s", "m1", approve=False))
    assert env.mcp.calls == []
    assert {"type": "confirm_result", "id": "m1", "status": "cancelled", "message": ""} in ev
    result = env.claude.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "Cancel" in result["content"]


def test_model_supplied_confirm_is_stripped(env):
    """Claude can't approve its own action by passing confirm=True."""
    env.claude.turns = [turn(use("m1", MUTATING, pod="p", confirm=True, confirm_destructive_name=True),
                             stop="tool_use")]
    ev = run(chat.send("s", "fix"))
    assert types(ev)[-1] == "done" and ev[-1]["stop"] == "awaiting_confirm"
    assert env.mcp.calls == []


def test_confirm_destructive_name_is_never_added(env):
    env.claude.turns = [turn(use("m1", MUTATING, pod="p", confirm_destructive_name=True), stop="tool_use"),
                        turn(text("done"))]
    run(chat.send("s", "fix"))
    run(chat.decide("s", "m1", approve=True))
    assert env.mcp.calls == [(MUTATING, {"pod": "p", "confirm": True})]


def test_tool_saying_not_applied_shows_as_failed(env, monkeypatch):
    async def refuse(name, args):
        return [SimpleNamespace(text=json.dumps({"applied": False, "message": "exceeded quota in prod"}))]
    monkeypatch.setattr(env.mcp, "call_tool", refuse)
    env.claude.turns = [turn(use("m1", MUTATING, pod="p"), stop="tool_use"), turn(text("It failed."))]
    run(chat.send("s", "fix"))
    ev = run(chat.decide("s", "m1", approve=True))
    final = [e for e in ev if e["type"] == "confirm_result"][-1]
    assert final["status"] == "failed" and "quota" in final["message"]


def test_mixed_turn_runs_reads_now_and_keeps_tool_result_order(env):
    env.claude.turns = [turn(use("r1", "k8s_scan"), use("m1", MUTATING, pod="p"), stop="tool_use"),
                        turn(text("done"))]
    ev = run(chat.send("s", "scan and fix"))
    assert types(ev) == ["tool_start", "tool_end", "confirm", "done"]
    assert env.mcp.calls == [("k8s_scan", {})]
    run(chat.decide("s", "m1", approve=True))
    results = env.claude.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["r1", "m1"]


def test_new_message_abandons_the_pending_action(env):
    env.claude.turns = [turn(use("m1", MUTATING, pod="p"), stop="tool_use"),
                        turn(text("Sure, here's the cost."))]
    run(chat.send("s", "fix it"))
    ev = run(chat.send("s", "never mind, what's AWS spend?"))
    assert ev[0] == {"type": "confirm_result", "id": "m1", "status": "cancelled", "message": ""}
    assert env.mcp.calls == []
    last = env.claude.requests[1]["messages"][-1]["content"]
    assert last[0]["type"] == "tool_result" and last[0]["tool_use_id"] == "m1"
    assert last[1] == {"type": "text", "text": "never mind, what's AWS spend?"}


def test_deciding_an_unknown_action_is_an_error(env):
    ev = run(chat.decide("nope", "m1", approve=True))
    assert types(ev) == ["error"]
    assert env.mcp.calls == []


def test_claude_unreachable_is_an_error_event(env):
    env.claude.turns = [RuntimeError("401 invalid x-api-key")]
    ev = run(chat.send("s", "hi"))
    assert types(ev) == ["error"] and "401" in ev[0]["detail"]


def test_clear_forgets_the_session(env):
    env.claude.turns = [turn(use("m1", MUTATING, pod="p"), stop="tool_use")]
    run(chat.send("s", "fix"))
    chat.clear("s")
    assert types(run(chat.decide("s", "m1", approve=True))) == ["error"]


def test_history_trim_keeps_a_valid_turn_boundary():
    s = chat._Session("s")
    for i in range(30):
        s.messages += [{"role": "user", "content": [{"type": "text", "text": f"q{i}"}]},
                       {"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]}]
    chat._trim(s)
    assert len(s.messages) <= chat.MAX_HISTORY
    assert chat._is_turn_start(s.messages[0])


# --- the real registry -------------------------------------------------------

def test_every_mutating_tool_is_tagged_in_the_real_registry():
    """The sets the assistant relies on must match what mcp_server actually
    marks as mutating — including the look-read-only-but-aren't ones."""
    from agent.mcp_server import MUTATING_TOOLS
    assert {"k8s_crashloop_apply_fix", "jenkins_trigger_build", "jenkins_apply_fix",
            "dns_scan", "security_drift", "incident_correlate"} <= MUTATING_TOOLS
    assert "k8s_scan" not in MUTATING_TOOLS and "jenkins_scan" not in MUTATING_TOOLS
