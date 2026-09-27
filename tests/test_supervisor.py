"""Supervisor behaviour, with real child processes and a fake clock.

The children are tiny Python one-liners (crash / sleep / hang), so these
tests exercise real process start, exit detection and termination, while the
clock is simulated so backoff timing is exact and the suite stays fast.
"""
import json
import subprocess
import sys
import time

import pytest

from agent.core import supervisor as sv
from agent.core.supervisor import ServiceSpec, Supervisor

PY = sys.executable
CRASH = [PY, "-c", "import sys; sys.exit(3)"]
SLEEP = [PY, "-c", "import time; time.sleep(120)"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(sv, "_now", c)
    return c


@pytest.fixture
def base(tmp_path):
    (tmp_path / "data").mkdir()
    return tmp_path


def make(base, specs, **kw):
    kw.setdefault("health_interval", 10)
    kw.setdefault("health_failures", 3)
    kw.setdefault("backoff_base", 2)
    kw.setdefault("backoff_max", 300)
    kw.setdefault("stable_after", 600)
    kw.setdefault("dep_wait", 30)
    kw.setdefault("stop_timeout", 5)
    return Supervisor(specs, base_dir=base, **kw)


def wait_exit(rt, timeout=15):
    deadline = time.time() + timeout
    while rt.proc and rt.proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert rt.proc is None or rt.proc.poll() is not None, "child did not exit"


@pytest.fixture
def cleanup():
    sups = []
    yield sups.append
    for s in sups:
        s.shutdown()


# --- restart and backoff --------------------------------------------------

def test_crashed_service_is_restarted_with_exponential_backoff(base, clock, cleanup):
    sup = make(base, [ServiceSpec("svc", command=lambda: CRASH)])
    cleanup(sup)
    rt = sup.services[0]
    delays = []
    for _ in range(4):
        sup.step()                       # start it
        assert rt.proc is not None
        wait_exit(rt)
        sup.step()                       # notice the crash
        assert rt.state == "crashed" and rt.last_exit == 3
        delays.append(rt.next_start - clock())
        clock.advance(rt.next_start - clock())
    assert delays == [2, 4, 8, 16]
    assert rt.restarts == 4


def test_backoff_is_capped(base, clock, cleanup):
    sup = make(base, [ServiceSpec("svc", command=lambda: CRASH)], backoff_max=10)
    cleanup(sup)
    rt = sup.services[0]
    for _ in range(6):
        sup.step(); wait_exit(rt); sup.step()
        clock.advance(rt.next_start - clock())
    assert rt.backoff == 10


def test_backoff_resets_after_a_stable_run(base, clock, cleanup, tmp_path):
    flag = tmp_path / "die"
    script = [PY, "-c", f"import os,time\nwhile not os.path.exists({str(flag)!r}): time.sleep(0.05)"]
    sup = make(base, [ServiceSpec("svc", command=lambda: script)], stable_after=600)
    cleanup(sup)
    rt = sup.services[0]
    rt.backoff = 64                     # as if it had been crash-looping
    sup.step()
    clock.advance(601)                  # stays up past stable_after
    flag.write_text("x")
    wait_exit(rt)
    sup.step()
    assert rt.state == "crashed" and rt.backoff == 2


# --- health ---------------------------------------------------------------

def test_hung_service_is_restarted_after_consecutive_health_failures(base, clock, cleanup):
    healthy = {"v": True}
    spec = ServiceSpec("svc", command=lambda: SLEEP, health=lambda: healthy["v"], grace=5)
    sup = make(base, [spec], health_interval=10, health_failures=3)
    cleanup(sup)
    rt = sup.services[0]
    sup.step()
    first_pid = rt.proc.pid
    clock.advance(6); sup.step()
    assert rt.healthy

    healthy["v"] = False
    for n in (1, 2):
        clock.advance(10); sup.step()
        assert rt.proc is not None and rt.health_fails == n     # not yet
    clock.advance(10); sup.step()
    assert rt.proc is None and rt.state == "unhealthy"

    clock.advance(rt.next_start - clock()); healthy["v"] = True; sup.step()
    assert rt.proc is not None and rt.proc.pid != first_pid


def test_failures_during_grace_do_not_count(base, clock, cleanup):
    spec = ServiceSpec("svc", command=lambda: SLEEP, health=lambda: False, grace=30)
    sup = make(base, [spec], health_failures=1)
    cleanup(sup)
    rt = sup.services[0]
    sup.step()
    for _ in range(10):
        clock.advance(2.5); sup.step()
    assert rt.proc is not None and rt.health_fails == 0


# --- dependencies and reconfiguration --------------------------------------

def test_dependent_waits_for_dependency_health(base, clock, cleanup):
    dep_ok = {"v": False}
    sup = make(base, [
        ServiceSpec("tunnel", command=lambda: SLEEP, health=lambda: dep_ok["v"], grace=10),
        ServiceSpec("app", command=lambda: SLEEP, depends_on="tunnel"),
    ], dep_wait=30)
    cleanup(sup)
    tunnel, app = sup.services
    sup.step()
    assert tunnel.proc and app.proc is None and app.state == "waiting"
    dep_ok["v"] = True
    clock.advance(2); sup.step()
    assert tunnel.healthy
    sup.step()
    assert app.proc is not None


def test_dependent_starts_anyway_after_dep_wait(base, clock, cleanup):
    sup = make(base, [
        ServiceSpec("tunnel", command=lambda: SLEEP, health=lambda: False, grace=999),
        ServiceSpec("app", command=lambda: SLEEP, depends_on="tunnel"),
    ], dep_wait=30)
    cleanup(sup)
    app = sup.services[1]
    sup.step()
    clock.advance(29); sup.step()
    assert app.proc is None
    clock.advance(2); sup.step()
    assert app.proc is not None      # no tunnel (e.g. offline) must not block local use


def test_config_change_restarts_immediately_without_counting_a_failure(base, clock, cleanup):
    hosts = {"v": "a.ngrok-free.dev"}
    sup = make(base, [
        ServiceSpec("tunnel", command=lambda: SLEEP, health=lambda: True, grace=0),
        ServiceSpec("app", command=lambda: SLEEP, env=lambda: {"MCP_ALLOWED_HOSTS": hosts["v"]},
                    health=lambda: True, depends_on="tunnel", grace=0),
    ], health_interval=10)
    cleanup(sup)
    tunnel, app = sup.services
    sup.step(); clock.advance(2); sup.step(); sup.step()
    first = app.proc.pid
    assert app.env_used == {"MCP_ALLOWED_HOSTS": "a.ngrok-free.dev"}

    hosts["v"] = "b.ngrok-free.dev"
    clock.advance(10); sup.step()
    assert app.proc is None and app.state == "reconfiguring"
    sup.step()
    assert app.proc.pid != first and app.env_used["MCP_ALLOWED_HOSTS"] == "b.ngrok-free.dev"
    assert app.restarts == 0


def test_no_reconfigure_while_dependency_is_down(base, clock, cleanup):
    dep_ok, hosts = {"v": True}, {"v": "a"}
    sup = make(base, [
        ServiceSpec("tunnel", command=lambda: SLEEP, health=lambda: dep_ok["v"], grace=0),
        ServiceSpec("app", command=lambda: SLEEP, env=lambda: {"H": hosts["v"]},
                    health=lambda: True, depends_on="tunnel", grace=0),
    ], health_interval=10, health_failures=99)
    cleanup(sup)
    tunnel, app = sup.services
    sup.step(); clock.advance(2); sup.step(); sup.step()
    first = app.proc.pid
    dep_ok["v"], hosts["v"] = False, ""       # tunnel blips: env looks empty
    clock.advance(10); sup.step()
    assert app.proc is not None and app.proc.pid == first


# --- misc -----------------------------------------------------------------

def test_unavailable_executable_is_reported_not_crashlooped(base, clock, cleanup):
    sup = make(base, [ServiceSpec("ngrok", command=lambda: None)])
    cleanup(sup)
    sup.step()
    rt = sup.services[0]
    assert rt.state == "unavailable" and rt.proc is None


def test_state_file_and_logs(base, clock, cleanup):
    script = [PY, "-c", "print('hello from child', flush=True); import time; time.sleep(120)"]
    sup = make(base, [ServiceSpec("svc", command=lambda: script)])
    cleanup(sup)
    sup.step(); sup.write_state()
    state = json.loads((base / "data" / "supervisor.json").read_text(encoding="utf-8"))
    assert state["services"]["svc"]["state"] == "running"
    assert state["services"]["svc"]["pid"] == sup.services[0].proc.pid
    deadline = time.time() + 10
    log = base / "data" / "logs" / "svc.log"
    while "hello from child" not in log.read_text(encoding="utf-8") and time.time() < deadline:
        time.sleep(0.1)
    assert "hello from child" in log.read_text(encoding="utf-8")


def test_pid_file_written_and_removed(base, clock):
    pid_file = base / "data" / "daemon.pid"
    sup = make(base, [ServiceSpec("daemon", command=lambda: SLEEP, pid_file=sv.Path("data/daemon.pid"))])
    sup.step()
    assert int(pid_file.read_text()) == sup.services[0].proc.pid
    sup.shutdown()
    assert not pid_file.exists()


def test_stop_file_shuts_down_children(base, cleanup):
    sup = make(base, [ServiceSpec("svc", command=lambda: SLEEP)], stop_timeout=5)
    sup.tick = 0.05
    import threading
    t = threading.Thread(target=sup.run, kwargs={"max_seconds": 30})
    t.start()
    deadline = time.time() + 10
    while not sup.services[0].proc and time.time() < deadline:
        time.sleep(0.05)
    child = sup.services[0].proc
    assert child is not None
    (base / "data" / "supervisor.stop").write_text("")
    t.join(15)
    assert not t.is_alive()
    assert child.poll() is not None
    state = json.loads((base / "data" / "supervisor.json").read_text(encoding="utf-8"))
    assert state["services"]["svc"]["state"] == "stopped"
    assert not (base / "data" / "supervisor.stop").exists()


def test_kill_orphans_only_kills_recorded_processes_that_look_like_ours(base):
    ours = subprocess.Popen([PY, "-c", "import time  # agent.mcp_server\ntime.sleep(120)"])
    stranger = subprocess.Popen(SLEEP)
    try:
        (base / "data" / "supervisor.json").write_text(json.dumps({"services": {
            "mcp": {"pid": ours.pid},
            "daemon": {"pid": stranger.pid},    # recorded, but not a daemon cmdline
        }}), encoding="utf-8")
        assert sv.kill_orphans(base) == [ours.pid]
        ours.wait(10)
        assert stranger.poll() is None
    finally:
        for p in (ours, stranger):
            if p.poll() is None:
                p.kill()


def test_running_supervisor_pid_clears_stale_file(base):
    pid_file = base / "data" / "supervisor.pid"
    pid_file.write_text("999999")
    assert sv.running_supervisor_pid(base) is None
    assert not pid_file.exists()


def test_ngrok_hosts_only_counts_tunnels_to_our_port(monkeypatch):
    import io

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    payload = {"tunnels": [
        {"public_url": "https://mine.ngrok-free.dev", "config": {"addr": "http://localhost:8000"}},
        {"public_url": "https://other.ngrok-free.dev", "config": {"addr": "http://localhost:3000"}},
    ]}
    monkeypatch.setattr(sv, "MCP_PORT", 8000)
    monkeypatch.setattr(sv.urllib.request, "urlopen", lambda *a, **k: R(json.dumps(payload).encode()))
    assert sv.ngrok_hosts() == ["mine.ngrok-free.dev"]
