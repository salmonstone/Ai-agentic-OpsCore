"""
agent/skills/_fix_runner.py

Shared fix command runner.

network.py, dns.py, and tls.py all had an identical 40-line apply_fix() method.
One bug fixed in one file wasn't fixed in the others.

This module is the single source of truth. Import it everywhere.

Usage:
    from agent.skills._fix_runner import apply_shell_fix

    result = apply_shell_fix("kubectl rollout restart deploy/coredns -n kube-system")
    # returns "ok" or "failed"

    # with namespace injection:
    result = apply_shell_fix(
        "kubectl delete pod my-pod",
        ns="infragpt",          # appended if -n not already in cmd
    )

    # multi-step && chain:
    result = apply_shell_fix(
        "helm repo update && helm upgrade cert-manager jetstack/cert-manager -n cert-manager"
    )
"""
from __future__ import annotations

import shutil
import subprocess

from agent.integrations.kubectl import run_kubectl
from agent.observability.logging import get_logger

log = get_logger(__name__)


def apply_shell_fix(cmd: str, ns: str = "") -> str:
    """
    Execute a fix command or an ``&&``-chained sequence of commands.

    Routing:
    - ``kubectl`` commands → the safe ``run_kubectl()`` wrapper (validated, logged)
    - everything else (helm, cmctl, openssl…) → subprocess with 60s timeout

    Args:
        cmd: Command string. May contain ``&&`` to chain multiple steps.
        ns:  Namespace to inject when the command doesn't already contain
             ``-n`` or ``--namespace``.

    Returns:
        ``"ok"`` if all steps succeeded, ``"failed"`` otherwise.
    """
    try:
        from rich.console import Console
        console = Console()
        _rich = True
    except ImportError:
        console = None  # type: ignore[assignment]
        _rich = False

    def _print(msg: str) -> None:
        if _rich and console:
            console.print(msg)
        else:
            print(msg)

    steps   = [s.strip() for s in cmd.split("&&") if s.strip()]
    success = True

    for step in steps:
        tokens = step.split()
        if not tokens:
            continue

        binary = tokens[0]
        _print(f"[dim]$ {step}[/dim]" if _rich else f"$ {step}")

        if binary == "kubectl":
            args = tokens[1:]
            if ns and "-n" not in args and "--namespace" not in args:
                args += ["-n", ns]
            r  = run_kubectl(args)
            ok = r.success
            out = r.output if r.success else r.error

        elif shutil.which(binary):
            try:
                proc = subprocess.run(
                    tokens, capture_output=True, text=True, timeout=60,
                )
                ok  = proc.returncode == 0
                out = (proc.stdout + proc.stderr).strip()
            except subprocess.TimeoutExpired:
                ok  = False
                out = f"'{binary}' timed out after 60s"
            except Exception as exc:
                ok  = False
                out = str(exc)

        else:
            ok  = False
            out = f"'{binary}' not found — install it first"
            log.warning("fix_runner.binary_not_found", binary=binary)

        log.info("fix_runner.step", cmd=step[:80], ok=ok)

        if _rich:
            color = "green" if ok else "red"
            icon  = "✓" if ok else "✗"
            _print(f"[{color}]{icon} {out[:140]}[/{color}]")
        else:
            prefix = "✓" if ok else "✗"
            print(f"{prefix} {out[:140]}")

        if not ok:
            success = False
            log.warning("fix_runner.step_failed", cmd=step[:80], output=out[:200])
            break   # honour &&  semantics: stop on first failure

    return "ok" if success else "failed"
