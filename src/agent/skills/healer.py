"""
Autonomous healing skill — called by the daemon.

Each handler follows: diagnose -> decide -> act -> verify -> report.
Every handler is defensive: it never raises, always logs to daemon_db,
and respects cooldowns / fix-count escalation thresholds.
"""
from __future__ import annotations

import json
import re
import time

from agent.integrations import daemon_db
from agent.integrations.kubectl import run_kubectl
from agent.integrations.slack import send_alert_generic
from agent.observability.logging import get_logger

log = get_logger(__name__)

# How many times we auto-fix a single resource in a day before escalating.
_MAX_FIXES_BEFORE_ESCALATE = 3

# Commands we refuse to run automatically (destructive / forceful).
_UNSAFE_TOKENS = ("delete", "--force", "drain", "cordon", "uncordon", "wipe", "destroy")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def deployment_from_pod(pod_name: str) -> str:
    """
    Strip the ReplicaSet hash + pod suffix from a pod name.

    "api-server-7d8f9b-xkp2q" -> "api-server"
    """
    parts = pod_name.rsplit("-", 2)
    return "-".join(parts[:-2]) if len(parts) > 2 else pod_name


def parse_mem(s: str) -> int:
    """Parse a kubernetes memory quantity into MB. Returns 0 on failure."""
    if not s:
        return 0
    s = s.strip()
    try:
        if s.endswith("Mi"):
            return int(float(s[:-2]))
        if s.endswith("Gi"):
            return int(float(s[:-2]) * 1024)
        if s.endswith("Ki"):
            return int(float(s[:-2]) / 1024)
        if s.endswith("M"):
            return int(float(s[:-1]))
        if s.endswith("G"):
            return int(float(s[:-1]) * 1024)
        return int(float(s)) // (1024 * 1024)  # raw bytes
    except (ValueError, TypeError):
        return 0


def parse_cpu_millicores(s: str) -> int:
    """Parse a kubernetes CPU quantity into millicores. Returns 0 on failure."""
    if not s:
        return 0
    s = s.strip()
    try:
        if s.endswith("m"):
            return int(float(s[:-1]))
        return int(float(s) * 1000)
    except (ValueError, TypeError):
        return 0


def _is_safe_command(cmd: str) -> bool:
    low = cmd.lower()
    return not any(tok in low for tok in _UNSAFE_TOKENS)


def _notify(action: str, message: str, pod: str, namespace: str,
            severity: str = "info", fix_command: str | None = None) -> None:
    try:
        send_alert_generic(
            title=f"Auto-fixed: {action}",
            message=message,
            severity=severity,
            fields={"Pod": pod, "Namespace": namespace, "Action": action},
            fix_command=fix_command,
        )
    except Exception as exc:  # never let Slack break healing
        log.warning("healer.slack_failed", error=str(exc))


# ---------------------------------------------------------------------------
# CrashLoopBackOff
# ---------------------------------------------------------------------------

