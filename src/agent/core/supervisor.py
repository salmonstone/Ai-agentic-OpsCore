"""
Process supervisor: keeps AtlasOS's long-running pieces up.

Before this, the remote MCP server and the ngrok tunnel were started by hand
in two terminals. A crash, a closed window, sleep, or a reboot took them down
until someone noticed — and even when both were running, starting them in the
wrong order left the MCP server with an empty Host allowlist (it reads the
ngrok hostname once, at startup), so every tunnelled request got 421.

`agent supervise run` owns those processes:

- restarts a service that exits, with exponential backoff (2s … 5min) so a
  crash loop does not spin the CPU; the backoff resets once it stays up;
- probes health (MCP: /mcp answers 401; ngrok: a tunnel to our port exists)
  and restarts a service that is running but hung;
- starts MCP after ngrok and hands it ngrok's current hostname explicitly,
  restarting MCP if that hostname changes;
- writes data/supervisor.json so `agent supervise status` can show state;
- stops cleanly (children included) when data/supervisor.stop appears, which
  is how `agent supervise stop` asks — a hard kill would orphan children.

It is started at sign-in by a Task Scheduler entry (see start-atlasos.bat).
The autonomous healing daemon is opt-in (--services ngrok,mcp,daemon): it
applies fixes on its own, and running it 24/7 is a separate decision.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

STATE_FILE = Path("data/supervisor.json")
PID_FILE = Path("data/supervisor.pid")
STOP_FILE = Path("data/supervisor.stop")
LOG_DIR = Path("data/logs")
LOG_ROTATE_BYTES = 5 * 1024 * 1024
LOG_KEEP = 3

MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
NGROK_API = os.getenv("NGROK_API_URL", "http://127.0.0.1:4040").rstrip("/")


def _now() -> float:
    return time.monotonic()


def _replace_atomic(tmp: Path, path: Path, attempts: int = 6) -> bool:
    """os.replace with retries.

    Observed live: Windows Defender or the search indexer can hold a
    just-written file open for scanning for a moment, and os.replace() onto
    it then raises PermissionError (WinError 5) even though nothing in this
    process has it open. That is transient — retrying briefly clears it.
    On persistent failure this drops the write rather than raise: the state
    file is informational (`agent supervise status`), and losing one write is
    far cheaper than the alternative of crashing the loop that is supervising
    real services over it.
    """
    delay = 0.05
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return True
        except OSError:
            if attempt == attempts - 1:
                tmp.unlink(missing_ok=True)
                return False
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
    return False


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


# ---------------------------------------------------------------------------
# service definitions
# ---------------------------------------------------------------------------

@dataclass
class ServiceSpec:
    name: str
    command: Callable[[], list[str] | None]         # None = not available here
    env: Callable[[], dict[str, str]] = dict        # extra env, re-evaluated for drift
    health: Callable[[], bool] | None = None
    depends_on: str | None = None
    grace: float = 30.0          # seconds after start before health counts
    pid_file: Path | None = None  # compatibility with existing CLI status commands


def _http_status(url: str, timeout: float = 4.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def ngrok_hosts() -> list[str]:
    """Public hostnames of ngrok tunnels that forward to our MCP port."""
    try:
        with urllib.request.urlopen(f"{NGROK_API}/api/tunnels", timeout=3) as r:
            tunnels = json.load(r).get("tunnels", [])
    except Exception:
        return []
    from urllib.parse import urlparse
    hosts = []
    for t in tunnels:
        addr = str(t.get("config", {}).get("addr", ""))
        host = urlparse(t.get("public_url", "")).hostname
        if host and addr.rstrip("/").endswith(f":{MCP_PORT}") and host not in hosts:
            hosts.append(host)
    return hosts


def _ngrok_command() -> list[str] | None:
    exe = shutil.which("ngrok") or (r"C:\ngrok\ngrok.exe" if Path(r"C:\ngrok\ngrok.exe").is_file() else None)
    if not exe:
        return None
    cmd = [exe, "http", str(MCP_PORT), "--log", "stdout"]
    if os.getenv("NGROK_URL"):
        cmd += ["--url", os.environ["NGROK_URL"]]
    return cmd


def _mcp_env() -> dict[str, str]:
    env = {"MCP_TRANSPORT": "http"}
    # An operator-pinned allowlist wins; otherwise follow ngrok's live hostname.
    if not os.getenv("MCP_ALLOWED_HOSTS"):
        hosts = ngrok_hosts()
        if hosts:
            env["MCP_ALLOWED_HOSTS"] = ",".join(hosts)
    return env


def default_services() -> dict[str, ServiceSpec]:
    return {
        "ngrok": ServiceSpec(
            name="ngrok",
            command=_ngrok_command,
            health=lambda: bool(ngrok_hosts()),
            grace=20,
        ),
        "mcp": ServiceSpec(
            name="mcp",
            command=lambda: [sys.executable, "-m", "agent.mcp_server"],
            env=_mcp_env,
            health=lambda: _http_status(f"http://127.0.0.1:{MCP_PORT}/mcp") == 401,
            depends_on="ngrok",
            grace=40,
        ),
        "daemon": ServiceSpec(
            name="daemon",
            command=lambda: [sys.executable, "-m", "agent.core.daemon"],
            pid_file=Path("data/daemon.pid"),
        ),
    }


# ---------------------------------------------------------------------------
# supervisor
# ---------------------------------------------------------------------------

@dataclass
class _Runtime:
    spec: ServiceSpec
    proc: subprocess.Popen | None = None
    state: str = "pending"
    reason: str = ""
    restarts: int = 0
    backoff: float = 0.0
    next_start: float = 0.0
    started_at: float | None = None
    started_wall: float | None = None
    last_exit: int | None = None
    health_fails: int = 0
    last_health: float = 0.0
    healthy: bool = False
    env_used: dict = field(default_factory=dict)
    log: object = None
    waiting_since: float | None = None


class Supervisor:
    def __init__(self, specs: list[ServiceSpec], *, base_dir: Path | None = None,
                 tick: float = 1.0, health_interval: float = 20.0, health_failures: int = 3,
                 backoff_base: float = 2.0, backoff_max: float = 300.0,
                 stable_after: float = 600.0, dep_wait: float = 30.0,
                 stop_timeout: float = 8.0):
        self.base = (base_dir or Path.cwd()).resolve()
        self.services = [_Runtime(s) for s in specs]
        self.tick = tick
        self.health_interval = health_interval
        self.health_failures = health_failures
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.stable_after = stable_after
        self.dep_wait = dep_wait
        self.stop_timeout = stop_timeout
        self.started_wall = time.time()

    # paths are resolved against base so tests can use a temp dir
    def _p(self, rel: Path) -> Path:
        return self.base / rel

    def _by_name(self, name: str) -> _Runtime | None:
        return next((r for r in self.services if r.spec.name == name), None)

    # -- lifecycle ---------------------------------------------------------

    def _open_log(self, name: str):
        log_dir = self._p(LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{name}.log"
        # Rotate only between runs: Windows cannot rename a file a child holds open.
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            for i in range(LOG_KEEP - 1, 0, -1):
                older = log_dir / f"{name}.log.{i}"
                if older.exists():
                    older.replace(log_dir / f"{name}.log.{i + 1}")
            path.replace(log_dir / f"{name}.log.1")
        f = open(path, "a", encoding="utf-8", errors="replace")
        f.write(f"\n===== supervisor: starting {name} at {datetime.now().isoformat(timespec='seconds')} =====\n")
        f.flush()
        return f

    def _start(self, rt: _Runtime) -> None:
        cmd = rt.spec.command()
        if not cmd:
            rt.state, rt.reason = "unavailable", "executable not found"
            rt.next_start = _now() + self.backoff_max
            return
        extra = rt.spec.env()
        env = {**os.environ, **extra}
        rt.log = self._open_log(rt.spec.name)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            rt.proc = subprocess.Popen(cmd, cwd=self.base, env=env, stdout=rt.log,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       creationflags=flags)
        except OSError as e:
            rt.log.close()
            rt.log = None
            self._schedule_restart(rt, "failed", f"could not start: {e}")
            return
        rt.env_used = extra
        rt.state, rt.reason = "running", ""
        rt.started_at, rt.started_wall = _now(), time.time()
        rt.health_fails, rt.last_health, rt.healthy = 0, _now(), False
        rt.waiting_since = None
        if rt.spec.pid_file:
            self._p(rt.spec.pid_file).write_text(str(rt.proc.pid))

    def _terminate(self, rt: _Runtime) -> None:
        if rt.proc and rt.proc.poll() is None:
            rt.proc.terminate()
            try:
                rt.proc.wait(self.stop_timeout)
            except subprocess.TimeoutExpired:
                rt.proc.kill()
                rt.proc.wait(5)
        if rt.log:
            rt.log.close()
            rt.log = None
        if rt.spec.pid_file:
            self._p(rt.spec.pid_file).unlink(missing_ok=True)
        rt.proc, rt.healthy = None, False

    def _schedule_restart(self, rt: _Runtime, state: str, reason: str) -> None:
        # A run that stayed up long enough counts as recovered: start over at
        # the base delay instead of carrying the old crash-loop backoff.
        if rt.started_at and _now() - rt.started_at >= self.stable_after:
            rt.backoff = 0.0
        rt.backoff = min(self.backoff_max, rt.backoff * 2 if rt.backoff else self.backoff_base)
        rt.next_start = _now() + rt.backoff
        rt.state, rt.reason = state, reason
        rt.restarts += 1

    def _restart_now(self, rt: _Runtime, state: str, reason: str) -> None:
        self._terminate(rt)
        self._schedule_restart(rt, state, reason)

    # -- one supervision pass ----------------------------------------------

    def step(self) -> None:
        now = _now()
        for rt in self.services:
            if rt.proc is not None:
                code = rt.proc.poll()
                if code is not None:
                    rt.last_exit = code
                    self._terminate(rt)
                    self._schedule_restart(rt, "crashed", f"exited with code {code}")
                    continue
                if not rt.spec.health:
                    rt.healthy = True
                    continue
                in_grace = now - rt.started_at < rt.spec.grace
                # Poll quickly until the first verdict so dependents can start
                # promptly; failures only count once the grace period is over.
                first_verdict_pending = not rt.healthy and rt.health_fails == 0
                interval = 2.0 if first_verdict_pending else self.health_interval
                if now - rt.last_health < interval:
                    continue
                rt.last_health = now
                if rt.spec.health():
                    rt.healthy, rt.health_fails, rt.reason = True, 0, ""
                    if self._config_drifted(rt):
                        self._terminate(rt)
                        # Not a failure: restart immediately, no backoff, not counted.
                        rt.state, rt.reason = "reconfiguring", "configuration changed; restarting"
                        rt.next_start = now
                elif not in_grace:
                    rt.healthy = False
                    rt.health_fails += 1
                    rt.reason = f"health check failed ({rt.health_fails}/{self.health_failures})"
                    if rt.health_fails >= self.health_failures:
                        self._restart_now(rt, "unhealthy", "stopped responding; restarted")
                continue

            if now < rt.next_start:
                continue
            dep = self._by_name(rt.spec.depends_on) if rt.spec.depends_on else None
            if dep and not dep.healthy and dep.state != "unavailable":
                rt.waiting_since = rt.waiting_since or now
                if now - rt.waiting_since < self.dep_wait:
                    rt.state, rt.reason = "waiting", f"waiting for {dep.spec.name}"
                    continue
            self._start(rt)

    def _config_drifted(self, rt: _Runtime) -> bool:
        """True if the env this service would get now differs from its start env.

        Only judged while its dependency is healthy: a dependency that is
        briefly down is not a new configuration, and restarting for it would
        restart this service twice per blip.
        """
        dep = self._by_name(rt.spec.depends_on) if rt.spec.depends_on else None
        if dep is not None and not dep.healthy:
            return False
        return rt.spec.env() != rt.env_used

    def write_state(self) -> None:
        state = {
            "pid": os.getpid(),
            "started": _iso(self.started_wall),
            "updated": _iso(time.time()),
            "services": {
                rt.spec.name: {
                    "state": rt.state,
                    "healthy": rt.healthy,
                    "pid": rt.proc.pid if rt.proc else None,
                    "since": _iso(rt.started_wall) if rt.proc else None,
                    "restarts": rt.restarts,
                    "last_exit": rt.last_exit,
                    "reason": rt.reason,
                    "next_retry_in": round(max(0.0, rt.next_start - _now()))
                                     if rt.proc is None and rt.state != "pending" else None,
                } for rt in self.services
            },
        }
        path = self._p(STATE_FILE)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        _replace_atomic(tmp, path)

    def stop_requested(self) -> bool:
        return self._p(STOP_FILE).exists()

    def shutdown(self) -> None:
        # Reverse order: MCP goes down before the tunnel that feeds it.
        for rt in reversed(self.services):
            self._terminate(rt)
            rt.state, rt.reason = "stopped", ""
        try:
            self.write_state()
        except Exception as exc:
            print(f"supervisor: final state write failed: {exc!r}")

    def run(self, max_seconds: float | None = None) -> None:
        self._p(STOP_FILE).unlink(missing_ok=True)
        deadline = _now() + max_seconds if max_seconds else None
        try:
            while not self.stop_requested():
                if deadline and _now() >= deadline:
                    break
                try:
                    self.step()
                    self.write_state()
                except Exception as exc:
                    # The one job of this loop is keeping ngrok/mcp/daemon up.
                    # A glitch in bookkeeping (or anything else unforeseen)
                    # must not take every supervised service down with it —
                    # log it and keep supervising.
                    print(f"supervisor: tick failed, continuing: {exc!r}")
                time.sleep(self.tick)
        finally:
            self.shutdown()
            self._p(STOP_FILE).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# single-instance helpers (used by the CLI)
# ---------------------------------------------------------------------------

def running_supervisor_pid(base: Path | None = None) -> int | None:
    """PID of a live supervisor for this project, else None (stale file removed)."""
    pid_file = (base or Path.cwd()) / PID_FILE
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        import psutil
        p = psutil.Process(pid)
        if p.is_running() and any("supervise" in part for part in p.cmdline()):
            return pid
    except Exception:
        pass
    pid_file.unlink(missing_ok=True)
    return None


def kill_orphans(base: Path | None = None) -> list[int]:
    """Kill children left behind by a supervisor that was killed hard.

    Only PIDs recorded in the last state file whose command line still looks
    like one of ours are touched, so a recycled PID is never killed.
    """
    path = (base or Path.cwd()) / STATE_FILE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    killed = []
    try:
        import psutil
    except ImportError:
        return []
    for name, svc in state.get("services", {}).items():
        pid = svc.get("pid")
        if not pid:
            continue
        try:
            p = psutil.Process(pid)
            cmd = " ".join(p.cmdline())
        except Exception:
            continue
        marker = {"mcp": "agent.mcp_server", "daemon": "agent.core.daemon", "ngrok": "ngrok"}.get(name)
        if marker and marker in cmd:
            p.kill()
            killed.append(pid)
    return killed
