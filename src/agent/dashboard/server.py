"""AtlasOS Dashboard — FastAPI backend.

Thin HTTP/WebSocket wrapper around existing agent skills.
Zero business logic here — every endpoint delegates to existing
functions in src/agent/skills/ and src/agent/integrations/.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_DIST = Path(__file__).parent / "frontend" / "dist"

app = FastAPI(title="AtlasOS Dashboard API", docs_url="/api/docs", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        con = sqlite3.connect(str(p), check_same_thread=False)
        con.row_factory = sqlite3.Row
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
    if group == "k8s":
        if name == "scan":
            from agent.skills.k8s import full_scan
            return full_scan(namespace=params.get("namespace", "all"))
        if name == "pods":
            from agent.integrations.kubectl import run_kubectl
            r = run_kubectl(["get", "pods", "-A"])
            return {"output": r.output or r.error or ""}

    # ── resources ─────────────────────────────────────────────────────────────
    if group == "resources":
        if name == "scan":
            from agent.skills.resource_monitor import scan_resources
            return scan_resources()

    # ── cost ─────────────────────────────────────────────────────────────────
    if group == "cost":
        if name == "scan":
            from agent.skills.cost import full_aws_analysis
            return full_aws_analysis()

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
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.core.daemon"],
        creationflags=flags,
    )
    Path("data/daemon.pid").write_text(str(proc.pid))
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
    if not Path("data/slo.db").exists():
        return JSONResponse([])
    try:
        from agent.integrations import slo_db
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


# ── SPA catch-all (must be last — only when dist/ is present) ─────────────────
if _DIST.exists():
    @app.get("/{full_path:path}")
    def spa_catchall(full_path: str):
        index = _DIST / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return JSONResponse({"error": "index.html not found"}, status_code=404)