def _handle_crashloop(
    pod_name: str,
    namespace: str,
    container_name: str,
    restart_count: int = 0,
) -> dict:
    """
    Auto-fix CrashLoopBackOff:
    1. Always try rolling restart first (safe, fixes most transient crashes).
    2. If Claude finds a specific fix, apply it too.
    3. After 3 failed attempts, escalate to Slack and stop retrying.
    """
    key = f"{namespace}/{pod_name}/crashloop"
    result = {"action": "heal_crashloop", "detected": True, "skipped": False,
              "escalated": False, "fixed": False}

    if daemon_db.check_cooldown(key, cooldown_minutes=30):
        log.info("healer.crashloop.cooldown", pod=pod_name, ns=namespace)
        result["skipped"] = True
        return result

    fix_count = daemon_db.get_fix_count(key)

    # Open (or dedup into) an incident for this crash.
    inc_id = ""
    try:
        from agent.skills.incident import open_incident as _open_incident
        inc_id = _open_incident(
            title=f"CrashLoopBackOff: {pod_name}",
            severity="critical" if restart_count > 10 else "warning",
            service=deployment_from_pod(pod_name),
            namespace=namespace,
            cause=f"Pod restarted {restart_count} times",
        )
        result["incident_id"] = inc_id
    except Exception as exc:  # incident tracking must never break healing
        log.warning("healer.incident_open_failed", error=str(exc))

    if fix_count >= _MAX_FIXES_BEFORE_ESCALATE:
        _notify(
            "CrashLoop escalation",
            f"Pod `{pod_name}` has been auto-fixed {fix_count} times and is "
            f"still crashing. Needs human review.",
            pod_name, namespace, severity="critical",
        )
        daemon_db.log_action(
            "pod_heal", "escalate_crashloop", pod_name, namespace,
            success=True, auto_fixed=False,
            note=f"Escalated after {fix_count} failed attempts",
        )
        if inc_id:
            try:
                from agent.skills.incident import escalate_incident as _escalate_incident
                _escalate_incident(inc_id, "exceeded max fix attempts")
            except Exception as exc:
                log.warning("healer.incident_escalate_failed", error=str(exc))
        # Page on-call (PagerDuty or OpsGenie) after 3 failed fixes
        try:
            from agent.integrations.pagerduty import page_oncall
            page_oncall(
                title=f"CrashLoop escalation: {pod_name}",
                body=(
                    f"Pod `{pod_name}` in `{namespace}` has crashed {fix_count} times "
                    f"and auto-fixes have not resolved it. Immediate human review required."
                ),
                severity="critical",
                service=deployment_from_pod(pod_name),
                incident_id=inc_id or "",
            )
        except Exception as exc:
            log.warning("healer.page_oncall_failed", error=str(exc))
        result["escalated"] = True
        result["action"] = "slack_alert"
        return result

    deployment = deployment_from_pod(pod_name)

    # ── Step 1: Always do a rolling restart (safe, zero-downtime) ──────────
    restart_result = run_kubectl([
        "rollout", "restart", f"deployment/{deployment}", "-n", namespace,
    ])
    log.info("healer.crashloop.restart",
             pod=pod_name, ns=namespace, ok=restart_result.success)

    # ── Step 2: Get logs + exit code ──────────────────────────────────────
    logs      = _get_previous_logs(pod_name, namespace, container_name)
    exit_code = _get_exit_code(pod_name, namespace)
    command   = _get_container_command(deployment, namespace)

    # ── Step 3: Detect command-level crash (exit 1, wrong command, etc.) ──
    specific_applied = False
    cause = "Unknown"
    fix_cmd = "ESCALATE"

    if _is_command_crash(exit_code, command):
        log.info("healer.command_crash_detected",
                 pod=pod_name, exit_code=exit_code, command=command)
        cause, new_cmd = _diagnose_command_crash(deployment, namespace, command, logs)
        if new_cmd:
            patched = _patch_deployment_command(deployment, namespace, new_cmd)
            specific_applied = patched
            fix_cmd = f"patch command → {new_cmd}"
            log.info("healer.command_patch_applied",
                     deployment=deployment, new_command=new_cmd, ok=patched)
    else:
        # Ask Claude for a more specific fix
        cause, fix_cmd = _diagnose_crashloop(pod_name, logs)
        if fix_cmd.upper() != "ESCALATE" and _is_safe_command(fix_cmd):
            specific_applied = run_kubectl(fix_cmd.split()).success

    # ── Step 4: Wait and verify ────────────────────────────────────────────
    recovered = _verify_running(pod_name, namespace, wait_s=60)

    daemon_db.set_cooldown(key)
    daemon_db.log_action(
        "pod_heal",
        f"crashloop: rolling restart + {fix_cmd if specific_applied else 'no extra fix'}",
        pod_name, namespace,
        before_state={"restart_count": restart_count},
        after_state={"recovered": recovered, "cause": cause},
        success=bool(restart_result.success),
        note=cause,
    )

    if inc_id:
        try:
            from agent.skills.incident import add_fix_attempt as _add_fix_attempt
            _add_fix_attempt(inc_id, f"rolling restart + {fix_cmd}", recovered)
            if recovered:
                from agent.skills.incident import resolve_incident as _resolve_incident
                _resolve_incident(inc_id)
        except Exception as exc:
            log.warning("healer.incident_update_failed", error=str(exc))

    if recovered:
        _notify(
            "CrashLoopBackOff fixed",
            f"✅ Auto-fixed `{pod_name}` in `{namespace}`\n"
            f"Cause: {cause}\n"
            f"Fix: rolling restart"
            + (f" + {fix_cmd}" if specific_applied else ""),
            pod_name, namespace, severity="info",
        )
    else:
        _notify(
            "CrashLoopBackOff — fix attempted",
            f"⚠️ Attempted fix on `{pod_name}` in `{namespace}` "
            f"(attempt {fix_count + 1}/{_MAX_FIXES_BEFORE_ESCALATE})\n"
            f"Cause: {cause}\nStill not recovered — will retry in 30 min.",
            pod_name, namespace, severity="warning",
        )

    result["fixed"] = bool(restart_result.success)
    result["recovered"] = recovered
    result["cause"] = cause
    return result


