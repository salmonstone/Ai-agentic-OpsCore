"""AtlasOS Dashboard — FastAPI backend.

Thin HTTP/WebSocket wrapper around existing agent skills.
Zero business logic here — every endpoint delegates to existing
functions in src/agent/skills/ and src/agent/integrations/.
"""
from __future__ import annotations

import os

# The dashboard assistant (dashboard/chat.py) runs tools from the MCP registry
# in-process. Mutating tools are only registered when MCP_READONLY is off, and
# the decision is made once, when agent.mcp_server is first imported — so it
# has to be set before anything imports it. This affects only this local
# process; the public MCP server (ngrok, ChatGPT) is a separate process with
# its own setting. An explicit MCP_READONLY=1 in the environment still wins.
os.environ.setdefault("MCP_READONLY", "0")

import asyncio
import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.dashboard.security import COOKIE, GuardMiddleware, expected_token, token_ok

_DIST = Path(__file__).parent / "frontend" / "dist"

app = FastAPI(title="AtlasOS Dashboard API", docs_url="/api/docs", redoc_url=None)

# Replaces CORS allow_origins=["*"]: no cross-site state changes, optional
# login via DASHBOARD_TOKEN. See dashboard/security.py.
app.add_middleware(GuardMiddleware)


class LoginRequest(BaseModel):
    token: str

@app.get("/api/auth/status")
def api_auth_status(request: Request) -> JSONResponse:
    required = bool(expected_token())
    return JSONResponse({"required": required,
                         "ok": (not required) or token_ok(request.cookies.get(COOKIE, ""))})

@app.post("/api/login")
async def api_login(req: LoginRequest, request: Request) -> JSONResponse:
    if not expected_token():
        return JSONResponse({"ok": True, "required": False})
    if not token_ok(req.token.strip()):
        await asyncio.sleep(0.8)          # slow down guessing
        return JSONResponse({"ok": False, "detail": "Wrong token."}, status_code=401)
    resp = JSONResponse({"ok": True, "required": True})
    resp.set_cookie(COOKIE, req.token.strip(), httponly=True, samesite="strict",
                    secure=request.url.scheme == "https", max_age=30 * 86400, path="/")
    return resp

@app.post("/api/logout")
def api_logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp

# Serve pre-built React dist when available (production mode)
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

# ── active WebSocket connections ────────────────────────────────────────────
_live_connections: set[WebSocket] = set()
_exec_sockets: dict[str, WebSocket] = {}

# ── running executions ───────────────────────────────────────────────────────
_executions: dict[str, dict] = {}

# ── SQLite helpers ───────────────────────────────────────────────────────────
def _query(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    p = Path(db_path)
    if not p.exists():
        return []
    try:
        from agent.integrations.sqlite_conn import connect
        con = connect(p)
        rows = con.execute(sql, params).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []

def _scalar(db_path: str, sql: str, params: tuple = (), default: Any = 0) -> Any:
    rows = _query(db_path, sql, params)
    if not rows:
        return default
    v = list(rows[0].values())[0]
    return v if v is not None else default

def _daemon_running() -> bool:
    pid_file = Path("data/daemon.pid")
    if not pid_file.exists():
        return False
    try:
        import psutil
        pid = int(pid_file.read_text().strip())
        return psutil.pid_exists(pid)
    except Exception:
        return False

def _daemon_pid() -> int | None:
    try:
        p = Path("data/daemon.pid")
        return int(p.read_text().strip()) if p.exists() else None
    except Exception:
        return None

# ── broadcast to all live connections ────────────────────────────────────────
async def _broadcast(msg: dict) -> None:
    dead = set()
    for ws in list(_live_connections):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    _live_connections.difference_update(dead)

# ── cluster snapshot (no Claude, pure kubectl) ────────────────────────────────
def _cluster_snapshot() -> dict:
    from agent.integrations.kubectl import run_kubectl, is_cluster_available

    # Fast availability check — avoids 8s timeouts when cluster is offline
    if not is_cluster_available():
        cutoff_today = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cutoff_30d   = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        return {
            "pods": [], "nodes": [],
            "summary": {"total": 0, "running": 0, "crashing": 0, "pending": 0},
            "offline": True,
            "stats": {
                "incidents_open":    int(_scalar("data/incident.db", "SELECT COUNT(*) FROM incidents WHERE status='open'")),
                "pods_healed_today": int(_scalar("data/daemon.db", "SELECT COUNT(*) FROM daemon_actions WHERE timestamp >= ? AND success=1", (cutoff_today,))),
                "cost_saved_month":  float(_scalar("data/daemon.db", "SELECT COALESCE(SUM(savings),0) FROM daemon_actions WHERE timestamp >= ?", (cutoff_30d,), default=0.0)),
                "actions_30d":       int(_scalar("data/daemon.db", "SELECT COUNT(*) FROM daemon_actions WHERE timestamp >= ?", (cutoff_30d,))),
                "daemon_running":    _daemon_running(),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    pods: list[dict] = []
    nodes: list[dict] = []

    r = run_kubectl(["get", "pods", "-A", "-o", "json"])
    if r.success:
        try:
            data = json.loads(r.output or "{}")
            for pod in data.get("items", []):
                meta   = pod.get("metadata", {})
                status = pod.get("status", {})
                phase  = status.get("phase", "Unknown")
                restarts = sum(
                    cs.get("restartCount", 0)
                    for cs in status.get("containerStatuses", [])
                )
                ready_count = sum(
                    1 for cs in status.get("containerStatuses", [])
                    if cs.get("ready", False)
                )
                total_count = len(status.get("containerStatuses", []))
                pods.append({
                    "name":      meta.get("name", ""),
                    "namespace": meta.get("namespace", ""),
                    "phase":     phase,
                    "ready":     f"{ready_count}/{total_count}",
                    "restarts":  restarts,
                    "node":      status.get("hostIP", ""),
                })
        except Exception:
            pass

    r2 = run_kubectl(["get", "nodes", "-o", "json"])
    if r2.success:
        try:
            data = json.loads(r2.output or "{}")
            for node in data.get("items", []):
                meta    = node.get("metadata", {})
                conds   = node.get("status", {}).get("conditions", [])
                ready   = next(
                    (c["status"] == "True" for c in conds if c.get("type") == "Ready"),
                    False,
                )
                nodes.append({
                    "name":  meta.get("name", ""),
                    "ready": ready,
                    "roles": ",".join(
                        k.replace("node-role.kubernetes.io/", "")
                        for k in meta.get("labels", {})
                        if k.startswith("node-role.kubernetes.io/")
                    ) or "worker",
                })
        except Exception:
            pass

    # Summary stats
    running  = sum(1 for p in pods if p["phase"] == "Running")
    crashing = sum(1 for p in pods if p["phase"] in ("Failed", "CrashLoopBackOff") or p["restarts"] > 3)
    pending  = sum(1 for p in pods if p["phase"] == "Pending")

    cutoff_today = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    cutoff_30d   = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    incidents_open  = int(_scalar("data/incident.db", "SELECT COUNT(*) FROM incidents WHERE status='open'"))
    pods_healed     = int(_scalar("data/daemon.db",
        "SELECT COUNT(*) FROM daemon_actions WHERE timestamp >= ? AND success=1", (cutoff_today,)))
    cost_saved      = float(_scalar("data/daemon.db",
        "SELECT COALESCE(SUM(savings),0) FROM daemon_actions WHERE timestamp >= ?",
        (cutoff_30d,), default=0.0))
    actions_30d     = int(_scalar("data/daemon.db",
        "SELECT COUNT(*) FROM daemon_actions WHERE timestamp >= ?", (cutoff_30d,)))

    return {
        "pods":      pods,
        "nodes":     nodes,
        "summary": {
            "total":    len(pods),
            "running":  running,
            "crashing": crashing,
            "pending":  pending,
        },
        "stats": {
            "incidents_open":    incidents_open,
            "pods_healed_today": pods_healed,
            "cost_saved_month":  round(cost_saved, 2),
            "actions_30d":       actions_30d,
            "daemon_running":    _daemon_running(),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# ── background task: push cluster state every 5s ─────────────────────────────
async def _live_pusher() -> None:
    while True:
        if _live_connections:
            try:
                snapshot = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, _cluster_snapshot),
                    timeout=15,
                )
                await _broadcast({"type": "cluster_update", **snapshot})
            except Exception:
                pass
        await asyncio.sleep(5)

@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_live_pusher())
    # Pre-warm command map cache in background so first /api/execute is instant
    asyncio.get_event_loop().run_in_executor(None, _get_cmd_map)

# ── WebSocket: live cluster feed ─────────────────────────────────────────────
@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    await ws.accept()
    _live_connections.add(ws)
    try:
        # send immediately on connect
        snapshot = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _cluster_snapshot),
            timeout=15,
        )
        await ws.send_json({"type": "cluster_update", **snapshot})
        while True:
            await asyncio.sleep(30)   # keep-alive; pusher handles updates
    except WebSocketDisconnect:
        pass
    finally:
        _live_connections.discard(ws)

# ── WebSocket: execution output stream ───────────────────────────────────────
@app.websocket("/ws/execution/{execution_id}")
async def ws_execution(ws: WebSocket, execution_id: str) -> None:
    await ws.accept()
    _exec_sockets[execution_id] = ws

    # Drain any messages that were buffered before WS connected (race fix)
    buffered = _exec_buffers.pop(execution_id, [])
    for msg in buffered:
        try:
            await ws.send_json(msg)
        except Exception:
            break

    try:
        while True:
            await ws.receive_text()   # keep connection alive; we only push
    except WebSocketDisconnect:
        pass
    finally:
        _exec_sockets.pop(execution_id, None)

# ── Command map cache — built once in a thread so it never blocks the event loop
_cmd_map_cache: dict | None = None

def _get_cmd_map() -> dict:
    global _cmd_map_cache
    if _cmd_map_cache is None:
        from agent.dashboard.discovery import get_command_groups
        m: dict = {}
        for cmds in get_command_groups().values():
            for c in cmds:
                m[c["full_command"]] = c
        _cmd_map_cache = m
    return _cmd_map_cache

# ── REST: command discovery ───────────────────────────────────────────────────
@app.get("/api/commands")
def api_commands() -> JSONResponse:
    from agent.dashboard.discovery import get_command_groups
    return JSONResponse(get_command_groups())

# ── REST: overview stats ──────────────────────────────────────────────────────
@app.get("/api/overview")
def api_overview() -> JSONResponse:
    snap = _cluster_snapshot()
    return JSONResponse(snap["stats"] | {"summary": snap["summary"]})

