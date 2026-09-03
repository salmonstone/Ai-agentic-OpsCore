"""CLI command discovery — introspects the Typer app at runtime.

Approach: typer.main.get_command(app) returns a Click CommandGroup.
Walk it recursively to extract every command, its params, help text,
and whether it's destructive. Zero hardcoding — if cli.py adds a new
command the next /api/commands call picks it up automatically.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any

# ── cache (max 10 s so restarts propagate quickly) ──────────────────────────
_cache: dict = {}
_cache_ts: float = 0.0
_CACHE_TTL = 10.0

# ── known destructive keywords ───────────────────────────────────────────────
_DESTRUCTIVE_WORDS = {
    "fix", "apply", "approve", "reject", "restart", "rollback",
    "delete", "scale", "patch", "stop", "drain", "cordon", "exec",
    "resolve", "send", "page",
}

_EVAL_GROUPS = {"eval"}


@dataclass
class ParamInfo:
    name: str
    type: str          # string / int / float / bool / flag
    default: Any
    required: bool
    help_text: str


@dataclass
class CommandInfo:
    full_command: str   # "k8s scan"
    group: str          # "k8s"
    name: str           # "scan"
    help_text: str
    params: list[ParamInfo] = field(default_factory=list)
    is_destructive: bool = False
    is_eval: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _click_type_name(param) -> str:
    import click
    t = param.type
    tn = type(t).__name__.upper()
    if tn in ("BOOL", "BOOLEAN"):
        return "bool"
    if tn == "INT":
        return "int"
    if tn == "FLOAT":
        return "float"
    if tn == "CHOICE":
        return "choice"
    # Also handle singleton instances by name
    name = getattr(t, "name", "").upper()
    if name in ("BOOL", "BOOLEAN"):
        return "bool"
    if name == "INT":
        return "int"
    if name == "FLOAT":
        return "float"
    return "string"


def _extract_params(click_cmd) -> list[ParamInfo]:
    params = []
    for p in click_cmd.params:
        import click
        is_flag = isinstance(p, click.Option) and p.is_flag
        type_name = "flag" if is_flag else _click_type_name(p)
        required = getattr(p, "required", False)
        # Arguments are always required unless they have a default
        if hasattr(p, "default") and p.default is not None:
            required = False
        raw_default = p.default if not callable(p.default) else None
        # Ensure default is JSON-serializable (Path, Enum etc. → str)
        if raw_default is not None and not isinstance(raw_default, (str, int, float, bool)):
            raw_default = str(raw_default)
        params.append(ParamInfo(
            name=p.name or "",
            type=type_name,
            default=raw_default,
            required=bool(required),
            help_text=getattr(p, "help", "") or "",
        ))
    return params


def _is_destructive(group: str, cmd_name: str, help_text: str) -> bool:
    tokens = {group.lower(), cmd_name.lower()}
    tokens.update(help_text.lower().split())
    return bool(tokens & _DESTRUCTIVE_WORDS)


def introspect_cli() -> list[CommandInfo]:
    """Walk the Typer CLI app and return every discoverable command."""
    try:
        import typer.main as _tm
        from agent.cli import app as _cli_app
        root = _tm.get_command(_cli_app)
    except Exception:
        return []

    results: list[CommandInfo] = []

    def _walk(click_cmd, group: str = ""):
        import click
        # Use hasattr check so it works with both Click 7 (MultiCommand) and Click 8+ (Group)
        is_group = hasattr(click_cmd, "list_commands") and callable(click_cmd.list_commands)
        if is_group:
            for sub_name in (click_cmd.list_commands(None) or []):
                try:
                    sub = click_cmd.get_command(None, sub_name)
                except Exception:
                    continue
                if sub is None:
                    continue
                sub_is_group = hasattr(sub, "list_commands") and callable(sub.list_commands)
                if sub_is_group:
                    _walk(sub, group=sub_name)
                else:
                    full = f"{group} {sub_name}".strip() if group else sub_name
                    grp = group or "root"
                    ht = sub.help or sub.short_help or ""
                    results.append(CommandInfo(
                        full_command=full,
                        group=grp,
                        name=sub_name,
                        help_text=ht,
                        params=_extract_params(sub),
                        is_destructive=_is_destructive(grp, sub_name, ht),
                        is_eval=(grp in _EVAL_GROUPS),
                    ))
        else:
            full = f"{group} {click_cmd.name}".strip() if group else (click_cmd.name or "")
            grp = group or "root"
            ht = click_cmd.help or click_cmd.short_help or ""
            results.append(CommandInfo(
                full_command=full,
                group=grp,
                name=click_cmd.name or "",
                help_text=ht,
                params=_extract_params(click_cmd),
                is_destructive=_is_destructive(grp, click_cmd.name or "", ht),
                is_eval=(grp in _EVAL_GROUPS),
            ))

    _walk(root)
    return results


def get_command_groups() -> dict[str, list[dict]]:
    """Return commands grouped by CLI group. Cached for 10 s."""
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache and (now - _cache_ts) < _CACHE_TTL:
        return _cache

    commands = introspect_cli()
    groups: dict[str, list[dict]] = {}
    for cmd in commands:
        groups.setdefault(cmd.group, []).append(cmd.to_dict())

    # Sort groups: root last, eval last-ish
    ordered: dict[str, list[dict]] = {}
    for g in sorted(groups.keys(), key=lambda x: (x == "root", x == "eval", x)):
        ordered[g] = groups[g]

    _cache = ordered
    _cache_ts = now
    return ordered