def _get_previous_logs(pod: str, ns: str, container: str) -> str:
    res = run_kubectl(
        ["logs", pod, "-n", ns, "-c", container, "--tail=100", "--previous"]
    )
    return res.output if res.success else (res.error or "")


def _get_exit_code(pod_name: str, namespace: str) -> int:
    """Return last container exit code, or -1 on failure."""
    res = run_kubectl([
        "get", "pod", pod_name, "-n", namespace, "-o",
        "jsonpath={.status.containerStatuses[0].lastState.terminated.exitCode}",
    ])
    try:
        return int(res.output.strip()) if res.success and res.output.strip() else -1
    except ValueError:
        return -1


def _get_container_command(deployment: str, namespace: str) -> list[str]:
    """Return the container command from the deployment spec."""
    res = run_kubectl([
        "get", "deployment", deployment, "-n", namespace, "-o",
        "jsonpath={.spec.template.spec.containers[0].command}",
    ])
    if not res.success or not res.output.strip():
        return []
    try:
        import json as _json
        return _json.loads(res.output.strip())
    except Exception:
        return []


def _is_command_crash(exit_code: int, command: list[str]) -> bool:
    """Return True when the container command itself is causing the crash."""
    if exit_code not in (1, 2, 127, 128):
        return False
    cmd_str = " ".join(command).lower()
    # Explicit crash patterns
    crash_patterns = ["exit 1", "exit 2", "false", "return 1", "/dev/null"]
    return any(p in cmd_str for p in crash_patterns) or (
        # Short command that always exits immediately
        "sleep" not in cmd_str and "tail" not in cmd_str
        and "server" not in cmd_str and "serve" not in cmd_str
        and len(command) > 0 and exit_code == 1
    )


def _patch_deployment_command(
    deployment: str,
    namespace: str,
    new_command: list[str],
) -> bool:
    """Patch a deployment's container command."""
    import json as _json
    patch = _json.dumps({
        "spec": {"template": {"spec": {"containers": [{"name": deployment, "command": new_command}]}}}
    })
    res = run_kubectl([
        "patch", "deployment", deployment, "-n", namespace,
        "--type=strategic", "-p", patch,
    ])
    return res.success


