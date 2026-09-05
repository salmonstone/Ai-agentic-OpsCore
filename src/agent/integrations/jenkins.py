"""
Raw Jenkins REST API client. No AI. No business logic.

Mirrors how kubectl.py wraps kubectl and aws.py wraps boto3 — every function
here does exactly one HTTP call and returns a typed model or plain value.
Auth is HTTP Basic (JENKINS_USER, JENKINS_API_TOKEN) — never the account
password. Uses httpx (already a project dependency) rather than adding
requests/python-jenkins as new dependencies.
"""
from __future__ import annotations

import time
from urllib.parse import quote

import httpx

from agent.config import settings
from agent.core.models import BuildInfo, JenkinsInfo, JenkinsJob, JenkinsNode, QueueItem
from agent.observability.logging import get_logger

log = get_logger(__name__)


def _base_url() -> str:
    return settings.jenkins_url.rstrip("/")


def _auth() -> tuple[str, str]:
    return (settings.jenkins_user, settings.jenkins_api_token)


def _client() -> httpx.Client:
    return httpx.Client(
        auth=_auth(),
        verify=settings.jenkins_verify_ssl,
        timeout=settings.jenkins_timeout,
    )


def _job_path(job_name: str) -> str:
    """Turn 'folder/job' into '/job/folder/job/job' (Jenkins folder URL shape)."""
    parts = [quote(p, safe="") for p in job_name.split("/") if p]
    return "/job/" + "/job/".join(parts)


def _get_json(path: str, params: dict | None = None) -> dict:
    with _client() as c:
        r = c.get(f"{_base_url()}{path}", params=params)
        r.raise_for_status()
        return r.json()


def _post(path: str, data: dict | None = None) -> httpx.Response:
    with _client() as c:
        r = c.post(f"{_base_url()}{path}", data=data or {})
        r.raise_for_status()
        return r


# ---------------------------------------------------------------------------
# 1. Connection info
# ---------------------------------------------------------------------------