# ── REST: execute command ─────────────────────────────────────────────────────
# Message buffer per exec_id — solves the race where the command finishes
# before the WebSocket connection is established.
_exec_buffers: dict[str, list[dict]] = {}

class ExecuteRequest(BaseModel):
    full_command: str          # matches frontend field name
    params: dict = {}
    confirmed: bool = False

@app.post("/api/execute")
async def api_execute(req: ExecuteRequest) -> JSONResponse:
    # Use cached command map (built in executor on first call, never blocks event loop)
    all_cmds = await asyncio.get_event_loop().run_in_executor(None, _get_cmd_map)

    cmd_info = all_cmds.get(req.full_command)
    if not cmd_info:
        raise HTTPException(404, f"Command '{req.full_command}' not found")
    if cmd_info.get("is_destructive") and not req.confirmed:
        raise HTTPException(400, "Destructive command requires confirmed=true")

    exec_id = uuid.uuid4().hex
    _executions[exec_id]  = {"status": "running", "command": req.full_command}
    _exec_buffers[exec_id] = []   # pre-create buffer before task starts

    asyncio.create_task(_run_command(exec_id, req.full_command, req.params))
    return JSONResponse({"exec_id": exec_id})   # frontend expects exec_id


async def _send_exec(exec_id: str, msg: dict) -> None:
    """Send to WebSocket if connected, otherwise buffer for later drain."""
    ws = _exec_sockets.get(exec_id)
    if ws:
        try:
            await ws.send_json(msg)
            return
        except Exception:
            pass
    # buffer if WS not yet connected
    buf = _exec_buffers.get(exec_id)
    if buf is not None:
        buf.append(msg)


async def _run_command(exec_id: str, full_command: str, params: dict) -> None:
    await _send_exec(exec_id, {"type": "line", "data": f"▶ {full_command}"})
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _execute_skill, full_command, params
        )
        # format result as human-readable lines
        lines = _result_to_lines(result)
        for line in lines:
            await _send_exec(exec_id, {"type": "line", "data": line})
        await _send_exec(exec_id, {"type": "done", "exit_code": 0})
    except Exception as exc:
        await _send_exec(exec_id, {"type": "line", "data": f"ERROR: {exc}"})
        await _send_exec(exec_id, {"type": "done", "exit_code": 1})
    finally:
        _executions.pop(exec_id, None)
        # keep buffer a bit longer so late WS connections can drain it
        async def _cleanup():
            await asyncio.sleep(30)
            _exec_buffers.pop(exec_id, None)
        asyncio.create_task(_cleanup())


def _result_to_lines(result: Any) -> list[str]:
    """Convert a skill result dict into display lines."""
    if result is None:
        return ["(no output)"]
    if isinstance(result, str):
        return result.splitlines() or ["(done)"]

    lines = []
    # Common patterns: output key, items list, dict of lists
    if "output" in result:
        raw = result["output"]
        if isinstance(raw, str):
            lines = raw.splitlines()
        elif isinstance(raw, list):
            lines = [str(x) for x in raw]
        else:
            lines = [str(raw)]
    else:
        # Pretty-print each key
        for k, v in result.items():
            if isinstance(v, list):
                lines.append(f"── {k} ({len(v)}) ──")
                for item in v[:50]:
                    if isinstance(item, dict):
                        lines.append("  " + "  ".join(f"{ik}={iv}" for ik, iv in item.items()))
                    else:
                        lines.append(f"  {item}")
            elif isinstance(v, dict):
                lines.append(f"── {k} ──")
                for ik, iv in v.items():
                    lines.append(f"  {ik}: {iv}")
            else:
                lines.append(f"{k}: {v}")

    return lines if lines else ["(done)"]


def _execute_skill(full_command: str, params: dict) -> Any:
    """Dispatch to real skill functions. Falls back to CLI subprocess."""
    parts = full_command.strip().split()
    group = parts[0] if len(parts) > 1 else ""
    name  = parts[1] if len(parts) > 1 else parts[0]

    # Fast-fail kubectl commands when cluster is offline — avoids 8–30s hangs
    _KUBECTL_GROUPS = {"k8s", "resources", "security", "tls"}
    if group in _KUBECTL_GROUPS:
        from agent.integrations.kubectl import is_cluster_available
        if not is_cluster_available():
            return {
                "output": (
                    "Cluster offline — kubectl cannot reach the Kubernetes API server.\n"
                    "\nTo start a local cluster:\n"
                    "  minikube start --driver=docker\n"
                    "  minikube start --driver=virtualbox\n"
                    "\nOr check your kubeconfig:\n"
                    "  kubectl config get-contexts"
                )
            }

    # ── k8s ──────────────────────────────────────────────────────────────────
    # (resources scan / cost scan used to be dispatched here to functions that
    # don't exist — scan_resources / full_aws_analysis — so those buttons
    # always errored. They now fall through to the real CLI command below.)
    if group == "k8s":
        if name == "scan":
            from agent.skills.k8s import K8sSkill
            issues = K8sSkill().full_cluster_scan(params.get("namespace", "all"))
            return {"issues": issues} if issues else {"output": "✓ scan complete · no problems found"}
        if name == "pods":
            from agent.integrations.kubectl import run_kubectl
            r = run_kubectl(["get", "pods", "-A"])
            return {"output": r.output or r.error or ""}

    # ── db ────────────────────────────────────────────────────────────────────
    if group == "db":
        if name == "scan":
            from agent.skills.db_health import scan_all_databases
            return {"databases": scan_all_databases()}

    # ── scale ────────────────────────────────────────────────────────────────
    if group == "scale":
        if name == "list":
            from agent.integrations import autoscale_db
            return {"policies": autoscale_db.list_policies()}
        if name == "run":
            from agent.skills.autoscale import run_scheduled_scaling
            return {"results": run_scheduled_scaling()}

    # ── runbook ──────────────────────────────────────────────────────────────
    if group == "runbook":
        if name == "list":
            from agent.skills.runbook import list_runbooks
            return {"runbooks": list_runbooks()}
        if name in ("run", "history"):
            from agent.integrations import runbook_db
            return {"runs": runbook_db.list_runs()}

    # ── incident ─────────────────────────────────────────────────────────────
    if group == "incident":
        from agent.integrations import incident_db
        return {"incidents": incident_db.list_incidents()}

    # ── slo ──────────────────────────────────────────────────────────────────
    if group == "slo":
        if name == "list":
            from agent.integrations import slo_db
            return {"slos": slo_db.list_slos()}

    # ── daemon ────────────────────────────────────────────────────────────────
    if group == "daemon":
        if name == "status":
            return _daemon_status_dict()

    # ── fallback: run via the agent CLI as subprocess ─────────────────────────
    import sys as _sys
    cmd_parts = [_sys.executable, "-m", "agent"] + full_command.split()
    for k, v in params.items():
        if isinstance(v, bool):
            if v:
                cmd_parts.append(f"--{k.replace('_','-')}")
        else:
            cmd_parts += [f"--{k.replace('_','-')}", str(v)]
    r = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=120)
    output = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()
    return {"output": output or "(command completed with no output)"}

# ── daemon control ────────────────────────────────────────────────────────────
def _daemon_status_dict() -> dict:
    running = _daemon_running()
    pid = _daemon_pid()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    alerts = int(_scalar("data/daemon.db",
        "SELECT COUNT(*) FROM daemon_actions WHERE timestamp >= ?", (cutoff,)))
    last = _query("data/daemon.db",
        "SELECT timestamp FROM daemon_actions ORDER BY timestamp DESC LIMIT 1")
    return {
        "running":          running,
        "pid":              pid,
        "alerts_sent_today": alerts,
        "last_check_time":  last[0]["timestamp"] if last else None,
    }

@app.get("/api/daemon/status")
def api_daemon_status() -> JSONResponse:
    return JSONResponse(_daemon_status_dict())

@app.post("/api/daemon/start")
def api_daemon_start() -> JSONResponse:
    if _daemon_running():
        return JSONResponse({"status": "already_running", "pid": _daemon_pid()})
    flags = (0x00000008 | 0x00000200) if sys.platform == "win32" else 0
    # A detached process with no stdout/stderr dies at startup on Python 3.14,
    # so give the daemon a log file (same fix as `agent dashboard start`).
    log_path = Path("data/logs/daemon-dashboard.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent.core.daemon"],
            stdin=subprocess.DEVNULL, stdout=log_file, stderr=log_file,
            creationflags=flags,
        )
    import time as _time
    _time.sleep(2)
    if proc.poll() is not None:
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:])
        return JSONResponse({"status": "error", "error": f"daemon exited during startup (code {proc.returncode}): {tail}"})
    Path("data/daemon.pid").write_text(str(proc.pid))
    audit.record(f"Dashboard: healing daemon started (pid {proc.pid})")
    return JSONResponse({"status": "started", "pid": proc.pid})

@app.post("/api/daemon/stop")
def api_daemon_stop() -> JSONResponse:
    pid = _daemon_pid()
    if not pid:
        return JSONResponse({"status": "not_running"})
    try:
        import psutil
        psutil.Process(pid).terminate()
        Path("data/daemon.pid").unlink(missing_ok=True)
        audit.record(f"Dashboard: healing daemon stopped (pid {pid}) — auto-healing paused")
        return JSONResponse({"status": "stopped", "pid": pid})
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)})

# ── incidents ─────────────────────────────────────────────────────────────────
@app.get("/api/incidents")
def api_incidents() -> JSONResponse:
    rows = _query("data/incident.db",
        "SELECT id, title, severity, status, service, namespace, opened_at, resolved_at "
        "FROM incidents ORDER BY opened_at DESC LIMIT 20")
    return JSONResponse(rows)

# ── recent actions ────────────────────────────────────────────────────────────
@app.get("/api/actions")
def api_actions() -> JSONResponse:
    rows = _query("data/daemon.db",
        "SELECT timestamp, category, action, resource, namespace, success, note "
        "FROM daemon_actions ORDER BY timestamp DESC LIMIT 30")
    return JSONResponse(rows)