def _diagnose_command_crash(
    deployment: str,
    namespace: str,
    command: list[str],
    logs: str,
) -> tuple[str, list[str]]:
    """Ask Claude to suggest a fixed container command. Returns (cause, new_command)."""
    from agent.core import llm
    from agent.core.async_utils import run_sync

    system = (
        "You are an SRE fixing a Kubernetes deployment whose container command always exits immediately.\n"
        "Respond with exactly two lines:\n"
        "CAUSE: <one-line root cause>\n"
        "COMMAND: <JSON array for the new working container command, e.g. [\"/bin/sh\",\"-c\",\"sleep 3600\"]>\n"
        "If you cannot determine a safe fix, COMMAND line must be: ESCALATE\n"
        "Never suggest deleting the deployment."
    )
    user = (
        f"Deployment: {deployment} (namespace: {namespace})\n"
        f"Current command: {command}\n"
        f"Container logs:\n{logs[:3000]}"
    )
    try:
        resp = run_sync(llm.chat(
            messages=[{"role": "user", "content": user}],
            system=system,
            max_tokens=400,
        ))
        return _parse_command_diagnosis(resp.content)
    except Exception as exc:
        log.warning("healer.command_diagnose_failed", error=str(exc))
        return ("Diagnosis unavailable", [])


def _parse_command_diagnosis(text: str) -> tuple[str, list[str]]:
    import json as _json
    cause = "Unknown"
    new_cmd: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"(?i)^CAUSE:\s*(.+)$", line)
        if m:
            cause = m.group(1).strip()
        m = re.match(r"(?i)^COMMAND:\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            if val.upper() == "ESCALATE":
                new_cmd = []
            else:
                try:
                    new_cmd = _json.loads(val)
                except Exception:
                    new_cmd = []
    return cause, new_cmd


def _diagnose_crashloop(pod_name: str, logs: str) -> tuple[str, str]:
    """Ask Claude (fast model) for CAUSE + FIX. Returns (cause, fix_command)."""
    from agent.core import llm
    from agent.core.async_utils import run_sync

    system = (
        "You are an SRE diagnosing a CrashLoopBackOff pod. "
        "Reply with exactly two lines:\n"
        "CAUSE: <one line root cause>\n"
        "FIX: <one safe kubectl command, or the word ESCALATE>\n"
        "Never suggest 'kubectl delete' or '--force'. If no safe automated "
        "fix exists, the FIX line must be exactly: ESCALATE"
    )
    user = f"Pod {pod_name} is CrashLoopBackOff. Previous container logs:\n{logs[:4000]}"

    try:
        resp = run_sync(llm.chat(
            messages=[{"role": "user", "content": user}],
            system=system,
            max_tokens=300,
        ))
        return _parse_diagnosis(resp.content)
    except Exception as exc:
        log.warning("healer.diagnose_failed", pod=pod_name, error=str(exc))
        return ("Diagnosis unavailable", "ESCALATE")


def _parse_diagnosis(text: str) -> tuple[str, str]:
    cause = "Unknown"
    fix = "ESCALATE"
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"(?i)^CAUSE:\s*(.+)$", line)
        if m:
            cause = m.group(1).strip()
        m = re.match(r"(?i)^FIX:\s*(.+)$", line)
        if m:
            fix = m.group(1).strip().strip("`")
    return cause, fix


# ---------------------------------------------------------------------------
# OOMKilled
# ---------------------------------------------------------------------------

