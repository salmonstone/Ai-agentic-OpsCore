"""GitHub Actions integration + diagnosis. The GitHub API and Claude are
faked — no network, no tokens, no LLM spend.
"""
from types import SimpleNamespace

import pytest

from agent.integrations import github_actions as gha
from agent.skills import github_actions as skill_mod
from agent.skills.github_actions import GitHubActionsSkill


# --- repo + auth resolution ---------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("https://github.com/salmonstone/Ai-agentic-OpsCore.git", "salmonstone/Ai-agentic-OpsCore"),
    ("https://github.com/salmonstone/Ai-agentic-OpsCore", "salmonstone/Ai-agentic-OpsCore"),
    ("git@github.com:salmonstone/infragpt.git", "salmonstone/infragpt"),
    ("https://gitlab.com/a/b.git", None),
    ("", None),
])
def test_parse_remote(url, expected):
    assert gha.parse_remote(url) == expected


def test_explicit_repo_wins(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "env/repo")
    assert gha.resolve_repo("given/repo") == "given/repo"


def test_env_repo_beats_git_remote(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "env/repo")
    assert gha.resolve_repo("") == "env/repo"


def test_no_repo_anywhere_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.setattr(gha, "default_repo", lambda: None)
    with pytest.raises(gha.GitHubError, match="No repo given"):
        gha.resolve_repo("")


