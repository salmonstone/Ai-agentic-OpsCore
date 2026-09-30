"""JenkinsSkill.scan must not report an unreachable Jenkins as healthy.

Found live: with a Wi-Fi captive portal answering for Jenkins, get_all_jobs()
returned [], the scan reported "0 failing jobs, health 100/100", and the
dashboard assistant repeated that to the user.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agent.skills import jenkins as js


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(js, "remember", lambda *a, **k: None)
    monkeypatch.setattr(js.jk, "get_offline_nodes", lambda: [])
    monkeypatch.setattr(js.jk, "get_stuck_queue_items", lambda: [])


def test_unreachable_jenkins_raises_instead_of_scoring_100(monkeypatch):
    monkeypatch.setattr(js.jk, "get_all_jobs", lambda: [])
    monkeypatch.setattr(js.jk, "get_connection_info",
                        lambda: SimpleNamespace(connected=False, error="HTTP 302: captive portal"))
    with pytest.raises(RuntimeError, match="Couldn't reach Jenkins.*302"):
        js.JenkinsSkill().scan()


def test_reachable_jenkins_with_no_jobs_still_scans(monkeypatch):
    monkeypatch.setattr(js.jk, "get_all_jobs", lambda: [])
    monkeypatch.setattr(js.jk, "get_connection_info", lambda: SimpleNamespace(connected=True, error=None))
    report = js.JenkinsSkill().scan()
    assert report.total_jobs == 0 and report.failing_jobs == 0


def test_mcp_tool_surfaces_the_error(monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError

    from agent import mcp_server as m
    monkeypatch.setattr(js.jk, "get_all_jobs", lambda: [])
    monkeypatch.setattr(js.jk, "get_connection_info",
                        lambda: SimpleNamespace(connected=False, error="connection refused"))
    with pytest.raises(ToolError, match="Couldn't reach Jenkins"):
        asyncio.run(m.mcp.call_tool("jenkins_scan", {}))
