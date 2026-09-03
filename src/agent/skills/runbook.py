"""Runbook automation engine — executes YAML-defined playbooks."""
from __future__ import annotations

import uuid
from pathlib import Path

from agent.observability.logging import get_logger

log = get_logger(__name__)

_RUNBOOKS_PATH = Path("data/runbooks.yaml")


def _load_runbooks() -> list[dict]:
    try:
        import yaml  # type: ignore[import]
        if not _RUNBOOKS_PATH.exists():
            return []
        data = yaml.safe_load(_RUNBOOKS_PATH.read_text(encoding="utf-8"))
        return data.get("runbooks", []) if isinstance(data, dict) else []
    except Exception as exc:
        log.warning("runbook.load_failed", error=str(exc))
        return []


def get_runbook(runbook_id: str) -> dict | None:
    for rb in _load_runbooks():
        if rb.get("id") == runbook_id:
            return rb
    return None


def list_runbooks() -> list[dict]:
    return [
        {
            "id": rb.get("id", ""),
            "name": rb.get("name", ""),
            "description": rb.get("description", ""),
            "trigger_condition": rb.get("trigger_condition", ""),
            "step_count": len(rb.get("steps", [])),
        }
        for rb in _load_runbooks()
    ]


def _resolve_template(text: str, context: dict) -> str:
    for k, v in context.items():
        text = text.replace(f"{{{k}}}", str(v))
    return text


def _exec_kubectl_step(step: dict, context: dict) -> tuple[bool, str]:
    from agent.integrations.kubectl import run_kubectl
    cmd = _resolve_template(step.get("command", ""), context)
    tokens = cmd.split()
    res = run_kubectl(tokens)
    out = res.output or res.error or ""
    return res.success, out


def _exec_python_step(step: dict, context: dict) -> tuple[bool, str]:
    fn_name = step.get("function", "")
    raw_args = step.get("args") or {}
    args = {k: _resolve_template(str(v), context) for k, v in raw_args.items()}

    if fn_name == "expand_pvc":
        return _expand_pvc(**args)
    elif fn_name == "wait_deployment_ready":
        return _wait_deployment_ready(**args)
    else:
        return False, f"unknown function: {fn_name}"


def _exec_slack_step(step: dict, context: dict) -> tuple[bool, str]:
    try:
        from agent.integrations.slack import send_alert_generic
        msg = _resolve_template(step.get("message", ""), context)
        severity = step.get("severity", "info")
        title = context.get("runbook_name", "Runbook")
        send_alert_generic(title=title, message=msg, severity=severity, fields={})
        return True, "sent"
    except Exception as exc:
        return False, str(exc)


def _expand_pvc(
    pvc_name: str,
    namespace: str,
    add_gb: str = "10",
) -> tuple[bool, str]:
    from agent.integrations.kubectl import run_kubectl
    res = run_kubectl([
        "get", "pvc", pvc_name, "-n", namespace,
        "-o", "jsonpath={.spec.resources.requests.storage}",
    ])
    current_str = (res.output or "10Gi").replace("Gi", "").replace("G", "").strip()
    try:
        current = int(current_str)
    except ValueError:
        current = 10
    new_size = current + int(add_gb)
    patch = f'{{"spec":{{"resources":{{"requests":{{"storage":"{new_size}Gi"}}}}}}}}'
    res2 = run_kubectl([
        "patch", "pvc", pvc_name, "-n", namespace,
        "--type=merge", "-p", patch,
    ])
    return res2.success, f"PVC expanded from {current}Gi to {new_size}Gi"


def _wait_deployment_ready(
    deployment: str,
    namespace: str,
    timeout_s: str = "120",
) -> tuple[bool, str]:
    from agent.integrations.kubectl import run_kubectl
    res = run_kubectl([
        "rollout", "status", f"deployment/{deployment}",
        "-n", namespace, f"--timeout={timeout_s}s",
    ])
    return res.success, res.output or ""


def run_runbook(
    runbook_id: str,
    context: dict | None = None,
    trigger: str = "manual",
) -> dict:
    """Execute a runbook by id. Returns run result dict."""
    from agent.integrations import runbook_db

    context = dict(context or {})
    runbook = get_runbook(runbook_id)
    if not runbook:
        return {"success": False, "error": f"runbook {runbook_id!r} not found"}

    context["runbook_name"] = runbook["name"]
    steps = runbook.get("steps", [])
    run_id = runbook_db.start_run(runbook_id, trigger, len(steps), context)

    results = []
    overall_status = "success"

    for step in steps:
        step_name = step.get("name", "")
        step_type = step.get("type", "")
        on_failure = step.get("on_failure", "escalate")

        cmd_label = (
            step.get("command")
            or step.get("function")
            or step.get("message")
            or ""
        )
        step_id = runbook_db.log_step(
            run_id, runbook_id, step_name, step_type, cmd_label, "running"
        )

        try:
            if step_type == "kubectl":
                ok, out = _exec_kubectl_step(step, context)
            elif step_type == "python":
                ok, out = _exec_python_step(step, context)
            elif step_type == "slack":
                ok, out = _exec_slack_step(step, context)
            else:
                ok, out = False, f"unknown step type: {step_type}"

            status = "success" if ok else "failed"
            runbook_db.update_step(
                step_id, status,
                output=out if ok else "",
                error="" if ok else out,
            )
            results.append({"step": step_name, "status": status, "output": out})

            if not ok:
                if on_failure == "continue":
                    overall_status = "partial"
                else:
                    overall_status = "failed"
                    _send_escalation(runbook, step_name, out)
                    break

        except Exception as exc:
            runbook_db.update_step(step_id, "failed", error=str(exc))
            results.append({"step": step_name, "status": "failed", "output": str(exc)})
            if on_failure != "continue":
                overall_status = "failed"
                break
            overall_status = "partial"

    runbook_db.finish_run(run_id, overall_status)
    return {
        "run_id": run_id,
        "runbook_id": runbook_id,
        "status": overall_status,
        "steps": results,
        "success": overall_status in ("success", "partial"),
    }


def _send_escalation(runbook: dict, step_name: str, error: str) -> None:
    try:
        from agent.integrations.slack import send_alert_generic
        send_alert_generic(
            title=f"Runbook failed: {runbook['name']}",
            message=f"Step '{step_name}' failed: {error}",
            severity="critical",
            fields={"Runbook": runbook.get("id", ""), "Step": step_name},
        )
    except Exception:
        pass