def test_token_env_beats_gh_cli(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    monkeypatch.setattr(gha.shutil, "which", lambda _: (_ for _ in ()).throw(AssertionError("gh consulted")))
    assert gha._token() == "from-env"


def test_token_falls_back_to_gh_cli(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(gha.shutil, "which", lambda _: "gh")
    monkeypatch.setattr(gha.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="gho_cli_token\n"))
    assert gha._token() == "gho_cli_token"


def test_no_token_at_all_is_none_not_an_error(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(gha.shutil, "which", lambda _: None)
    assert gha._token() is None


# --- API responses -------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, json_data=None, text="", headers=None):
        self.status_code, self._json, self.text, self.headers = status, json_data or {}, text, headers or {}

    def json(self):
        return self._json


def _serve(monkeypatch, routes: dict):
    monkeypatch.setattr(gha, "_token", lambda: "t")

    def fake_get(url, **kw):
        for suffix, resp in routes.items():
            if url.endswith(suffix):
                return resp
        raise AssertionError(f"unexpected GET {url}")
    monkeypatch.setattr(gha.httpx, "get", fake_get)


RUN = {"id": 99, "run_number": 7, "name": "CI", "display_title": "add feature", "head_branch": "main",
       "event": "push", "status": "completed", "conclusion": "failure", "head_sha": "abcdef1234",
       "created_at": "2026-09-29T10:00:00Z", "html_url": "https://github.com/o/r/actions/runs/99"}


def test_list_runs_maps_fields(monkeypatch):
    _serve(monkeypatch, {"/actions/runs": FakeResponse(json_data={"workflow_runs": [RUN]})})
    (r,) = gha.list_runs("o/r")
    assert r["id"] == 99 and r["workflow"] == "CI" and r["sha"] == "abcdef1" and r["conclusion"] == "failure"


def test_failed_jobs_only_returns_failed_ones_with_their_failed_steps(monkeypatch):
    jobs = {"jobs": [
        {"id": 1, "name": "tests", "conclusion": "failure", "steps": [
            {"name": "Install", "conclusion": "success"}, {"name": "Tests", "conclusion": "failure"}]},
        {"id": 2, "name": "docker", "conclusion": "success", "steps": []},
    ]}
    _serve(monkeypatch, {"/runs/99/jobs": FakeResponse(json_data=jobs)})
    (j,) = gha.get_failed_jobs("o/r", 99)
    assert j["id"] == 1 and j["failed_steps"] == ["Tests"]


@pytest.mark.parametrize("status, headers, match", [
    (401, {}, "refused access"),
    (403, {"x-ratelimit-remaining": "0"}, "Rate-limited"),
    (404, {}, "Not found"),
    (500, {}, "API error 500"),
])
def test_http_errors_become_readable_github_errors(monkeypatch, status, headers, match):
    _serve(monkeypatch, {"/actions/runs": FakeResponse(status=status, headers=headers)})
    with pytest.raises(gha.GitHubError, match=match):
        gha.list_runs("o/r")


def test_clean_log_strips_timestamps_and_keeps_the_tail():
    raw = ("2026-09-29T10:00:01.1234567Z first line\n"
           "2026-09-29T10:00:02.1234567Z AssertionError: expected 2\n")
    assert gha.clean_log(raw) == "first line\nAssertionError: expected 2\n"
    assert gha.clean_log("x" * 50, tail_chars=10) == "x" * 10


# --- diagnosis -----------------------------------------------------------------

@pytest.fixture
def fake_github(monkeypatch):
    monkeypatch.setattr(gha, "get_run", lambda repo, rid: gha._run_row(RUN))
    monkeypatch.setattr(gha, "latest_failed_run", lambda repo: gha._run_row(RUN))
    monkeypatch.setattr(gha, "get_failed_jobs", lambda repo, rid: [
        {"id": 1, "name": "tests", "conclusion": "failure", "failed_steps": ["Tests"], "url": ""}])
    monkeypatch.setattr(gha, "get_job_log", lambda repo, jid, tail: "FAILED tests/test_x.py::test_y - assert 1 == 2")
    monkeypatch.setattr(skill_mod, "remember", lambda *a, **k: None)
    monkeypatch.setattr(skill_mod, "retrieve_context", lambda *a, **k: [])


def _fake_llm(monkeypatch, content: str):
    seen = {}

    async def chat(**kw):
        seen["prompt"] = kw["messages"][0]["content"]
        return SimpleNamespace(content=content)
    monkeypatch.setattr(skill_mod.llm, "chat", chat)
    return seen


def test_diagnose_parses_the_model_answer_and_sends_it_the_log(monkeypatch, fake_github):
    seen = _fake_llm(monkeypatch, '{"category": "test_failure", "root_cause": "test_y asserts 1 == 2", '
                                  '"confidence": "high", "suggested_fix": "fix test_y", '
                                  '"explanation": "log shows it", "rerun_likely_helps": false}')
    d = GitHubActionsSkill().diagnose("o/r", 99)
    assert d.category == "test_failure" and d.root_cause == "test_y asserts 1 == 2"
    assert d.failed_steps == ["tests › Tests"] and d.rerun_likely_helps is False
    assert "assert 1 == 2" in seen["prompt"]


def test_bad_model_values_are_normalised(monkeypatch, fake_github):
    _fake_llm(monkeypatch, '{"category": "cosmic_rays", "confidence": "certain", '
                           '"root_cause": "x", "rerun_likely_helps": "yes"}')
    d = GitHubActionsSkill().diagnose("o/r", 99)
    assert d.category == "unknown" and d.confidence == "low"
    assert d.rerun_likely_helps is False     # only a real JSON true counts


def test_a_run_that_did_not_fail_skips_the_model(monkeypatch, fake_github):
    monkeypatch.setattr(gha, "get_run", lambda repo, rid: {**gha._run_row(RUN), "conclusion": "success"})
    monkeypatch.setattr(skill_mod.llm, "chat", lambda **kw: (_ for _ in ()).throw(AssertionError("LLM called")))
    d = GitHubActionsSkill().diagnose("o/r", 99)
    assert "didn't fail" in d.root_cause


def test_no_failed_runs_at_all(monkeypatch, fake_github):
    monkeypatch.setattr(gha, "latest_failed_run", lambda repo: None)
    assert GitHubActionsSkill().diagnose("o/r", 0).root_cause == "No failed runs found."


def test_mcp_tool_returns_error_instead_of_raising(monkeypatch):
    import asyncio

    from agent.mcp_server import github_actions_runs
    monkeypatch.setattr(gha, "resolve_repo", lambda r: (_ for _ in ()).throw(gha.GitHubError("No repo given")))
    assert asyncio.run(github_actions_runs())["error"] == "No repo given"