# ── SLOs ──────────────────────────────────────────────────────────────────────
@app.get("/api/slos")
def api_slos() -> JSONResponse:
    """Active SLOs with live error-budget status.

    Delegates to slo_db.get_budget_status (the same math the CLI uses). The
    `slos` table stores no precomputed downtime — burn is summed from
    `slo_burns` — so we must not SELECT a `used_downtime_min` column.
    """
    from agent.integrations import slo_db
    if not Path(slo_db._DB_PATH).exists():
        return JSONResponse([])
    try:
        result = []
        for s in slo_db.list_slos(active_only=True):
            b = slo_db.get_budget_status(s["id"])
            result.append({
                "id":               s["id"],
                "name":             s.get("name", ""),
                "service":          s.get("service", ""),
                "namespace":        s.get("namespace", ""),
                "target_pct":       s.get("target_pct", 99.9),
                "window_days":      s.get("window_days", 30),
                "budget_minutes":   round(b["allowed_downtime_min"], 1),
                "budget_remaining": round(b["remaining_min"], 1),
                "used_minutes":     round(b["used_downtime_min"], 1),
                "budget_pct":       round(b["budget_pct_remaining"], 1),
                "status":           b["status"],
            })
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

# ── Jenkins ────────────────────────────────────────────────────────────────────
@app.get("/api/jenkins")
def api_jenkins() -> JSONResponse:
    """
    Lightweight, non-AI Jenkins snapshot for the dashboard panel — jobs/agents/
    queue only (no Claude call), so this stays cheap on every poll. Deep AI
    diagnosis and fixes go through the CLI (`agent jenkins diagnose` / `heal`)
    rather than an unauthenticated dashboard button.
    """
    from agent.config import settings
    if not settings.jenkins_url:
        return JSONResponse({"configured": False})
    try:
        from agent.integrations import jenkins as jk
        info = jk.get_connection_info()
        if not info.connected:
            return JSONResponse({"configured": True, "connected": False, "error": info.error})

        jobs    = jk.get_all_jobs()
        failing = [j for j in jobs if not j.is_folder and j.is_failing]
        offline = jk.get_offline_nodes()
        stuck   = jk.get_stuck_queue_items()
        total   = sum(1 for j in jobs if not j.is_folder)
        score   = max(0, 100 - 10 * len(failing) - 15 * len(offline) - 5 * len(stuck))

        return JSONResponse({
            "configured":    True,
            "connected":     True,
            "health_score":  score,
            "total_jobs":    total,
            "failing_jobs":  [{"name": j.name, "color": j.color} for j in failing],
            "offline_nodes": [n.name for n in offline],
            "stuck_queue":   len(stuck),
        })
    except Exception as exc:
        return JSONResponse({"configured": True, "connected": False, "error": str(exc)}, status_code=500)

# ── pending deploys ───────────────────────────────────────────────────────────
@app.get("/api/pending-deploys")
def api_pending_deploys() -> JSONResponse:
    # NB: the column is `deployment`, not `service`; aliased here so the UI
    # can use a stable field name. `reason` is synthesized from risk + commit.
    rows = _query("data/deploy.db",
        "SELECT id, repo, branch, deployment AS service, namespace, "
        "old_image, new_image, commit_sha, commit_message, author, "
        "risk_score, risk_label, status, created_at AS requested_at "
        "FROM pending_deploys WHERE status='pending' "
        "ORDER BY created_at DESC LIMIT 20")
    for r in rows:
        bits = []
        if r.get("risk_label"):
            bits.append(f"Risk: {r['risk_label']} ({r.get('risk_score', 0)}/100)")
        if r.get("commit_message"):
            bits.append(r["commit_message"].strip().splitlines()[0])
        if r.get("author"):
            bits.append(f"by {r['author']}")
        r["reason"] = " · ".join(bits)
    return JSONResponse(rows)

@app.post("/api/deploys/{deploy_id}/approve")
async def api_deploy_approve(deploy_id: str) -> JSONResponse:
    def _run():
        from agent.skills.deployment import DeploymentSkill
        logs: list[str] = []
        report = DeploymentSkill().execute_approve(
            deploy_id, on_status=lambda m: logs.append(m)
        )
        rep_dict = None
        if report is not None:
            if hasattr(report, "model_dump"):
                rep_dict = report.model_dump(mode="json")
            elif hasattr(report, "to_dict"):
                rep_dict = report.to_dict()
        return {
            "status": "approved" if report is not None else "gated",
            "log": logs,
            "report": rep_dict,
        }
    try:
        # Approve runs a full deploy+watch flow — offload so it never blocks the loop
        result = await asyncio.get_event_loop().run_in_executor(None, _run)
        return JSONResponse(result)
    except Exception as exc:
        raise HTTPException(500, str(exc))

@app.post("/api/deploys/{deploy_id}/reject")
async def api_deploy_reject(deploy_id: str) -> JSONResponse:
    def _run():
        from agent.skills.deployment import DeploymentSkill
        logs: list[str] = []
        ok = DeploymentSkill().execute_reject(
            deploy_id, reason="Rejected from dashboard",
            on_status=lambda m: logs.append(m),
        )
        return {"status": "rejected" if ok else "not_found", "log": logs}
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _run)
        return JSONResponse(result)
    except Exception as exc:
        raise HTTPException(500, str(exc))

# ── 30-day chart ──────────────────────────────────────────────────────────────
@app.get("/api/chart")
def api_chart() -> JSONResponse:
    rows = _query("data/daemon.db",
        "SELECT substr(timestamp,1,10) as day, COUNT(*) as count "
        "FROM daemon_actions "
        "WHERE timestamp >= datetime('now','-30 days') "
        "GROUP BY day ORDER BY day ASC")
    today = datetime.now(timezone.utc).date()
    day_map = {r["day"]: r["count"] for r in rows}
    labels, data = [], []
    for i in range(30):
        d = str(today - timedelta(days=29 - i))
        labels.append(d[5:])
        data.append(day_map.get(d, 0))
    return JSONResponse({"labels": labels, "data": data})

# ── runbooks ──────────────────────────────────────────────────────────────────
@app.get("/api/runbooks")
def api_runbooks() -> JSONResponse:
    rows = _query("data/runbook.db",
        "SELECT id, runbook_id, trigger, status, steps_done, steps_total, started_at "
        "FROM runbook_runs ORDER BY started_at DESC LIMIT 15")
    return JSONResponse(rows)


# ── about: living project documentation ───────────────────────────────────────
@app.get("/api/about")
def api_about() -> JSONResponse:
    """Live project scan — structure, runtime state, and skills.

    Delegates entirely to AboutSkill (same source the CLI `agent about` uses).
    Claude analysis is intentionally omitted to keep this endpoint instant.
    """
    try:
        from agent.skills.about import AboutSkill
        skill = AboutSkill()
        structure = skill.scan_project_structure()
        state = skill.scan_runtime_state()
        skills = [
            {
                "label": s.label, "name": s.name, "description": s.description,
                "eval_suite": s.eval_suite, "eval_cases": s.eval_cases,
                "integrations_used": s.integrations_used, "methods": s.methods,
            }
            for s in skill.get_skills_summary(structure)
        ]
        return JSONResponse({
            "version": structure.version,
            "structure": structure.to_dict(),
            "runtime": state.to_dict(),
            "skills": skills,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── clusters: multi-cluster management ────────────────────────────────────────
@app.get("/api/clusters")
def api_clusters() -> JSONResponse:
    """All kubeconfig contexts.

    Health/node-count is only probed when the active cluster is reachable, so
    the panel stays instant when everything is offline. Talks to the same
    kubectl helpers the `agent multi-cluster` CLI uses — no side effects.
    """
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from agent.integrations.kubectl import (
            get_all_contexts, get_context_node_count,
            get_current_context, is_cluster_available,
        )
        contexts = get_all_contexts()
        checked  = is_cluster_available()
        if checked and contexts:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(get_context_node_count, c.name): c for c in contexts}
                for fut in as_completed(futures):
                    c = futures[fut]
                    try:
                        n = fut.result(timeout=12)
                        if n >= 0:
                            c.health, c.node_count = "healthy", n
                        else:
                            c.health = "unreachable"
                    except Exception:
                        c.health = "unreachable"
        return JSONResponse({
            "current":        get_current_context(),
            "clusters":       [c.model_dump() for c in contexts],
            "health_checked": checked,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc), "clusters": []}, status_code=500)


class SwitchClusterRequest(BaseModel):
    name: str

@app.post("/api/clusters/switch")
def api_cluster_switch(req: SwitchClusterRequest) -> JSONResponse:
    from agent.integrations.kubectl import switch_context, get_current_context
    if not switch_context(req.name):
        return JSONResponse(
            {"status": "error", "error": f"Could not switch to context '{req.name}'"},
            status_code=400,
        )
    # Invalidate the availability cache so the live panel re-probes the new cluster now
    try:
        from agent.integrations import kubectl as _k
        _k._avail_cache.update({"ok": None, "ts": 0.0})
    except Exception:
        pass
    audit.record(f"Dashboard: switched kube context to {req.name}")
    return JSONResponse({"status": "switched", "current": get_current_context()})


class AddEKSRequest(BaseModel):
    cluster_name: str
    region: str
    profile: str = "default"
    rename_to: str = ""

@app.post("/api/clusters/add-eks")
async def api_cluster_add_eks(req: AddEKSRequest) -> JSONResponse:
    """Register an EKS cluster via `aws eks update-kubeconfig` (offloaded — it shells out)."""
    def _run():
        from agent.skills.multi_cluster import MultiClusterSkill
        return MultiClusterSkill().add_eks(
            req.cluster_name.strip(), req.region.strip(),
            req.profile.strip() or "default", req.rename_to.strip(),
        )
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _run)
        # Force the live panel to re-probe once the new context is active
        try:
            from agent.integrations import kubectl as _k
            _k._avail_cache.update({"ok": None, "ts": 0.0})
        except Exception:
            pass
        return JSONResponse(result, status_code=200 if result.get("success") else 400)
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── status summary (Overview tiles) ───────────────────────────────────────────
# The same seven sections as the daily Slack summary (skills/daily_summary.py),
# each ok / warn / error — where error means "couldn't check", never healthy.
# Cached: the AWS section is a Cost Explorer call ($0.01) and the Jenkins and
# GitHub sections are several API calls, so polling must not rebuild it.
_SUMMARY_TTL_S = 300
_summary_cache: dict = {"at": 0.0, "data": None}