def _handle_oom(
    pod_name: str,
    namespace: str,
    container_name: str,
    current_limit_mb: int = 0,
) -> dict:
    """Bump the memory limit of an OOMKilled container's deployment."""
    key = f"{namespace}/{pod_name}/oom"
    result = {"action": "heal_oom", "detected": True, "skipped": False,
              "escalated": False, "limit_increased": False}

    if daemon_db.check_cooldown(key, cooldown_minutes=45):
        log.info("healer.oom.cooldown", pod=pod_name, ns=namespace)
        result["skipped"] = True
        return result

    inc_id = ""
    try:
        from agent.skills.incident import open_incident as _open_incident
        inc_id = _open_incident(
            title=f"OOMKilled: {pod_name}",
            severity="warning",
            service=deployment_from_pod(pod_name),
            namespace=namespace,
            cause="Container OOMKilled (out of memory)",
        )
        result["incident_id"] = inc_id
    except Exception as exc:
        log.warning("healer.incident_open_failed", error=str(exc))

    if daemon_db.get_fix_count(key) >= _MAX_FIXES_BEFORE_ESCALATE:
        _notify(
            "OOM escalation",
            f"Pod `{pod_name}` has been OOMKilled and bumped "
            f"{_MAX_FIXES_BEFORE_ESCALATE}+ times today — likely a memory leak.",
            pod_name, namespace, severity="critical",
        )
        daemon_db.log_action(
            "pod_heal", "escalate_oom", pod_name, namespace,
            success=True, auto_fixed=False,
            note="Bumped too many times — likely leak, escalated",
        )
        if inc_id:
            try:
                from agent.skills.incident import escalate_incident as _escalate_incident
                _escalate_incident(inc_id, "exceeded max fix attempts")
            except Exception as exc:
                log.warning("healer.incident_escalate_failed", error=str(exc))
        result["escalated"] = True
        result["action"] = "slack_alert"
        return result

    if current_limit_mb <= 0:
        current_limit_mb = _get_mem_limit_mb(pod_name, namespace, container_name)

    original = current_limit_mb if current_limit_mb > 0 else 256
    if current_limit_mb <= 0:
        # No limit set — start at observed usage + 50% (approx via 256 default).
        new_limit = int(original * 1.5)
    else:
        new_limit = int(current_limit_mb * 1.25)
    # Cap at 4x the original limit.
    new_limit = min(new_limit, original * 4)

    deployment = deployment_from_pod(pod_name)
    patch = json.dumps({
        "spec": {"template": {"spec": {"containers": [
            {"name": container_name,
             "resources": {"limits": {"memory": f"{new_limit}Mi"}}}
        ]}}}
    })
    applied = run_kubectl(
        ["patch", "deployment", deployment, "-n", namespace, "-p", patch]
    )
    recovered = _verify_running(pod_name, namespace, wait_s=90)

    daemon_db.set_cooldown(key)
    daemon_db.log_action(
        "pod_heal", f"OOMKilled → mem limit {original}→{new_limit}Mi",
        pod_name, namespace,
        before_state={"memory_mb": original},
        after_state={"memory_mb": new_limit, "recovered": recovered},
        success=bool(applied.success),
        note="memory limit increased",
    )
    _notify(
        "OOMKilled",
        f"Auto-fixed OOMKilled on `{pod_name}`: memory limit "
        f"{original}Mi → {new_limit}Mi",
        pod_name, namespace, severity="info",
    )
    if inc_id:
        try:
            from agent.skills.incident import add_fix_attempt as _add_fix_attempt
            _add_fix_attempt(
                inc_id,
                f"bumped memory limit {original}Mi → {new_limit}Mi",
                recovered,
            )
            if recovered:
                from agent.skills.incident import resolve_incident as _resolve_incident
                _resolve_incident(inc_id)
        except Exception as exc:
            log.warning("healer.incident_update_failed", error=str(exc))
    result["limit_increased"] = bool(applied.success)
    result["old_limit_mb"] = original
    result["new_limit_mb"] = new_limit
    return result


def _get_mem_limit_mb(pod: str, ns: str, container: str) -> int:
    res = run_kubectl([
        "get", "pod", pod, "-n", ns,
        "-o", "jsonpath={.spec.containers[*].resources.limits.memory}",
    ])
    if res.success and res.output.strip():
        return parse_mem(res.output.strip().split()[0])
    return 0


# ---------------------------------------------------------------------------
# High CPU → autoscale
# ---------------------------------------------------------------------------

