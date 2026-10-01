"""
Integration settings — read and change AtlasOS's connection settings without
a terminal (the dashboard's Settings page), using the same storage the CLI
uses:

  - secrets (names judged by core.secrets.is_secret_name) go to the OS
    keychain via core.secrets.store — the same path as `agent secrets set`;
  - plain settings (JENKINS_URL, AWS_REGION, …) go to .env, one line at a
    time, keeping every other line and comment intact.

Reading never returns a secret's value — only whether it's set and where.
Only names in INTEGRATIONS can be written; this is not a general env editor.

Changes apply to the running process at once (os.environ and the settings
object). Other AtlasOS processes — the daemon, the MCP server — read their
settings at start-up and pick changes up on their next restart.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from agent.core import secrets as sec

ENV_PATH = Path(".env")
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_SAFE_VALUE = re.compile(r"^[^\x00-\x1f\x7f]*$")       # no newlines / control chars

# (name, label, kind) — kind: text | select:<a>,<b> | url
INTEGRATIONS: list[dict] = [
    {"id": "anthropic", "name": "Claude (AI)", "icon": "ph-sparkle",
     "help": "Powers diagnoses and the assistant. Get a key at console.anthropic.com → API keys.",
     "fields": [("ANTHROPIC_API_KEY", "API key", "text"),
                ("LLM_MODEL", "Model", "text")]},
    {"id": "slack", "name": "Slack", "icon": "ph-slack-logo",
     "help": "Alerts, the daily summary, and Approve/Reject buttons. Webhook from your Slack app → Incoming Webhooks; "
             "signing secret from Basic Information.",
     "fields": [("SLACK_WEBHOOK_URL", "Incoming webhook URL", "url"),
                ("SLACK_SIGNING_SECRET", "Signing secret (for buttons)", "text")]},
    {"id": "jenkins", "name": "Jenkins", "icon": "ph-hammer",
     "help": "Build scans, diagnoses and the failure hook. API token from Jenkins → your user → Configure.",
     "fields": [("JENKINS_URL", "URL", "url"), ("JENKINS_USER", "User", "text"),
                ("JENKINS_API_TOKEN", "API token", "text"),
                ("JENKINS_WEBHOOK_SECRET", "Failure-hook secret", "text")]},
    {"id": "github", "name": "GitHub", "icon": "ph-github-logo",
     "help": "GitHub Actions runs and diagnoses. A fine-grained token with Actions: read. "
             "Without one, AtlasOS falls back to a logged-in `gh` CLI.",
     "fields": [("GITHUB_TOKEN", "Token", "text"),
                ("GITHUB_WEBHOOK_SECRET", "Webhook secret", "text")]},
    {"id": "aws", "name": "AWS", "icon": "ph-cloud",
     "help": "Cost, RDS and EC2 checks. iam_role uses the machine's role or ~/.aws defaults; "
             "sso_profile uses the profile below; access_key uses the two keys below.",
     "fields": [("AWS_AUTH_METHOD", "Auth method", "select:iam_role,sso_profile,access_key"),
                ("AWS_REGION", "Region", "text"), ("AWS_PROFILE", "Profile", "text"),
                ("AWS_ACCESS_KEY_ID", "Access key ID", "text"),
                ("AWS_SECRET_ACCESS_KEY", "Secret access key", "text")]},
    {"id": "paging", "name": "PagerDuty / OpsGenie", "icon": "ph-siren",
     "help": "Escalation when auto-healing fails 3 times. Never tested from here — a test would page someone.",
     "fields": [("PAGERDUTY_ROUTING_KEY", "PagerDuty routing key", "text"),
                ("OPSGENIE_API_KEY", "OpsGenie API key", "text")]},
]
ALLOWED = {name for i in INTEGRATIONS for name, _, _ in i["fields"]}


class SettingsError(Exception):
    pass


# ---------------------------------------------------------------------------
# .env, one line at a time
# ---------------------------------------------------------------------------

def _env_lines() -> list[str]:
    return ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []


def _env_value(name: str) -> str | None:
    for line in _env_lines():
        m = _ENV_LINE.match(line)
        if m and m.group(1) == name:
            v = line.split("=", 1)[1].strip()
            return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v
    return None


def _quote(value: str) -> str:
    if re.search(r"[\s#'\"]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def set_env_line(name: str, value: str | None) -> None:
    """Set (or with None, remove) one NAME=value line in .env, keeping every
    other line and comment as it was. Written atomically."""
    out, done = [], False
    for line in _env_lines():
        m = _ENV_LINE.match(line)
        if m and m.group(1) == name:
            if value is not None and not done:
                out.append(f"{name}={_quote(value)}")
                done = True
            continue
        out.append(line)
    if value is not None and not done:
        out.append(f"{name}={_quote(value)}")
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ENV_PATH.parent.resolve()), prefix=".env.", text=True)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + ("\n" if out else ""))
    os.replace(tmp, ENV_PATH)


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

def _where(name: str) -> str | None:
    try:
        if name in sec.stored_names():
            return "keychain"
    except Exception:
        pass
    if _env_value(name):
        return ".env"
    if os.environ.get(name):
        return "environment"
    return None


def describe() -> dict:
    """Every integration and its fields. Secret values are never included."""
    try:
        keychain = sec.backend_name() if sec.available() else None
    except Exception:
        keychain = None
    out = []
    for integ in INTEGRATIONS:
        fields = []
        for name, label, kind in integ["fields"]:
            secret = sec.is_secret_name(name)
            where = _where(name)
            f = {"name": name, "label": label, "secret": secret, "set": where is not None, "where": where}
            if kind.startswith("select:"):
                f["kind"], f["options"] = "select", kind.split(":", 1)[1].split(",")
            else:
                f["kind"] = kind
            if not secret:
                f["value"] = os.environ.get(name) or _env_value(name) or _setting_default(name)
            if secret and where == ".env":
                f["warning"] = "Stored as plaintext in .env — saving here moves it to the keychain."
            fields.append(f)
        out.append({k: integ[k] for k in ("id", "name", "icon", "help")} | {"fields": fields})
    return {"keychain": keychain, "integrations": out}


def _setting_default(name: str) -> str:
    try:
        from agent.config import settings
        v = getattr(settings, name.lower(), "")
        return "" if v is None else str(v)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def _apply_live(name: str, value: str | None) -> None:
    """Make the change visible to this process right away."""
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        from agent.config import Settings, settings
        field = name.lower()
        if field in Settings.model_fields:
            default = Settings.model_fields[field].default
            if value is None:
                new = default
            elif isinstance(default, bool):
                new = value.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                new = int(value)
            elif isinstance(default, float):
                new = float(value)
            else:
                new = value
            object.__setattr__(settings, field, new)
    except Exception:
        pass


def save(values: dict[str, str], clear: list[str] | None = None) -> list[str]:
    """Store each non-empty value (secrets → keychain, the rest → .env) and
    remove each name in `clear`. Returns notes worth showing the user."""
    clear = clear or []
    unknown = (set(values) | set(clear)) - ALLOWED
    if unknown:
        raise SettingsError(f"Not an AtlasOS setting: {', '.join(sorted(unknown))}")
    notes: list[str] = []
    for name, raw in values.items():
        value = (raw or "").strip()
        if not value:
            continue                      # empty input = leave unchanged
        if not _SAFE_VALUE.match(value):
            raise SettingsError(f"{name}: value contains a newline or control character")
        if name == "AWS_AUTH_METHOD" and value not in ("iam_role", "sso_profile", "access_key"):
            raise SettingsError("AWS_AUTH_METHOD must be iam_role, sso_profile or access_key")
        if sec.is_secret_name(name):
            try:
                sec.store(name, value)
            except sec.SecretsError as exc:
                raise SettingsError(str(exc)) from exc
            if _env_value(name):
                set_env_line(name, None)  # the keychain copy replaces the plaintext one
                notes.append(f"{name}: removed the plaintext copy from .env")
        else:
            set_env_line(name, value)
        _apply_live(name, value)
    for name in clear:
        if sec.is_secret_name(name):
            try:
                sec.delete(name)
            except sec.SecretsError:
                pass
        if _env_value(name) is not None:
            set_env_line(name, None)
        _apply_live(name, None)
    return notes


# ---------------------------------------------------------------------------
# connection checks — read-only, never send or page anything
# ---------------------------------------------------------------------------

def check(integration_id: str) -> dict:
    """{"status": ok | warn | off | error, "detail": str}"""
    fn = _CHECKS.get(integration_id)
    if fn is None:
        raise SettingsError(f"Unknown integration: {integration_id}")
    try:
        status, detail = fn()
    except Exception as exc:
        status, detail = "error", str(exc)[:300]
    return {"status": status, "detail": detail}


def _check_anthropic():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return "off", "No API key — diagnoses and the assistant won't work."
    import anthropic
    anthropic.Anthropic(api_key=key).models.list(limit=1)     # free call
    return "ok", "API key accepted."


def _check_slack():
    from agent.integrations.slack import is_configured
    if not is_configured():
        return "off", "No webhook — alerts and the daily summary won't be sent."
    if not os.environ.get("SLACK_SIGNING_SECRET"):
        return "warn", "Webhook set, but no signing secret — Approve/Reject buttons won't work."
    return "ok", "Webhook and signing secret set. Use “Send test message” to check delivery."


def _check_jenkins():
    from agent.integrations import jenkins as jk
    if not os.environ.get("JENKINS_URL") and not _setting_default("JENKINS_URL"):
        return "off", "No Jenkins URL."
    info = jk.get_connection_info()
    if not info.connected:
        return "error", f"Can't reach Jenkins: {(info.error or 'no response')[:240]}"
    return "ok", f"Connected — Jenkins {getattr(info, 'version', '') or ''}".strip()


def _check_github():
    from agent.integrations import github_actions as gha
    if not gha._token():
        return "off", "No token and no logged-in `gh` CLI."
    repo = gha.resolve_repo(None)
    gha.list_runs(repo, limit=1, failed_only=False)
    return "ok", f"Token works — reading {repo}."


def _check_aws():
    from agent.integrations.aws import get_aws_client
    ident = get_aws_client("sts").get_caller_identity()
    return "ok", f"Signed in as {ident.get('Arn', '?')}"


def _check_paging():
    if os.environ.get("PAGERDUTY_ROUTING_KEY") or os.environ.get("OPSGENIE_API_KEY"):
        return "ok", "Key set. Not tested from here — a test would page someone."
    return "off", "No key — failed auto-heals escalate to Slack only."


_CHECKS = {"anthropic": _check_anthropic, "slack": _check_slack, "jenkins": _check_jenkins,
           "github": _check_github, "aws": _check_aws, "paging": _check_paging}
