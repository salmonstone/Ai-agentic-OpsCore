"""The read-only k8s MCP tools must say "couldn't reach the cluster" instead
of returning an empty list when the API server is down.

Found live: the dashboard assistant was asked "is my cluster healthy?" while
the cluster was unreachable, got [] back, and answered "healthy". The same
tools serve ChatGPT over MCP, so the fix lives in mcp_server.py.
"""
import asyncio

import pytest

from agent import mcp_server as m


@pytest.fixture
def cluster_down(monkeypatch):
    monkeypatch.setattr("agent.integrations.kubectl.is_cluster_available", lambda: False)

    def must_not_run(*a, **k):
        raise AssertionError("kubectl collectors must not run when the cluster is unreachable")
    for fn in ("get_pods", "get_nodes_detail", "get_pod_metrics"):
        monkeypatch.setattr(f"agent.integrations.kubectl.{fn}", must_not_run)


@pytest.mark.parametrize("call", [
    lambda: m.k8s_list_pods(),
    lambda: m.k8s_list_nodes(),
    lambda: m.k8s_pod_metrics(),
])
def test_list_tools_return_an_explicit_unreachable_marker(cluster_down, call):
    out = asyncio.run(call())
    assert len(out) == 1 and out[0]["error"] == "cluster_unreachable"
    assert "NOT an empty result" in out[0]["message"]


def test_diagnose_returns_the_marker(cluster_down, monkeypatch):
    monkeypatch.setattr("agent.skills.k8s.K8sSkill.diagnose_pod",
                        lambda self, p: (_ for _ in ()).throw(AssertionError("must not diagnose")))
    out = asyncio.run(m.k8s_diagnose("api-7f9c", "prod"))
    assert out["error"] == "cluster_unreachable"


def test_reachable_cluster_still_lists_normally(monkeypatch):
    monkeypatch.setattr("agent.integrations.kubectl.is_cluster_available", lambda: True)
    monkeypatch.setattr("agent.integrations.kubectl.get_nodes_detail", lambda: [])
    assert asyncio.run(m.k8s_list_nodes()) == []


def test_the_helper_is_not_itself_a_tool():
    names = {t.name for t in asyncio.run(m.mcp.list_tools())}
    assert "_cluster_unreachable" not in names