def get_connection_info() -> JenkinsInfo:
    if not settings.jenkins_url:
        return JenkinsInfo(connected=False, error="JENKINS_URL not configured")
    try:
        data = _get_json("/api/json")
        nodes = get_all_nodes()
        return JenkinsInfo(
            version=data.get("_class", ""),
            url=_base_url(),
            num_executors=data.get("numExecutors", 0),
            node_count=len(nodes),
            connected=True,
        )
    except httpx.HTTPStatusError as exc:
        return JenkinsInfo(url=_base_url(), connected=False,
                            error=f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:
        return JenkinsInfo(url=_base_url(), connected=False, error=str(exc))


# ---------------------------------------------------------------------------
# 2-3. Jobs
# ---------------------------------------------------------------------------

_JOB_FIELDS = (
    "jobs[name,url,color,jobs[name,url,color],"
    "lastBuild[number,result,timestamp,duration]]"
)


def get_all_jobs(folder: str | None = None) -> list[JenkinsJob]:
    """Recursively lists jobs, descending one level into folders/pipelines."""
    path = f"{_job_path(folder)}/api/json" if folder else "/api/json"
    try:
        data = _get_json(path, params={"tree": _JOB_FIELDS})
    except Exception as exc:
        log.warning("jenkins.get_all_jobs.error", folder=folder, error=str(exc))
        return []

    jobs: list[JenkinsJob] = []
    for item in data.get("jobs", []):
        is_folder = "color" not in item
        last = item.get("lastBuild") or {}
        jobs.append(JenkinsJob(
            name=item.get("name", ""),
            url=item.get("url", ""),
            color=item.get("color", ""),
            last_build_number=last.get("number"),
            last_build_status=last.get("result"),
            last_build_timestamp=last.get("timestamp"),
            last_build_duration_ms=last.get("duration"),
            is_folder=is_folder,
        ))
        # one level of nested folder jobs, already fetched via the tree query
        for sub in item.get("jobs", []):
            jobs.append(JenkinsJob(
                name=f"{item.get('name','')}/{sub.get('name','')}",
                url=sub.get("url", ""),
                color=sub.get("color", ""),
                is_folder=False,
            ))
    return jobs


def get_failed_jobs() -> list[JenkinsJob]:
    return [j for j in get_all_jobs() if not j.is_folder and j.is_failing]


def get_build_info(job_name: str, build_number: int) -> BuildInfo:
    data = _get_json(f"{_job_path(job_name)}/{build_number}/api/json")
    causes = [
        c.get("shortDescription", "")
        for action in data.get("actions", [])
        for c in action.get("causes", [])
    ] if data.get("actions") else []
    changes = [
        c.get("msg", "")
        for action in data.get("actions", [])
        for c in action.get("items", [])
        if "msg" in c
    ] if data.get("actions") else []
    parameters = {}
    for action in data.get("actions", []):
        for p in action.get("parameters", []):
            if "name" in p:
                parameters[p["name"]] = p.get("value")

    return BuildInfo(
        number=data.get("number", build_number),
        status=data.get("result") or "RUNNING",
        timestamp=data.get("timestamp", 0),
        duration_ms=data.get("duration", 0),
        causes=causes,
        changes=changes,
        parameters=parameters,
        node=data.get("builtOn", ""),
        artifacts=[a.get("fileName", "") for a in data.get("artifacts", [])],
    )


def get_console_log(job_name: str, build_number: int, tail_lines: int = 200) -> str:
    with _client() as c:
        r = c.get(f"{_base_url()}{_job_path(job_name)}/{build_number}/consoleText")
        r.raise_for_status()
        lines = r.text.splitlines()
        return "\n".join(lines[-tail_lines:])


def get_job_config(job_name: str) -> str:
    with _client() as c:
        r = c.get(f"{_base_url()}{_job_path(job_name)}/config.xml")
        r.raise_for_status()
        return r.text


def get_build_history(job_name: str, count: int = 10) -> list[BuildInfo]:
    data = _get_json(
        f"{_job_path(job_name)}/api/json",
        params={"tree": f"builds[number,result,timestamp,duration,builtOn]{{0,{count}}}"},
    )
    return [
        BuildInfo(
            number=b.get("number", 0),
            status=b.get("result") or "RUNNING",
            timestamp=b.get("timestamp", 0),
            duration_ms=b.get("duration", 0),
            node=b.get("builtOn", ""),
        )
        for b in data.get("builds", [])
    ]


# ---------------------------------------------------------------------------
# 8-9. Nodes / agents
# ---------------------------------------------------------------------------

def get_all_nodes() -> list[JenkinsNode]:
    try:
        data = _get_json("/computer/api/json", params={
            "tree": "computer[displayName,offline,idle,offlineCauseReason,"
                    "numExecutors,temporarilyOffline,assignedLabels[name]]"
        })
    except Exception as exc:
        log.warning("jenkins.get_all_nodes.error", error=str(exc))
        return []

    nodes: list[JenkinsNode] = []
    for c in data.get("computer", []):
        nodes.append(JenkinsNode(
            name=c.get("displayName", ""),
            online=not c.get("offline", False),
            idle=c.get("idle", False),
            offline_cause=c.get("offlineCauseReason") or None,
            num_executors=c.get("numExecutors", 0),
            labels=[l.get("name", "") for l in c.get("assignedLabels", [])],
            temp_offline=c.get("temporarilyOffline", False),
        ))
    return nodes


def get_offline_nodes() -> list[JenkinsNode]:
    return [n for n in get_all_nodes() if not n.online]


# ---------------------------------------------------------------------------
# 10-11. Build queue
# ---------------------------------------------------------------------------

def get_queue() -> list[QueueItem]:
    try:
        data = _get_json("/queue/api/json")
    except Exception as exc:
        log.warning("jenkins.get_queue.error", error=str(exc))
        return []

    now_ms = time.time() * 1000
    items: list[QueueItem] = []
    for item in data.get("items", []):
        in_queue_since = item.get("inQueueSince", now_ms)
        stuck_minutes = max(0, int((now_ms - in_queue_since) / 60000))
        task = item.get("task", {})
        items.append(QueueItem(
            id=item.get("id", 0),
            job_name=task.get("name", ""),
            why=item.get("why", "") or "",
            blocked=item.get("blocked", False),
            buildable=item.get("buildable", False),
            params={},
            stuck_minutes=stuck_minutes,
        ))
    return items


def get_stuck_queue_items(threshold_minutes: int = 10) -> list[QueueItem]:
    return [q for q in get_queue() if q.stuck_minutes > threshold_minutes]


# ---------------------------------------------------------------------------
# Write operations — called only after approval
# ---------------------------------------------------------------------------

def retrigger_build(job_name: str, params: dict | None = None) -> int:
    """Triggers a new build. Returns the queue item id (not the build number —
    Jenkins assigns the build number asynchronously once an executor is free)."""
    path = f"{_job_path(job_name)}/buildWithParameters" if params else f"{_job_path(job_name)}/build"
    r = _post(path, data=params)
    location = r.headers.get("Location", "")
    if "/queue/item/" in location:
        try:
            return int(location.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            pass
    return -1


def restart_agent(node_name: str) -> bool:
    try:
        _post(f"/computer/{quote(node_name, safe='')}/launchSlaveAgent")
        return True
    except Exception as exc:
        log.warning("jenkins.restart_agent.error", node=node_name, error=str(exc))
        return False


def toggle_agent_offline(node_name: str, offline: bool, message: str = "") -> bool:
    try:
        node = quote(node_name, safe="")
        nodes = {n.name: n for n in get_all_nodes()}
        current = nodes.get(node_name)
        if current and current.online == (not offline):
            return True  # already in the desired state
        _post(f"/computer/{node}/toggleOffline", data={"offlineMessage": message})
        return True
    except Exception as exc:
        log.warning("jenkins.toggle_agent_offline.error", node=node_name, error=str(exc))
        return False


def clear_workspace(job_name: str) -> bool:
    try:
        _post(f"{_job_path(job_name)}/doWipeOutWorkspace")
        return True
    except Exception as exc:
        log.warning("jenkins.clear_workspace.error", job=job_name, error=str(exc))
        return False


def cancel_build(job_name: str, build_number: int) -> bool:
    try:
        _post(f"{_job_path(job_name)}/{build_number}/stop")
        return True
    except Exception as exc:
        log.warning("jenkins.cancel_build.error", job=job_name, build=build_number, error=str(exc))
        return False


def cancel_queue_item(queue_id: int) -> bool:
    try:
        with _client() as c:
            r = c.post(f"{_base_url()}/queue/cancelItem", params={"id": queue_id})
            # Jenkins returns 404 on this endpoint even on success — anything
            # else is a real failure.
            return r.status_code in (200, 204, 302, 404)
    except Exception as exc:
        log.warning("jenkins.cancel_queue_item.error", queue_id=queue_id, error=str(exc))
        return False
