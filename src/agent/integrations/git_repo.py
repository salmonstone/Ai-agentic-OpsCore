"""
Read-only git client. No AI. No business logic. No writes, ever.

Named git_repo rather than git so it cannot shadow the `git` module that
GitPython installs — this project shells out instead of taking that
dependency, exactly like kubectl.py does.

Why this exists: "what changed?" is the first question of every real root
-cause analysis, and the cluster only knows half the answer. It can tell you
a Deployment's image went from :a3f19c to :b7e220 at 11:09; only the repo can
tell you that b7e220 changed the database connection pool size and who
merged it.

Every function returns an empty result on any failure — not a git repo, git
not installed, detached HEAD, shallow clone — and never raises.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from datetime import datetime, timezone

from agent.observability.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = 15
_SEP = "\x1f"       # unit separator — safe inside commit subjects, unlike |


def _repo_path(path: str = "") -> str:
    if path:
        return path
    try:
        from agent.config import settings
        return settings.git_repo_path or "."
    except Exception:
        return "."


def _run_git(args: list[str], path: str = "") -> tuple[bool, str]:
    """Run one read-only git command. Returns (success, stdout)."""
    if not shutil.which("git"):
        log.debug("git.not_found")
        return False, ""

    cwd = _repo_path(path)
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.warning("git.timeout", args=" ".join(args[:3]))
        return False, ""
    except Exception as e:
        log.warning("git.failed", args=" ".join(args[:3]), error=str(e)[:200])
        return False, ""

    if proc.returncode != 0:
        log.debug("git.nonzero", args=" ".join(args[:3]), stderr=proc.stderr[:200])
        return False, ""
    return True, proc.stdout


def is_repo(path: str = "") -> bool:
    ok, out = _run_git(["rev-parse", "--is-inside-work-tree"], path)
    return ok and out.strip() == "true"


def current_sha(path: str = "", short: bool = True) -> str:
    args = ["rev-parse", "--short", "HEAD"] if short else ["rev-parse", "HEAD"]
    ok, out = _run_git(args, path)
    return out.strip() if ok else ""


def current_branch(path: str = "") -> str:
    ok, out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    return out.strip() if ok else ""


def _parse_log(raw: str) -> list[dict]:
    commits = []
    for line in raw.splitlines():
        parts = line.split(_SEP)
        if len(parts) != 5:
            continue
        sha, author, iso, subject, body = parts
        try:
            ts = datetime.fromisoformat(iso).timestamp()
        except ValueError:
            ts = 0.0
        commits.append({
            "sha": sha, "author": author, "at": iso, "ts": ts,
            "subject": subject, "body": body[:500],
        })
    return commits


def recent_commits(minutes: int = 120, path: str = "", limit: int = 50) -> list[dict]:
    """Commits authored within the last `minutes`.

    Uses --since rather than -n so the result is a real time window: a repo
    that had 200 commits in the window returns all of them (up to limit),
    and a quiet repo correctly returns nothing rather than stale history.
    """
    fmt = _SEP.join(["%h", "%an", "%aI", "%s", "%b"]).replace("\n", " ")
    ok, out = _run_git([
        "log", f"--since={minutes} minutes ago", f"-n{limit}",
        f"--pretty=format:{fmt}", "--no-merges",
    ], path)
    if not ok:
        return []
    return _parse_log(out)


def commits_between(old_ref: str, new_ref: str, path: str = "",
                    limit: int = 100) -> list[dict]:
    """Commits contained in new_ref but not old_ref — the exact diff a
    deployment shipped, when both image tags carry a git sha."""
    if not old_ref or not new_ref:
        return []
    fmt = _SEP.join(["%h", "%an", "%aI", "%s", "%b"]).replace("\n", " ")
    ok, out = _run_git([
        "log", f"{old_ref}..{new_ref}", f"-n{limit}",
        f"--pretty=format:{fmt}", "--no-merges",
    ], path)
    if not ok:
        return []
    return _parse_log(out)


def commit_info(ref: str, path: str = "") -> dict | None:
    """One commit by sha/tag/ref."""
    if not ref:
        return None
    fmt = _SEP.join(["%h", "%an", "%aI", "%s", "%b"]).replace("\n", " ")
    ok, out = _run_git(["show", "-s", f"--pretty=format:{fmt}", ref], path)
    if not ok:
        return None
    parsed = _parse_log(out)
    return parsed[0] if parsed else None


def files_changed(ref: str, path: str = "", limit: int = 60) -> list[str]:
    """Paths touched by a commit — cheap blast-radius signal. A commit that
    only edits docs/ is far less interesting than one touching charts/ or a
    migration."""
    if not ref:
        return []
    ok, out = _run_git(["show", "--name-only", "--pretty=format:", ref], path)
    if not ok:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()][:limit]


def sha_from_image(image: str) -> str:
    """Best-effort extraction of a git sha from a container image reference.

    Handles the tagging conventions that actually appear in the wild:
        repo/app:a3f19c2               -> a3f19c2
        repo/app:main-a3f19c2          -> a3f19c2
        repo/app:v1.4.2-a3f19c2        -> a3f19c2
        repo/app:build-417-a3f19c2     -> a3f19c2
    Returns "" for :latest, semver-only tags and digest pins, because a wrong
    sha is worse than no sha — it would point the RCA at someone else's code.
    """
    if not image or ":" not in image:
        return ""
    tag = image.rsplit(":", 1)[-1]
    if "@" in tag or tag in ("latest", "stable", "main", "master"):
        return ""

    for candidate in reversed(tag.split("-")):
        c = candidate.lower()
        if c.startswith("v") and c[1:].replace(".", "").isdigit():
            continue                      # v1.4.2 — a version, not a sha
        if len(c) < 7 or len(c) > 40:
            continue
        if all(ch in "0123456789abcdef" for ch in c):
            return c
    return ""


def resolve_image_change(old_image: str, new_image: str, path: str = "") -> dict:
    """Turn an image change into the commits it shipped, when both tags carry
    a sha and both are present in this clone.

    `reason` explains every empty result, because "no commits found" and "the
    tags do not encode shas" lead to completely different follow-up actions.
    """
    old_sha = sha_from_image(old_image)
    new_sha = sha_from_image(new_image)

    if not new_sha:
        return {"commits": [], "old_sha": old_sha, "new_sha": new_sha,
                "reason": f"Tag on {new_image!r} does not encode a git sha."}
    if not is_repo(path):
        return {"commits": [], "old_sha": old_sha, "new_sha": new_sha,
                "reason": "No git repository available to resolve the sha against."}
    if not old_sha:
        info = commit_info(new_sha, path)
        return {"commits": [info] if info else [], "old_sha": "", "new_sha": new_sha,
                "reason": "Only the new image encodes a sha — showing that commit alone."}

    commits = commits_between(old_sha, new_sha, path)
    if not commits:
        return {"commits": [], "old_sha": old_sha, "new_sha": new_sha,
                "reason": "Both shas parsed, but neither is present in this clone "
                          "(shallow clone, or the build came from another repo)."}
    return {"commits": commits, "old_sha": old_sha, "new_sha": new_sha, "reason": ""}