def _handle_high_cpu(
    pod_name: str,
    namespace: str,
    cpu_pct: float,
    consecutive_count: int = 0,
) -> dict:
    """Scale up the deployment by 1 replica after sustained high CPU."""
    result = {"action": "autoscale", "scaled": False,
              "replicas_increased": False, "skipped": False}

    if consecutive_count < 3:
        result["skipped"] = True
        return result

    key = f"{namespace}/{pod_name}/cpu"
    if daemon_db.check_cooldown(key, cooldown_minutes=5):
        result["skipped"] = True
        return result

    deployment = deployment_from_pod(pod_name)
    current = _get_replicas(deployment, namespace)
    if current >= 10:
        _notify(
            "CPU at scale ceiling",
            f"`{deployment}` is at {current} replicas (max 10) but CPU is "
            f"{cpu_pct:.0f}%. Manual review needed.",
            pod_name, namespace, severity="warning",
        )
        result["skipped"] = True
        return result

    new_count = current + 1
    applied = run_kubectl([
        "scale", f"deployment/{deployment}", "-n", namespace,
        f"--replicas={new_count}",
    ])

    daemon_db.set_cooldown(key)
    daemon_db.log_action(
        "scale", f"CPU {cpu_pct:.0f}% → scaled {deployment} {current}→{new_count}",
        deployment, namespace,
        before_state={"replicas": current, "cpu_pct": cpu_pct},
        after_state={"replicas": new_count},
        success=bool(applied.success),
        note="auto-scaled on sustained high CPU",
    )
    _notify(
        f"scaled {deployment}",
        f"Auto-scaled `{deployment}`: {current} → {new_count} replicas "
        f"(CPU was {cpu_pct:.0f}%)",
        pod_name, namespace, severity="info",
    )
    # CPU alone does not open an incident — only annotate an existing one.
    try:
        from agent.integrations import incident_db
        existing = incident_db.get_open_incident(deployment, namespace)
        if existing:
            from agent.skills.incident import add_fix_attempt as _add_fix_attempt
            _add_fix_attempt(
                existing["id"],
                f"scaled {deployment} {current} → {new_count} replicas (CPU {cpu_pct:.0f}%)",
                bool(applied.success),
            )
    except Exception as exc:
        log.warning("healer.incident_update_failed", error=str(exc))
    result["scaled"] = bool(applied.success)
    result["replicas_increased"] = new_count > current
    result["old_replicas"] = current
    result["new_replicas"] = new_count
    return result


def _get_replicas(deployment: str, ns: str) -> int:
    res = run_kubectl([
        "get", "deployment", deployment, "-n", ns,
        "-o", "jsonpath={.spec.replicas}",
    ])
    if res.success and res.output.strip():
        try:
            return int(res.output.strip())
        except ValueError:
            return 1
    return 1


# ---------------------------------------------------------------------------
# High memory → alert only
# ---------------------------------------------------------------------------

def _handle_high_memory(pod_name: str, namespace: str, mem_pct: float) -> dict:
    """Alert only — never auto-scale on memory (could be a leak)."""
    _notify(
        "high memory",
        f"`{pod_name}` memory at {mem_pct:.0f}% — run: "
        f"agent k8s diagnose {pod_name} -n {namespace}",
        pod_name, namespace, severity="warning",
    )
    daemon_db.log_action(
        "pod_heal", f"high memory alert ({mem_pct:.0f}%)", pod_name, namespace,
        success=True, auto_fixed=False, note="alert only",
    )
    return {"action": "slack_alert", "alerted": True}


# ---------------------------------------------------------------------------
# Bad deploy → rollback
# ---------------------------------------------------------------------------