@app.get("/api/summary")
async def api_summary(force: bool = False) -> JSONResponse:
    import time as _time
    if force or _summary_cache["data"] is None or _time.time() - _summary_cache["at"] > _SUMMARY_TTL_S:
        def _build():
            from agent.skills import daily_summary as ds
            sections = ds.build_summary()
            return {
                "headline": ds.headline(sections),
                "sections": [{"title": s.title, "status": s.status, "lines": s.lines} for s in sections],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        _summary_cache["data"] = await asyncio.get_event_loop().run_in_executor(None, _build)
        _summary_cache["at"] = _time.time()
    return JSONResponse(_summary_cache["data"])


# ── AWS spend chart ───────────────────────────────────────────────────────────
# Daily spend + the statistical spike days — the same numbers the daemon's
# spend-anomaly alert uses. Cost Explorer is $0.01 a call, so cache an hour.
_SPEND_TTL_S = 3600
_spend_cache: dict = {"at": 0.0, "data": None}

@app.get("/api/spend")
async def api_spend(force: bool = False) -> JSONResponse:
    import time as _time
    if force or _spend_cache["data"] is None or _time.time() - _spend_cache["at"] > _SPEND_TTL_S:
        def _build():
            from agent.integrations.aws_cost import get_spend_anomalies
            d = get_spend_anomalies(days=30)
            return {"daily": d.get("daily_totals") or [], "mean": d.get("mean_daily") or 0.0,
                    "anomalies": [a.get("date") for a in d.get("anomaly_days") or []],
                    "generated_at": datetime.now(timezone.utc).isoformat()}
        try:
            _spend_cache["data"] = await asyncio.get_event_loop().run_in_executor(None, _build)
            _spend_cache["at"] = _time.time()
        except Exception as exc:
            return JSONResponse({"daily": [], "error": str(exc)}, status_code=200)
    return JSONResponse(_spend_cache["data"])


# ── cluster panel ─────────────────────────────────────────────────────────────
@app.get("/api/cluster")
def api_cluster() -> JSONResponse:
    """Nodes (with CPU/memory from metrics-server when installed), problem pods,
    and pods the daemon healed in the last 24h. Unreachable is reported as
    unreachable — empty lists here never stand in for "no problems"."""
    from agent.integrations.kubectl import (
        check_cluster_auth, get_current_context, get_node_metrics_top,
        get_nodes_detail, get_problematic_pods, is_cluster_available,
    )
    ctx = get_current_context()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    healed = _query("data/daemon.db",
        "SELECT timestamp, category, action, resource, namespace, note FROM daemon_actions "
        "WHERE timestamp >= ? AND success = 1 ORDER BY timestamp DESC LIMIT 20", (cutoff,))
    base = {"context": ctx, "healed": healed, "checked_at": datetime.now(timezone.utc).isoformat()}

    if not is_cluster_available():
        return JSONResponse(base | {"reachable": False, "nodes": [], "problem_pods": [],
                                    "error": "Kubernetes API server not reachable from this machine."})
    nodes = get_nodes_detail()
    if not nodes:
        ok, msg = check_cluster_auth()
        return JSONResponse(base | {"reachable": False, "nodes": [], "problem_pods": [],
                                    "error": msg if not ok else "kubectl returned no nodes."})
    metrics = {m.name: m for m in get_node_metrics_top()}
    node_rows = []
    for n in nodes:
        m = metrics.get(n.name)
        node_rows.append({
            "name": n.name, "status": n.status, "instance_type": n.instance_type, "age": n.age,
            "cpu_percent": m.cpu_percent if m else None,
            "memory_percent": m.memory_percent if m else None,
        })
    pods = [p.model_dump() for p in get_problematic_pods()]
    return JSONResponse(base | {"reachable": True, "nodes": node_rows, "problem_pods": pods,
                                "metrics_available": bool(metrics), "error": None})


# ── Jenkins panel: builds ─────────────────────────────────────────────────────
@app.get("/api/jenkins/builds")
def api_jenkins_builds() -> JSONResponse:
    """Every job's last build + last 10 results, and failed builds from the last
    7 days. Plain Jenkins API calls — no AI. Root causes come from the
    assistant's jenkins_diagnose tool on demand, never guessed here."""
    from agent.config import settings
    if not settings.jenkins_url:
        return JSONResponse({"configured": False, "connected": False, "jobs": [], "failures": []})
    try:
        from concurrent.futures import ThreadPoolExecutor
        from agent.integrations import jenkins as jk
        info = jk.get_connection_info()
        if not info.connected:
            return JSONResponse({"configured": True, "connected": False, "error": info.error,
                                 "jobs": [], "failures": []})
        jobs = [j for j in jk.get_all_jobs() if not j.is_folder][:25]

        def _hist(job):
            try:
                return job, jk.get_build_history(job.name, count=10)
            except Exception:
                return job, []
        with ThreadPoolExecutor(max_workers=6) as pool:
            histories = list(pool.map(_hist, jobs))

        week_ago_ms = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000
        rows, failures = [], []
        for job, builds in histories:
            last = builds[0] if builds else None
            rows.append({
                "name": job.name,
                "last_number": job.last_build_number,
                "status": (last.status if last else job.last_build_status) or "NOT_BUILT",
                "duration_ms": last.duration_ms if last else job.last_build_duration_ms,
                "timestamp": last.timestamp if last else job.last_build_timestamp,
                "trend": [b.status for b in reversed(builds)],
            })
            for b in builds:
                if b.status == "FAILURE" and b.timestamp >= week_ago_ms:
                    failures.append({"job": job.name, "number": b.number, "timestamp": b.timestamp,
                                     "duration_ms": b.duration_ms, "node": b.node})
        failures.sort(key=lambda f: f["timestamp"], reverse=True)
        return JSONResponse({"configured": True, "connected": True, "url": settings.jenkins_url,
                             "jobs": rows, "failures": failures})
    except Exception as exc:
        return JSONResponse({"configured": True, "connected": False, "error": str(exc),
                             "jobs": [], "failures": []})


# ── approvals (fix proposals from the daemon, Jenkins hook, chat, CLI) ───────
@app.get("/api/approvals")
def api_approvals() -> JSONResponse:
    from agent.core import approvals
    approvals.expire_stale()
    def _row(a):
        return {"id": a.id, "kind": a.kind, "summary": a.summary, "params": a.params,
                "status": a.status, "created_at": a.created_at, "expires_at": a.expires_at,
                "decided_at": a.decided_at, "decided_by": a.decided_by, "result": a.result}
    items = approvals.list_actions(status=None, limit=50)
    return JSONResponse({
        "pending": [_row(a) for a in items if a.status == "pending"],
        "history": [_row(a) for a in items if a.status != "pending"][:30],
    })


class ApprovalDecision(BaseModel):
    approve: bool

@app.post("/api/approvals/{action_id}/decide")
async def api_approval_decide(action_id: str, req: ApprovalDecision) -> JSONResponse:
    """Approve (and run) or reject — the same fix_registry path the Slack button uses."""
    from agent.core import fix_registry
    out = await fix_registry.decide(action_id, req.approve, user="dashboard")
    return JSONResponse(out, status_code=404 if out["status"] == "not_found" else 200)


# ── assistant (chat bubble) ───────────────────────────────────────────────────
def _sse(events) -> StreamingResponse:
    async def body():
        try:
            async for ev in events:
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        except Exception as exc:   # never leave the UI hanging on a half-open stream
            yield f"data: {json.dumps({'type': 'error', 'message': 'The assistant hit an error.', 'detail': str(exc)[:300]})}\n\n"
    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ChatMessage(BaseModel):
    session_id: str
    message: str

class ChatDecision(BaseModel):
    session_id: str
    tool_use_id: str
    approve: bool

@app.get("/api/chat/info")
async def api_chat_info() -> JSONResponse:
    from agent.dashboard import chat
    try:
        return JSONResponse(await chat.tools_info())
    except Exception as exc:
        return JSONResponse({"mutations_enabled": False, "tools": [], "error": str(exc)}, status_code=500)

@app.post("/api/chat")
async def api_chat(req: ChatMessage) -> StreamingResponse:
    from agent.dashboard import chat
    text = req.message.strip()
    if not text:
        raise HTTPException(400, "Empty message")
    return _sse(chat.send(req.session_id, text[:4000]))

@app.post("/api/chat/decide")
async def api_chat_decide(req: ChatDecision) -> StreamingResponse:
    from agent.dashboard import chat
    return _sse(chat.decide(req.session_id, req.tool_use_id, req.approve))

@app.delete("/api/chat/{session_id}")
async def api_chat_clear(session_id: str) -> JSONResponse:
    from agent.dashboard import chat
    chat.clear(session_id)
    return JSONResponse({"cleared": True})


# ── settings & integrations ───────────────────────────────────────────────────
# Same storage as the CLI (core/settings_store.py → keychain / .env). Reading
# never returns a secret's value. Writing needs the dashboard to be protected
# (DASHBOARD_TOKEN set) — otherwise anyone who reached this page could swap
# your Slack webhook or AWS keys; the guard then requires the login too.

class SettingsSave(BaseModel):
    values: dict[str, str] = {}
    clear: list[str] = []

@app.get("/api/settings")
def api_settings() -> JSONResponse:
    from agent.core import settings_store
    return JSONResponse(settings_store.describe() | {"protected": bool(expected_token())})

@app.post("/api/settings")
def api_settings_save(req: SettingsSave) -> JSONResponse:
    from agent.core import settings_store
    if not expected_token():
        return JSONResponse({"detail": "Protect the dashboard first (Settings → Protect this dashboard) — "
                                       "saving credentials from a web page needs a login."}, status_code=403)
    try:
        notes = settings_store.save(req.values, req.clear)
    except settings_store.SettingsError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    _summary_cache["data"] = None          # tiles should reflect the new connections
    audit.record("Dashboard: settings changed — " + ", ".join(
        [f"set {k}" for k in req.values] + [f"removed {k}" for k in req.clear]))
    return JSONResponse({"saved": True, "notes": notes,
                         "restart_note": "Applied to the dashboard now. The daemon and MCP server "
                                         "pick it up the next time they restart."})

@app.post("/api/settings/check/{integration_id}")
async def api_settings_check(integration_id: str) -> JSONResponse:
    from agent.core import settings_store
    try:
        result = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, settings_store.check, integration_id), timeout=25)
    except asyncio.TimeoutError:
        result = {"status": "error", "detail": "No answer within 25s."}
    except settings_store.SettingsError as exc:
        raise HTTPException(404, str(exc))
    return JSONResponse(result)

@app.post("/api/settings/slack-test")
def api_settings_slack_test() -> JSONResponse:
    from agent.integrations.slack import is_configured, send_alert_generic
    if not is_configured():
        return JSONResponse({"sent": False, "detail": "No Slack webhook set."}, status_code=400)
    ok = send_alert_generic(title="AtlasOS test message",
                            message="Sent from the dashboard's Settings page — Slack is connected.",
                            severity="info")
    return JSONResponse({"sent": bool(ok), "detail": "" if ok else "Slack rejected the message."})

