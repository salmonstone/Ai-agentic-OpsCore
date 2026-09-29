"""
GitHub Actions — read workflow runs, failed jobs, and their logs.

Read-only. Auth, first match wins:
  1. GITHUB_TOKEN from the environment (e.g. `agent secrets set GITHUB_TOKEN`)
  2. the GitHub CLI's own login (`gh auth token`), the same way AWS commands
     use your configured AWS profile rather than a copied key
A token needs `repo` (private repos) or just public access plus
`actions:read`. Log downloads require auth even for public repos.

Repo, first match wins: an explicit `owner/name`, GITHUB_REPO, then the
`origin` remote of the current git checkout.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import httpx

from agent.observability.logging import get_logger

log = get_logger(__name__)

_API = "https://api.github.com"
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ", re.M)
_REMOTE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


class GitHubError(Exception):
    pass


# ---------------------------------------------------------------------------
# auth + repo resolution
# ---------------------------------------------------------------------------

def _token() -> str | None:
    tok = os.getenv("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    tok = out.stdout.strip()
    return tok if out.returncode == 0 and tok else None


def parse_remote(url: str) -> str | None:
    """'owner/name' from an https or ssh GitHub remote URL, else None."""
    m = _REMOTE.search((url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def default_repo() -> str | None:
    repo = os.getenv("GITHUB_REPO", "").strip()
    if repo:
        return repo
    try:
        out = subprocess.run(["git", "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_remote(out.stdout) if out.returncode == 0 else None


def resolve_repo(repo: str | None) -> str:
    repo = (repo or "").strip() or default_repo()
    if not repo or "/" not in repo:
        raise GitHubError("No repo given. Pass owner/name, set GITHUB_REPO, "
                          "or run inside a checkout whose origin is on GitHub.")
    return repo


def _get(path: str, **params) -> httpx.Response:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = _token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = httpx.get(f"{_API}{path}", headers=headers, params=params or None,
                      timeout=30, follow_redirects=True)
    except httpx.HTTPError as e:
        raise GitHubError(f"Couldn't reach GitHub: {e}") from e
    if r.status_code in (401, 403):
        raise GitHubError(
            f"GitHub refused access ({r.status_code}). "
            + ("Rate-limited — set GITHUB_TOKEN or log in with `gh auth login`."
               if r.headers.get("x-ratelimit-remaining") == "0"
               else "Check the token has access to this repo (`gh auth status`)."))
    if r.status_code == 404:
        raise GitHubError(f"Not found: {path} — wrong repo/run id, or no access to a private repo.")
    if r.status_code >= 400:
        raise GitHubError(f"GitHub API error {r.status_code}: {r.text[:200]}")
    return r


# ---------------------------------------------------------------------------
# runs, jobs, logs
# ---------------------------------------------------------------------------

def _run_row(r: dict) -> dict:
    return {
        "id": r.get("id"),
        "run_number": r.get("run_number"),
        "workflow": r.get("name", ""),
        "title": r.get("display_title", ""),
        "branch": r.get("head_branch", ""),
        "event": r.get("event", ""),
        "status": r.get("status", ""),
        "conclusion": r.get("conclusion"),
        "sha": (r.get("head_sha") or "")[:7],
        "created_at": r.get("created_at", ""),
        "url": r.get("html_url", ""),
    }


def list_runs(repo: str, limit: int = 10, failed_only: bool = False) -> list[dict]:
    params = {"per_page": max(1, min(limit, 100))}
    if failed_only:
        params["status"] = "failure"
    data = _get(f"/repos/{repo}/actions/runs", **params).json()
    return [_run_row(r) for r in data.get("workflow_runs", [])]


def get_run(repo: str, run_id: int) -> dict:
    return _run_row(_get(f"/repos/{repo}/actions/runs/{run_id}").json())


def latest_failed_run(repo: str) -> dict | None:
    runs = list_runs(repo, limit=1, failed_only=True)
    return runs[0] if runs else None


def get_failed_jobs(repo: str, run_id: int) -> list[dict]:
    data = _get(f"/repos/{repo}/actions/runs/{run_id}/jobs", filter="latest", per_page=100).json()
    failed = []
    for j in data.get("jobs", []):
        if j.get("conclusion") not in ("failure", "timed_out", "cancelled"):
            continue
        failed.append({
            "id": j.get("id"),
            "name": j.get("name", ""),
            "conclusion": j.get("conclusion"),
            "failed_steps": [s.get("name", "") for s in j.get("steps", []) or []
                             if s.get("conclusion") in ("failure", "timed_out")],
            "url": j.get("html_url", ""),
        })
    return failed


def clean_log(text: str, tail_chars: int = 6000) -> str:
    """Drop the per-line timestamps GitHub prefixes, keep the last tail_chars."""
    text = _TIMESTAMP.sub("", text or "")
    return text[-tail_chars:]


def get_job_log(repo: str, job_id: int, tail_chars: int = 6000) -> str:
    return clean_log(_get(f"/repos/{repo}/actions/jobs/{job_id}/logs").text, tail_chars)