def _handle_bad_deploy(deployment: str, namespace: str,
                       restart_count: int = 0) -> dict:
    """Roll back a deployment whose pods are crash-looping after a recent deploy."""
    result = {"action": "rollback", "rolled_back": False, "skipped": False}

    if not _is_recent_deploy(deployment, namespace):
        result["skipped"] = True
        return result

    if restart_count <= 3:
        result["skipped"] = True
        return result

    inc_id = ""
    try:
        from agent.skills.incident import open_incident as _open_incident
        inc_id = _open_incident(
            title=f"Bad deploy: {deployment}",
            severity="critical",
            service=deployment,
            namespace=namespace,
            cause=f"Pods crash-looping after deploy (restarts: {restart_count})",
        )
        result["incident_id"] = inc_id
    except Exception as exc:
        log.warning("healer.incident_open_failed", error=str(exc))

    applied = run_kubectl([
        "rollout", "undo", f"deployment/{deployment}", "-n", namespace,
    ])

    _mark_rolled_back(deployment, namespace)
    daemon_db.log_action(
        "rollback", f"auto-rollback (pods restarting {restart_count}x)",
        deployment, namespace,
        before_state={"restart_count": restart_count},
        success=bool(applied.success),
        note="bad deploy rolled back",
    )
    _notify(
        f"rolled back {deployment}",
        f"Auto-rolled back `{deployment}` — pods were crash-looping after deploy "
        f"(restarts: {restart_count})",
        deployment, namespace, severity="warning",
    )
    if inc_id:
        try:
            from agent.skills.incident import add_fix_attempt as _add_fix_attempt
            _add_fix_attempt(inc_id, "rollout undo (rollback)", bool(applied.success))
            if applied.success:
                from agent.skills.incident import resolve_incident as _resolve_incident
                _resolve_incident(inc_id, cause="rolled back bad deploy")
        except Exception as exc:
            log.warning("healer.incident_update_failed", error=str(exc))
    result["rolled_back"] = bool(applied.success)
    return result


def _is_recent_deploy(deployment: str, namespace: str) -> bool:
    """True if deploy_db has a report for this deployment within the last 2h."""
    try:
        from datetime import datetime, timedelta, timezone

        from agent.integrations import deploy_db
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        for r in deploy_db.list_reports(limit=50):
            if r.deployment != deployment or r.namespace != namespace:
                continue
            try:
                ts = datetime.fromisoformat(r.timestamp)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if ts >= cutoff:
                return True
    except Exception as exc:
        log.warning("healer.recent_deploy_check_failed", error=str(exc))
    return False


def _mark_rolled_back(deployment: str, namespace: str) -> None:
    try:
        from agent.integrations import deploy_db
        for r in deploy_db.list_reports(limit=50):
            if r.deployment == deployment and r.namespace == namespace:
                if hasattr(deploy_db, "update_pending_status"):
                    pass  # reports are immutable; pending status updated elsewhere
                break
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ImagePullBackOff → alert only
# ---------------------------------------------------------------------------

def _handle_imagepull(pod_name: str, namespace: str, image: str) -> dict:
    """Alert only — image/credential issues need a human."""
    _notify(
        "ImagePullBackOff",
        f"ImagePullBackOff on `{pod_name}`: image `{image}` — check "
        f"credentials or image tag.",
        pod_name, namespace, severity="critical",
    )
    daemon_db.log_action(
        "pod_heal", f"ImagePullBackOff alert ({image})", pod_name, namespace,
        success=True, auto_fixed=False, note="alert only — needs human",
    )
    return {"action": "slack_alert", "alerted": True}


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _verify_running(pod_name: str, namespace: str, wait_s: int = 60) -> bool:
    """Wait, then check whether a pod (or its replacement) is Running."""
    time.sleep(wait_s)
    res = run_kubectl([
        "get", "pod", pod_name, "-n", namespace,
        "-o", "jsonpath={.status.phase}",
    ])
    if res.success and res.output.strip() == "Running":
        return True
    # Pod may have been replaced by a new ReplicaSet pod — check the deployment.
    deployment = deployment_from_pod(pod_name)
    dep = run_kubectl([
        "get", "deployment", deployment, "-n", namespace,
        "-o", "jsonpath={.status.unavailableReplicas}",
    ])
    if dep.success and dep.output.strip() in ("", "0"):
        return True
    return False