@app.post("/api/settings/protect")
def api_settings_protect(request: Request) -> JSONResponse:
    """First-run: create DASHBOARD_TOKEN in the keychain, log this browser in,
    and return the token once so the user can save it. Refused if a token
    already exists (changing it needs the old login — use the CLI)."""
    if expected_token():
        return JSONResponse({"detail": "This dashboard is already protected."}, status_code=409)
    from agent.core import secrets as sec
    token = sec.generate_token()
    try:
        sec.store("DASHBOARD_TOKEN", token)
    except sec.SecretsError as exc:
        return JSONResponse({"detail": f"Couldn't store the token: {exc}"}, status_code=500)
    os.environ["DASHBOARD_TOKEN"] = token
    from agent.dashboard import security
    security._kc_cache["at"] = 0.0          # re-read the keychain on the next request
    audit.record("Dashboard: login turned on (DASHBOARD_TOKEN created)")
    resp = JSONResponse({"protected": True, "token": token})
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                    secure=request.url.scheme == "https", max_age=30 * 86400, path="/")
    return resp


# ── logs: pods, Jenkins builds, AtlasOS itself ────────────────────────────────
_SYSTEM_LOGS = {
    "daemon": ("Daemon", Path("data/daemon.log")),
    "dashboard": ("Dashboard server", Path("data/logs/dashboard.log")),
    "mcp": ("MCP server", Path("data/logs/mcp.log")),
    "supervisor": ("Supervisor", Path("data/logs/supervisor.log")),
    "ngrok": ("ngrok tunnel", Path("data/logs/ngrok.log")),
    "summary": ("Daily summary (last run)", Path("data/logs/summary-last-run.log")),
}

def _tail(path: Path, lines: int) -> str:
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 512 * 1024))
        text = f.read().decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])

@app.get("/api/logs/system")
def api_logs_system(name: str = "", lines: int = 400) -> JSONResponse:
    from agent.dashboard.security import redact
    if not name:
        return JSONResponse({"logs": [{"name": k, "label": v[0], "exists": v[1].exists()} for k, v in _SYSTEM_LOGS.items()]})
    if name not in _SYSTEM_LOGS:
        raise HTTPException(404, "Unknown log")
    label, path = _SYSTEM_LOGS[name]
    if not path.exists():
        return JSONResponse({"name": name, "label": label, "text": "", "missing": True})
    return JSONResponse({"name": name, "label": label, "text": redact(_tail(path, max(20, min(lines, 5000))))})

@app.get("/api/logs/pod")
def api_logs_pod(namespace: str, pod: str, lines: int = 300) -> JSONResponse:
    from agent.dashboard.security import redact
    from agent.integrations.kubectl import get_pod_events, get_pod_logs, is_cluster_available
    if not is_cluster_available():
        return JSONResponse({"error": "Cluster unreachable — couldn't fetch logs."}, status_code=503)
    return JSONResponse({"logs": redact(get_pod_logs(pod, namespace, lines=max(20, min(lines, 5000)))),
                         "events": get_pod_events(pod, namespace)})

@app.get("/api/logs/jenkins")
def api_logs_jenkins(job: str, build: int, lines: int = 600) -> JSONResponse:
    from agent.dashboard.security import redact
    from agent.integrations import jenkins as jk
    try:
        text = jk.get_console_log(job, build, tail_lines=max(20, min(lines, 10000)))
    except Exception as exc:
        return JSONResponse({"error": f"Couldn't fetch the build log: {str(exc)[:300]}"}, status_code=502)
    return JSONResponse({"logs": redact(text)})


# ── notification inbox ────────────────────────────────────────────────────────
class MarkRead(BaseModel):
    ids: list[str] | None = None        # None = mark everything read

@app.get("/api/notifications")
def api_notifications(limit: int = 50) -> JSONResponse:
    from agent.integrations import notifications
    return JSONResponse(notifications.list_recent(max(1, min(limit, 200))))

@app.post("/api/notifications/read")
def api_notifications_read(req: MarkRead) -> JSONResponse:
    from agent.integrations import notifications
    return JSONResponse({"marked": notifications.mark_read(req.ids)})


# ── GitHub Actions ─────────────────────────────────────────────────────────────
@app.get("/api/github/runs")
def api_github_runs(repo: str = "", limit: int = 30) -> JSONResponse:
    from agent.integrations import github_actions as gha
    if not gha._token():
        return JSONResponse({"configured": False, "runs": []})
    try:
        r = gha.resolve_repo(repo or None)
        return JSONResponse({"configured": True, "repo": r,
                             "runs": gha.list_runs(r, limit=max(1, min(limit, 100)), failed_only=False)})
    except Exception as exc:
        return JSONResponse({"configured": True, "repo": repo, "runs": [], "error": str(exc)[:300]})

@app.get("/api/github/jobs")
def api_github_jobs(repo: str, run_id: int) -> JSONResponse:
    from agent.integrations import github_actions as gha
    try:
        return JSONResponse({"jobs": gha.get_failed_jobs(repo, run_id)})
    except Exception as exc:
        return JSONResponse({"jobs": [], "error": str(exc)[:300]}, status_code=502)

@app.get("/api/github/job-log")
def api_github_job_log(repo: str, job_id: int) -> JSONResponse:
    from agent.dashboard.security import redact
    from agent.integrations import github_actions as gha
    try:
        return JSONResponse({"logs": redact(gha.get_job_log(repo, job_id, tail_chars=80000))})
    except Exception as exc:
        return JSONResponse({"error": f"Couldn't fetch the job log: {str(exc)[:300]}"}, status_code=502)


# ── AWS overview ──────────────────────────────────────────────────────────────
# Identity first: if AWS can't be reached, nothing else is asked and the page
# says "couldn't check". The inventory helpers return [] on API errors, so an
# empty list is only shown as "none" after the identity call has succeeded.
# Cost Explorer costs $0.01 a call and this makes several — cached 1h.
_AWS_TTL_S = 3600
_aws_cache: dict = {"at": 0.0, "data": None}

@app.get("/api/aws/overview")
async def api_aws_overview(force: bool = False) -> JSONResponse:
    import time as _time
    if not force and _aws_cache["data"] is not None and _time.time() - _aws_cache["at"] < _AWS_TTL_S:
        return JSONResponse(_aws_cache["data"])

    def _build():
        from concurrent.futures import ThreadPoolExecutor
        from agent.config import settings
        from agent.integrations.aws import (
            friendly_aws_error, get_all_ec2, get_all_load_balancers, get_all_rds, get_aws_client, get_elastic_ips,
        )
        region = settings.aws_region
        try:
            ident = get_aws_client("sts").get_caller_identity()
        except Exception as exc:
            return {"reachable": False, "region": region, "error": friendly_aws_error(exc)}

        def inv(fn):
            try:
                return fn(region)
            except Exception as exc:
                return [{"error": str(exc)[:200]}]
        with ThreadPoolExecutor(max_workers=2) as pool:      # boto3 is flaky with more (see aws_inventory)
            ec2, rds, lbs, eips = pool.map(inv, [get_all_ec2, get_all_rds, get_all_load_balancers, get_elastic_ips])

        from agent.integrations.aws_cost import get_cost_and_usage
        cost = get_cost_and_usage(days=30)
        cost_ok = bool(cost.get("by_service") or cost.get("daily"))
        return {
            "reachable": True, "region": region,
            "identity": {"arn": ident.get("Arn", ""), "account": ident.get("Account", "")},
            "cost": cost if cost_ok else None,
            "cost_error": None if cost_ok else "Cost Explorer returned nothing (permission or billing access?)",
            "inventory": {"ec2": ec2, "rds": rds, "load_balancers": lbs, "elastic_ips": eips},
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    data = await asyncio.get_event_loop().run_in_executor(None, _build)
    if data.get("reachable"):
        _aws_cache.update(data=data, at=_time.time())
    return JSONResponse(data)


# ── incident actions ──────────────────────────────────────────────────────────
class ResolveIncident(BaseModel):
    note: str = ""

@app.get("/api/incidents/{incident_id}")
def api_incident(incident_id: str) -> JSONResponse:
    from agent.integrations import incident_db
    inc = incident_db.get_incident(incident_id)
    if inc is None:
        raise HTTPException(404, "No such incident")
    return JSONResponse(inc)

@app.post("/api/incidents/{incident_id}/ack")
def api_incident_ack(incident_id: str) -> JSONResponse:
    from agent.skills.incident import acknowledge_incident
    if not acknowledge_incident(incident_id, who="dashboard"):
        raise HTTPException(409, "Incident not found or already resolved")
    audit.record(f"Dashboard: acknowledged incident {incident_id}")
    return JSONResponse({"acknowledged": True})

@app.post("/api/incidents/{incident_id}/resolve")
def api_incident_resolve(incident_id: str, req: ResolveIncident) -> JSONResponse:
    """Same path as `agent incident resolve`: closes the on-call page and
    ends the SLO burn too (skills/incident.resolve_incident)."""
    from agent.integrations import incident_db
    from agent.skills.incident import resolve_incident
    inc = incident_db.get_incident(incident_id)
    if inc is None:
        raise HTTPException(404, "No such incident")
    if inc.get("status") == "resolved":
        return JSONResponse({"resolved": True, "already": True})
    resolve_incident(incident_id, cause=(req.note.strip() or "resolved from the dashboard")[:500], auto_fixed=False)
    audit.record(f"Dashboard: resolved incident {incident_id} ({inc.get('title', '')})", note=req.note.strip())
    return JSONResponse({"resolved": True})


# ── System page: AtlasOS's own health ─────────────────────────────────────────
_SERVER_STARTED = datetime.now(timezone.utc).isoformat()

@app.get("/api/system")
def api_system() -> JSONResponse:
    from agent.core import backup as bk
    from agent.core import supervisor as sv
    from agent.integrations import event_queue as eq

    try:
        sup_state = json.loads(Path(sv.STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sup_state = {}
    sup_pid = sv.running_supervisor_pid()

    try:
        queue = eq.get_stats()
        pending = eq.list_events(status="pending", limit=200)
        oldest = min((e.get("created_at") for e in pending if e.get("created_at")), default=None)
        dead = eq.list_events(status="dead", limit=20)
        queue_out = {"stats": queue, "oldest_pending": oldest,
                     "dead": [{k: e.get(k) for k in ("id", "event_type", "error", "retry_count", "created_at")} for e in dead]}
    except Exception as exc:
        queue_out = {"error": str(exc)[:300]}

    try:
        backups = [{"name": b.name, "created": b.created.isoformat(), "size": b.size, "pre_restore": b.pre_restore}
                   for b in bk.list_backups()][:20]
        backup_out = {"dir": str(bk.backup_dir()), "items": backups}
    except Exception as exc:
        backup_out = {"error": str(exc)[:300], "items": []}

    return JSONResponse({
        "dashboard": {"pid": os.getpid(), "started": _SERVER_STARTED, "python": sys.version.split()[0]},
        "supervisor": {"running": bool(sup_pid), "pid": sup_pid, "updated": sup_state.get("updated"),
                       "services": sup_state.get("services", {}) if sup_pid else {}},
        "daemon": _daemon_status_dict(),
        "queue": queue_out,
        "backups": backup_out,
    })

@app.post("/api/system/backup")
async def api_system_backup() -> JSONResponse:
    from agent.core import backup as bk
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, bk.create_backup)
    except Exception as exc:
        return JSONResponse({"detail": f"Backup failed: {exc}"}, status_code=500)
    audit.record(f"Dashboard: backup {info.name} created")
    return JSONResponse({"name": info.name, "size": info.size, "created": info.created.isoformat()})

class BackupName(BaseModel):
    name: str

@app.post("/api/system/backup/verify")
async def api_system_backup_verify(req: BackupName) -> JSONResponse:
    from agent.core import backup as bk
    # Only names from the backup list — backup.resolve() would also accept any
    # file path on the machine, which a web endpoint must not.
    known = {b.name: b.path for b in bk.list_backups()}
    if req.name not in known:
        raise HTTPException(404, "No such backup")
    try:
        manifest = await asyncio.get_event_loop().run_in_executor(None, bk.verify_backup, known[req.name])
    except Exception as exc:
        return JSONResponse({"ok": False, "detail": str(exc)[:400]})
    return JSONResponse({"ok": True, "files": len(manifest.get("files", {}) if isinstance(manifest, dict) else [])})

@app.post("/api/system/events/{event_id}/retry")
def api_system_event_retry(event_id: str) -> JSONResponse:
    from agent.integrations import event_queue as eq
    if not eq.retry_dead(event_id):
        raise HTTPException(404, "Not a dead event")
    audit.record(f"Dashboard: re-queued dead event {event_id}")
    return JSONResponse({"requeued": True})


# ── everything-in-one-place: helpers ──────────────────────────────────────────
# The endpoints below give the dashboard what used to need the CLI. Each one
# calls the same skill/integration function as its `agent ...` command, and
# every change is written to the audit trail (dashboard/audit.py).
import re as _re

from agent.dashboard import audit

_K8S_NAME = _re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_REPO = _re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_BRANCH = _re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_IMAGE = _re.compile(r"^[A-Za-z0-9._/:@-]{0,300}$")
_HHMM = _re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DOMAIN = _re.compile(r"^(?=.{1,253}$)([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_EMAIL = _re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[a-zA-Z]{2,63}$")
# Runbook values end up inside a kubectl command that is split on spaces, so
# only plain names are accepted — anything else could add kubectl arguments.
_RB_VALUE = _re.compile(r"^[A-Za-z0-9._:/@-]{1,253}$")


def _bad(detail: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=code)


def _jsonable(x: Any) -> Any:
    """Skill results mix dicts with dataclasses, pydantic models and datetimes."""
    import dataclasses

    def _default(o: Any) -> Any:
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        if hasattr(o, "model_dump"):
            return o.model_dump(mode="json")
        if isinstance(o, datetime):
            return o.isoformat()
        if hasattr(o, "__dict__"):
            return {k: v for k, v in vars(o).items() if not k.startswith("_")}
        return str(o)
    return json.loads(json.dumps(x, default=_default))


async def _in_thread(fn, *args, timeout: float | None = None):
    fut = asyncio.get_event_loop().run_in_executor(None, fn, *args)
    return await (asyncio.wait_for(fut, timeout) if timeout else fut)


# ── 1. setup checklist ────────────────────────────────────────────────────────
@app.get("/api/setup")
def api_setup() -> JSONResponse:
    """What's connected so far, as a first-run checklist. Only reads local
    state (settings, files, pid) — the cluster step is added by the browser
    from the live socket, which already knows whether the API server answers."""
    from agent.core import settings_store
    from agent.integrations.mapping_loader import get_loader

    fields = {f["name"]: f for i in settings_store.describe()["integrations"] for f in i["fields"]}
    is_set = lambda *names: any(fields.get(n, {}).get("set") for n in names)   # noqa: E731
    try:
        mappings = len(get_loader().load().mappings)
    except Exception:
        mappings = 0
    try:
        from agent.integrations import slo_db
        slos = len(slo_db.list_slos(active_only=True)) if Path(slo_db._DB_PATH).exists() else 0
    except Exception:
        slos = 0
    aws_cfg = is_set("AWS_PROFILE", "AWS_ACCESS_KEY_ID") or (Path.home() / ".aws" / "credentials").exists() \
        or (Path.home() / ".aws" / "config").exists()

    steps = [
        {"id": "protect", "title": "Protect the dashboard with a login", "done": bool(expected_token()),
         "go": "settings", "hint": "Anyone who can open this page could otherwise change your systems."},
        {"id": "claude", "title": "Connect Claude (AI)", "done": is_set("ANTHROPIC_API_KEY"), "go": "settings",
         "hint": "Powers diagnoses and the assistant."},
        {"id": "slack", "title": "Connect Slack", "done": is_set("SLACK_WEBHOOK_URL"), "go": "settings",
         "hint": "Alerts, approvals and the daily summary."},
        {"id": "ci", "title": "Connect Jenkins or GitHub Actions", "done": is_set("JENKINS_URL", "GITHUB_TOKEN"),
         "go": "settings", "hint": "Build failures get diagnosed and, where safe, fixed."},
        {"id": "aws", "title": "Connect AWS", "done": bool(aws_cfg), "go": "settings", "optional": True,
         "hint": "Spend, idle resources and database health."},
        {"id": "webhook", "title": "Set up deploys from GitHub pushes", "done": bool(is_set("GITHUB_WEBHOOK_SECRET") and mappings),
         "go": "deploys", "optional": True, "hint": "A push becomes a risk-checked deploy waiting for your approval."},
        {"id": "slo", "title": "Define an SLO", "done": slos > 0, "go": "incidents", "optional": True,
         "hint": "Incidents burn an error budget; an empty budget blocks risky deploys."},
        {"id": "daemon", "title": "Start the healing daemon", "done": _daemon_running(), "go": "system",
         "hint": "Watches the cluster around the clock and fixes what it safely can."},
    ]
    return JSONResponse({"steps": steps})


# ── 3. SLOs: create / delete ──────────────────────────────────────────────────
class SloCreate(BaseModel):
    name: str
    service: str
    namespace: str = "default"
    target_pct: float = 99.9
    window_days: int = 30

@app.post("/api/slos")
def api_slo_create(req: SloCreate) -> JSONResponse:
    """Same call as `agent slo create`."""
    from agent.integrations import slo_db
    name = req.name.strip()
    if not name or len(name) > 120:
        return _bad("Give the SLO a name (up to 120 characters).")
    if not _K8S_NAME.match(req.service) or not _K8S_NAME.match(req.namespace):
        return _bad("Service and namespace must be Kubernetes names (lowercase letters, digits, '-').")
    if not 50 <= req.target_pct < 100:
        return _bad("Target must be between 50 and 99.999 %.")
    if not 1 <= req.window_days <= 90:
        return _bad("Window must be 1–90 days.")
    if slo_db.get_slo_by_service(req.service, req.namespace):
        return _bad(f"{req.service}/{req.namespace} already has an SLO — delete it first.", 409)
    slo_id = slo_db.create_slo(name, req.service, req.namespace, target_pct=req.target_pct, window_days=req.window_days)
    audit.record(f"Dashboard: created SLO '{name}' for {req.service}/{req.namespace} "
                 f"({req.target_pct}% over {req.window_days}d)", slo_id=slo_id)
    return JSONResponse({"id": slo_id})

@app.delete("/api/slos/{slo_id}")
def api_slo_delete(slo_id: str) -> JSONResponse:
    from agent.integrations import slo_db
    slo = slo_db.get_slo(slo_id)
    if not slo or not slo_db.delete_slo(slo_id):
        raise HTTPException(404, "No such SLO")
    audit.record(f"Dashboard: deleted SLO '{slo['name']}' ({slo['service']}/{slo['namespace']})", slo_id=slo_id)
    return JSONResponse({"deleted": True})


# ── 4. rotate the dashboard token ─────────────────────────────────────────────
@app.post("/api/settings/rotate-token")
def api_settings_rotate_token(request: Request) -> JSONResponse:
    """Replace DASHBOARD_TOKEN. Only reachable while logged in (the guard
    checks the current token first). Every other browser is logged out; this
    one gets the new cookie, and the new token is shown once."""
    if not expected_token():
        return _bad("This dashboard has no login yet — use Protect instead.", 409)
    from agent.core import secrets as sec
    token = sec.generate_token()
    try:
        sec.store("DASHBOARD_TOKEN", token)
    except sec.SecretsError as exc:
        return _bad(f"Couldn't store the new token: {exc}", 500)
    os.environ["DASHBOARD_TOKEN"] = token
    from agent.dashboard import security
    security._kc_cache["at"] = 0.0
    audit.record("Dashboard: login token rotated (other sessions logged out)")
    resp = JSONResponse({"rotated": True, "token": token})
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                    secure=request.url.scheme == "https", max_age=30 * 86400, path="/")
    return resp


# ── 2. deploys from GitHub pushes (webhook + mappings) ────────────────────────
class MappingIn(BaseModel):
    repo: str
    branch: str = "main"
    deployment: str
    namespace: str = "default"
    image_prefix: str = ""
    auto_approve_low_risk: bool = False

@app.get("/api/deploys/setup")
def api_deploys_setup() -> JSONResponse:
    from agent.core import settings_store
    from agent.core import supervisor as sv
    from agent.integrations.mapping_loader import get_loader
    fields = {f["name"]: f for i in settings_store.describe()["integrations"] for f in i["fields"]}
    secret = fields.get("GITHUB_WEBHOOK_SECRET", {})
    try:
        mappings = [m.model_dump() for m in get_loader().load().mappings]
        err = None
    except Exception as exc:
        mappings, err = [], str(exc)[:300]
    history = _query("data/deploy.db",
        "SELECT id, repo, branch, deployment, namespace, new_image, risk_label, status, created_at "
        "FROM pending_deploys ORDER BY created_at DESC LIMIT 15")
    return JSONResponse({
        "secret_set": bool(secret.get("set")), "secret_where": secret.get("where"),
        "protected": bool(expected_token()),
        "urls": [f"https://{h}/webhook/github" for h in sv.ngrok_hosts()],
        "local_url": f"http://127.0.0.1:{sv.MCP_PORT}/webhook/github",
        "mappings": mappings, "error": err, "history": history,
    })

class SecretReq(BaseModel):
    replace: bool = False

@app.post("/api/deploys/secret")
def api_deploys_secret(req: SecretReq) -> JSONResponse:
    """Generate GITHUB_WEBHOOK_SECRET into the keychain (via settings_store,
    the Settings page's path) and show it once, to paste into GitHub."""
    from agent.core import settings_store
    from agent.core import secrets as sec
    if not expected_token():
        return _bad("Protect the dashboard first — a webhook secret is a credential.", 403)
    fields = {f["name"]: f for i in settings_store.describe()["integrations"] for f in i["fields"]}
    if fields.get("GITHUB_WEBHOOK_SECRET", {}).get("set") and not req.replace:
        return _bad("A webhook secret is already set. Replace it only if you'll update GitHub too.", 409)
    value = sec.generate_token()
    try:
        settings_store.save({"GITHUB_WEBHOOK_SECRET": value})
    except settings_store.SettingsError as exc:
        return _bad(str(exc))
    audit.record(f"Dashboard: GitHub webhook secret {'replaced' if req.replace else 'created'}")
    return JSONResponse({"secret": value,
                         "note": "Restart AtlasOS's MCP server (it serves the webhook) so it uses the new secret."})

@app.post("/api/deploys/mappings")
async def api_deploys_add_mapping(req: MappingIn) -> JSONResponse:
    """Same as `agent deploy add-mapping`: validate against the cluster, then
    save even if validation only produced warnings (the CLI asks; we report)."""
    from agent.core.models import WebhookMapping
    from agent.integrations.mapping_loader import get_loader
    if not _REPO.match(req.repo):
        return _bad("Repo must be owner/name, e.g. salmonstone/infragpt.")
    if not _BRANCH.match(req.branch):
        return _bad("That branch name isn't valid.")
    if not _K8S_NAME.match(req.deployment) or not _K8S_NAME.match(req.namespace):
        return _bad("Deployment and namespace must be Kubernetes names.")
    if not _IMAGE.match(req.image_prefix):
        return _bad("Image prefix may only contain registry/path characters.")
    mapping = WebhookMapping(**req.model_dump())
    loader = get_loader()
    try:
        result = await _in_thread(loader.validate_mapping, mapping, timeout=20)
        warnings = [] if result.valid else list(result.errors)
    except asyncio.TimeoutError:
        warnings = ["Couldn't validate against the cluster (no answer in 20s) — saved anyway."]
    except Exception as exc:
        warnings = [f"Couldn't validate against the cluster: {str(exc)[:200]}"]
    loader.add_mapping(mapping)
    audit.record(f"Dashboard: deploy mapping {req.repo}:{req.branch} -> {req.deployment}/{req.namespace}",
                 auto_approve_low_risk=req.auto_approve_low_risk)
    return JSONResponse({"saved": True, "warnings": warnings})

@app.delete("/api/deploys/mappings")
def api_deploys_remove_mapping(repo: str, branch: str = "main") -> JSONResponse:
    from agent.integrations.mapping_loader import get_loader
    if not get_loader().remove_mapping(repo, branch):
        raise HTTPException(404, "No such mapping")
    audit.record(f"Dashboard: removed deploy mapping {repo}:{branch}")
    return JSONResponse({"removed": True})


# ── 5. runbooks ───────────────────────────────────────────────────────────────
def _runbook_vars(rb: dict) -> list[str]:
    found: list[str] = []
    for step in rb.get("steps", []):
        texts = [str(step.get(k, "")) for k in ("command", "message")] + [str(v) for v in (step.get("args") or {}).values()]
        for t in texts:
            for v in _re.findall(r"\{([a-z_]+)\}", t):
                if v != "runbook_name" and v not in found:
                    found.append(v)
    return found

@app.get("/api/runbooks/catalog")
def api_runbooks_catalog() -> JSONResponse:
    from agent.integrations import runbook_db
    from agent.skills import runbook as rbk
    books = []
    for rb in rbk._load_runbooks():
        books.append({
            "id": rb.get("id", ""), "name": rb.get("name", ""), "description": rb.get("description", ""),
            "trigger_condition": rb.get("trigger_condition", ""), "vars": _runbook_vars(rb),
            "steps": [{"name": s.get("name", ""), "type": s.get("type", ""), "on_failure": s.get("on_failure", "escalate"),
                       "detail": str(s.get("command") or s.get("function") or s.get("message") or "")[:200]}
                      for s in rb.get("steps", [])],
        })
    try:
        runs = runbook_db.list_runs(limit=20)
    except Exception:
        runs = []
    return JSONResponse({"runbooks": books, "runs": runs, "file": str(rbk._RUNBOOKS_PATH)})

@app.get("/api/runbooks/runs/{run_id}")
def api_runbook_run_steps(run_id: str) -> JSONResponse:
    from agent.integrations import runbook_db
    run = runbook_db.get_run(run_id)
    if not run:
        raise HTTPException(404, "No such run")
    return JSONResponse({"run": run, "steps": runbook_db.get_steps(run_id)})

class RunbookRun(BaseModel):
    context: dict[str, str] = {}

@app.post("/api/runbooks/{runbook_id}/run")
async def api_runbook_run(runbook_id: str, req: RunbookRun) -> JSONResponse:
    """Same call as `agent runbook run`. Only the runbook's own variables are
    accepted, and only as plain names (see _RB_VALUE)."""
    from agent.skills import runbook as rbk
    rb = rbk.get_runbook(runbook_id)
    if not rb:
        raise HTTPException(404, "No such runbook")
    allowed = set(_runbook_vars(rb))
    ctx: dict[str, str] = {}
    for k, v in req.context.items():
        v = v.strip()
        if not v:
            continue
        if k not in allowed:
            return _bad(f"'{k}' isn't a variable of this runbook.")
        if not _RB_VALUE.match(v):
            return _bad(f"'{k}' must be a plain name (letters, digits, . _ : / @ -).")
        ctx[k] = v
    ctx.setdefault("namespace", "default")
    audit.record(f"Dashboard: ran runbook {runbook_id}", context=json.dumps(ctx))
    result = await _in_thread(lambda: rbk.run_runbook(runbook_id, context=ctx, trigger="dashboard"))
    return JSONResponse(_jsonable(result))


# ── 6. auto-scaling policies ──────────────────────────────────────────────────
class ScalePolicyIn(BaseModel):
    deployment: str
    namespace: str = "default"
    down_time: str = "17:30"
    up_time: str = "03:30"
    down_replicas: int = 1
    up_replicas: int = 3
    cpu_pct: float = 80.0

@app.get("/api/scale")
def api_scale() -> JSONResponse:
    from agent.integrations import autoscale_db
    return JSONResponse({"policies": autoscale_db.list_policies(enabled_only=False),
                         "events": autoscale_db.get_events(limit=20),
                         "daemon_running": _daemon_running()})

@app.post("/api/scale")
def api_scale_add(req: ScalePolicyIn) -> JSONResponse:
    """Same as `agent scale add`. The daemon applies the schedule."""
    from agent.integrations import autoscale_db
    if not _K8S_NAME.match(req.deployment) or not _K8S_NAME.match(req.namespace):
        return _bad("Deployment and namespace must be Kubernetes names.")
    if not _HHMM.match(req.down_time) or not _HHMM.match(req.up_time):
        return _bad("Times are HH:MM in UTC, e.g. 17:30.")
    if not (0 <= req.down_replicas <= 50 and 1 <= req.up_replicas <= 50):
        return _bad("Replicas: down 0–50, up 1–50.")
    if not 10 <= req.cpu_pct <= 100:
        return _bad("CPU threshold must be 10–100 %.")
    pid = autoscale_db.add_policy(deployment=req.deployment, namespace=req.namespace, name=f"{req.deployment}-policy",
                                  min_r=1, max_r=max(10, req.up_replicas), down_utc=req.down_time, up_utc=req.up_time,
                                  down_r=req.down_replicas, up_r=req.up_replicas, cpu_pct=req.cpu_pct)
    audit.record(f"Dashboard: scaling policy for {req.deployment}/{req.namespace} — down {req.down_time} UTC "
                 f"to {req.down_replicas}, up {req.up_time} UTC to {req.up_replicas}", policy_id=pid)
    return JSONResponse({"id": pid})

@app.post("/api/scale/{policy_id}/{action}")
def api_scale_toggle(policy_id: str, action: str) -> JSONResponse:
    from agent.integrations import autoscale_db
    pol = autoscale_db.get_policy(policy_id)
    if not pol:
        raise HTTPException(404, "No such policy")
    if action == "enable":
        autoscale_db.enable_policy(policy_id)
    elif action == "disable":
        autoscale_db.disable_policy(policy_id)
    else:
        raise HTTPException(404, "Unknown action")
    audit.record(f"Dashboard: {action}d scaling policy for {pol['deployment']}/{pol['namespace']}", policy_id=policy_id)
    return JSONResponse({"enabled": action == "enable"})


# ── 7. domains & HTTPS ────────────────────────────────────────────────────────
@app.get("/api/domains")
async def api_domains(domain: str = "") -> JSONResponse:
    """Every Ingress domain checked end to end (LB → DNS → TLS → HTTP → pods),
    the same check as `agent domain live`. Read-only."""
    from agent.integrations.kubectl import is_cluster_available
    domain = domain.strip().lower()
    if domain and not _DOMAIN.match(domain):
        return _bad("That isn't a valid domain name.")
    if not await _in_thread(is_cluster_available):
        return JSONResponse({"reachable": False, "results": []})
    from agent.skills.domain import DomainSkill
    try:
        results = await _in_thread(lambda: DomainSkill().live(domain or None), timeout=120)
    except asyncio.TimeoutError:
        return JSONResponse({"reachable": True, "results": [], "error": "The check took over 2 minutes."})
    return JSONResponse({"reachable": True, "results": _jsonable(results)})

class EncryptReq(BaseModel):
    domain: str
    email: str
    namespace: str = "default"
    ingress: str = ""
    staging: bool = False

@app.post("/api/domains/encrypt")
async def api_domains_encrypt(req: EncryptReq) -> JSONResponse:
    """Same as `agent domain encrypt`: ClusterIssuer, Certificate, ingress TLS."""
    from agent.integrations.kubectl import is_cluster_available
    domain = req.domain.strip().lower()
    if not _DOMAIN.match(domain):
        return _bad("That isn't a valid domain name.")
    if not _EMAIL.match(req.email.strip()):
        return _bad("Let's Encrypt needs a valid email for expiry notices.")
    if not _K8S_NAME.match(req.namespace) or (req.ingress and not _K8S_NAME.match(req.ingress)):
        return _bad("Namespace and ingress must be Kubernetes names.")
    if not await _in_thread(is_cluster_available):
        return _bad("The cluster isn't reachable.", 503)
    from agent.skills.domain import DomainSkill
    audit.record(f"Dashboard: requested Let's Encrypt certificate for {domain} "
                 f"({'staging' if req.staging else 'production'}) in {req.namespace}")
    result = await _in_thread(lambda: DomainSkill().encrypt(domain=domain, email=req.email.strip(), namespace=req.namespace,
                                                            ingress=req.ingress or None, staging=req.staging))
    return JSONResponse(_jsonable(result))


# ── 8. databases (RDS / Aurora) ───────────────────────────────────────────────
_DB_TTL_S = 600
_db_cache: dict = {"at": 0.0, "data": None}

@app.get("/api/databases")
async def api_databases(force: bool = False) -> JSONResponse:
    """Same scan as `agent db scan` (it opens an incident for a critical
    finding, like the CLI). Identity first, so "couldn't check" never looks
    like "no databases". Cached 10 min — it's many CloudWatch calls."""
    import time as _time
    if not force and _db_cache["data"] is not None and _time.time() - _db_cache["at"] < _DB_TTL_S:
        return JSONResponse(_db_cache["data"])

    def _build():
        from agent.config import settings
        from agent.integrations.aws import friendly_aws_error, get_aws_client
        region = settings.aws_region
        try:
            get_aws_client("sts").get_caller_identity()
        except Exception as exc:
            return {"reachable": False, "region": region, "error": friendly_aws_error(exc)}
        from agent.skills.db_health import scan_all_databases
        try:
            results = scan_all_databases(region)
        except Exception as exc:
            return {"reachable": True, "region": region, "databases": [], "error": str(exc)[:300]}
        return {"reachable": True, "region": region, "databases": _jsonable(results),
                "generated_at": datetime.now(timezone.utc).isoformat()}

    data = await _in_thread(_build)
    if data.get("reachable") and not data.get("error"):
        _db_cache.update(data=data, at=_time.time())
    return JSONResponse(data)


# ── 9. daily summary: send now, schedule, history ─────────────────────────────
_SUMMARY_TASK = "AtlasOS Daily Summary"

def _scheduled_task(name: str) -> dict:
    if sys.platform != "win32":
        return {"available": False, "detail": "Scheduling is shown on Windows only."}
    try:
        out = subprocess.run(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"], capture_output=True,
                             text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return {"available": False, "detail": str(exc)[:200]}
    if out.returncode != 0:
        return {"available": False, "detail": f"No scheduled task named “{name}”."}
    want = {"Next Run Time": "next_run", "Last Run Time": "last_run", "Last Result": "last_result",
            "Status": "status", "Start Time": "start_time", "Schedule Type": "schedule"}
    info: dict = {"available": True, "name": name}
    for line in out.stdout.splitlines():
        key, _, val = line.partition(":")
        if key.strip() in want and want[key.strip()] not in info:
            info[want[key.strip()]] = val.strip()
    return info

@app.get("/api/summary/info")
def api_summary_info() -> JSONResponse:
    from agent.integrations import notifications
    from agent.integrations.slack import is_configured
    history = [n for n in notifications.list_recent(200)["items"] if n.get("kind") == "summary"][:14]
    return JSONResponse({"schedule": _scheduled_task(_SUMMARY_TASK), "slack": is_configured(), "history": history})

class SummarySend(BaseModel):
    force: bool = False

@app.post("/api/summary/send")
async def api_summary_send(req: SummarySend) -> JSONResponse:
    """Same as `agent summary send` — once a day unless force."""
    from agent.skills import daily_summary as ds
    r = await _in_thread(lambda: ds.send(force=req.force))
    if r.get("sent"):
        audit.record("Dashboard: daily summary sent to Slack" + (" (again today)" if req.force else ""))
    return JSONResponse(r)


# ── 10. activity: the audit trail ─────────────────────────────────────────────
# Sources that record something being *done*; everything else is a scan or
# diagnosis. The Activity page shows these by default.
_ACTION_SOURCES = ["dashboard", "approvals", "auto-healer", "node-healer", "k8s-fix", "security-fix", "tls-fix",
                   "cost-aws-fix", "terraform-fix", "deployment", "backup", "secrets", "multi-cluster-add"]

@app.get("/api/activity")
def api_activity(view: str = "actions", source: str = "", q: str = "", before: str = "", limit: int = 50) -> JSONResponse:
    from agent.dashboard.security import redact
    from agent.memory import store
    sources = [source] if source else (_ACTION_SOURCES if view == "actions" else None)
    items = store.list_memories(limit=max(1, min(limit, 200)), sources=sources, query=q.strip()[:200], before=before)
    return JSONResponse({
        "items": [{"id": m.id, "source": m.source, "created_at": m.created_at.isoformat(),
                   "content": redact(m.content[:4000]), "truncated": len(m.content) > 4000} for m in items],
        "sources": store.source_counts(), "action_sources": _ACTION_SOURCES,
    })


# ── 12. search across everything ──────────────────────────────────────────────
@app.get("/api/search")
def api_search(q: str) -> JSONResponse:
    """Ctrl+K search over AtlasOS's own records — local stores and the last
    AWS snapshot; nothing here calls an external API."""
    needle = q.strip().lower()
    if len(needle) < 2:
        return JSONResponse({"results": []})
    hit = lambda *parts: any(needle in str(p or "").lower() for p in parts)   # noqa: E731
    out: list[dict] = []

    def add(kind, icon, title, sub, go):
        out.append({"kind": kind, "icon": icon, "title": str(title)[:160], "sub": str(sub)[:200], "go": go})

    try:
        from agent.integrations import incident_db
        for i in incident_db.list_incidents(limit=200, since_hours=24 * 365):
            if hit(i.get("id"), i.get("title"), i.get("service"), i.get("namespace")):
                add("Incident", "ph-siren", i["title"], f"{i.get('status')} · {i.get('service')}/{i.get('namespace')}", "incidents")
    except Exception:
        pass
    try:
        from agent.integrations import slo_db
        if Path(slo_db._DB_PATH).exists():
            for s in slo_db.list_slos(active_only=True):
                if hit(s.get("name"), s.get("service")):
                    add("SLO", "ph-gauge", s["name"], f"{s['service']}/{s['namespace']} · {s['target_pct']}%", "incidents")
    except Exception:
        pass
    try:
        from agent.skills.runbook import list_runbooks
        for rb in list_runbooks():
            if hit(rb["id"], rb["name"], rb["description"]):
                add("Runbook", "ph-book-open", rb["name"], rb["description"], "automation")
    except Exception:
        pass
    try:
        from agent.integrations import autoscale_db
        for p in autoscale_db.list_policies(enabled_only=False):
            if hit(p.get("deployment"), p.get("namespace"), p.get("name")):
                add("Scaling policy", "ph-arrows-out-line-vertical", p["deployment"], f"{p['namespace']} · down {p.get('schedule_down_utc')} / up {p.get('schedule_up_utc')} UTC", "automation")
    except Exception:
        pass
    try:
        from agent.integrations.mapping_loader import get_loader
        for m in get_loader().load().mappings:
            if hit(m.repo, m.deployment, m.namespace):
                add("Deploy mapping", "ph-git-branch", f"{m.repo}:{m.branch}", f"→ {m.deployment}/{m.namespace}", "deploys")
    except Exception:
        pass
    inv = ((_aws_cache.get("data") or {}).get("inventory")) or {}
    for e in inv.get("ec2", []):
        if hit(e.get("id"), e.get("name"), e.get("private_ip"), e.get("public_ip")):
            add("EC2", "ph-hard-drives", e.get("name") or e.get("id"), f"{e.get('id')} · {e.get('instance_type')} · {e.get('state')}", "aws")
    for r in inv.get("rds", []):
        if hit(r.get("id"), r.get("engine")):
            add("RDS", "ph-database", r.get("id"), f"{r.get('engine')} · {r.get('status')}", "databases")
    for lb in inv.get("load_balancers", []):
        if hit(lb.get("name"), lb.get("dns_name")):
            add("Load balancer", "ph-arrows-split", lb.get("name"), lb.get("dns_name", ""), "aws")
    try:
        from agent.integrations import notifications
        for n in notifications.list_recent(200)["items"]:
            if hit(n.get("title"), n.get("message")):
                add("Notification", "ph-bell", n["title"], n.get("created_at", "")[:16].replace("T", " "), "activity")
                if sum(1 for o in out if o["kind"] == "Notification") >= 5:
                    break
    except Exception:
        pass
    try:
        from agent.memory import store
        for m in store.list_memories(limit=5, query=needle):
            add("Activity", "ph-clock-counter-clockwise", m.content.splitlines()[0][:140] if m.content else m.source,
                f"{m.source} · {m.created_at:%Y-%m-%d %H:%M}", "activity")
    except Exception:
        pass
    return JSONResponse({"results": out[:40]})


# ── PWA assets: served at the site root, so each needs its own route — the
# catch-all below would otherwise return index.html for them too. Must come
# before that catch-all.
if _DIST.exists():
    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(str(_DIST / "favicon.svg"), media_type="image/svg+xml")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def pwa_manifest() -> FileResponse:
        return FileResponse(str(_DIST / "manifest.webmanifest"), media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(str(_DIST / "sw.js"), media_type="text/javascript")


# ── SPA catch-all (must be last — only when dist/ is present) ─────────────────
if _DIST.exists():
    @app.get("/{full_path:path}")
    def spa_catchall(full_path: str):
        index = _DIST / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return JSONResponse({"error": "index.html not found"}, status_code=404)
