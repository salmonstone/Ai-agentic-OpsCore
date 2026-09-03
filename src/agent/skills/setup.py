"""
Interactive setup wizard for AI Agentic OS.

Walks the user through configuring every integration step-by-step.
Writes results to .env safely (never overwrites keys that already work).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

console = Console()

_ENV_PATH = Path(".env")
_SECTION  = "[bold cyan]"
_OK       = "[bold green]✓[/bold green]"
_FAIL     = "[bold red]✗[/bold red]"
_WARN     = "[bold yellow]⚠[/bold yellow]"
_SKIP     = "[dim]–[/dim]"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str = "", password: bool = False) -> str:
    """Prompt the user and return their input (stripped)."""
    suffix = f" [{default}]" if default else ""
    display = f"  [cyan]{prompt}{suffix}:[/cyan] "
    if password:
        import getpass
        console.print(display, end="")
        val = getpass.getpass("").strip()
    else:
        console.print(display, end="")
        val = input("").strip()
    return val or default


def _confirm(prompt: str, default: bool = False) -> bool:
    hint = "[y/N]" if not default else "[Y/n]"
    console.print(f"  [cyan]{prompt} {hint}:[/cyan] ", end="")
    val = input("").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def _run(cmd: list[str], timeout: int = 15) -> tuple[bool, str]:
    """Run a shell command. Returns (success, output)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out
    except FileNotFoundError:
        return False, f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as exc:
        return False, str(exc)


def _read_env() -> dict[str, str]:
    """Read current .env into a dict."""
    env: dict[str, str] = {}
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _write_env(env: dict[str, str]) -> None:
    """Write env dict back to .env, preserving order."""
    lines = [f"{k}={v}" for k, v in env.items()]
    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# Module-level cache — populated once at wizard start, flushed at the end.
# Avoids read-write-read cycles that scramble values on Windows.
_env_cache: dict[str, str] = {}


def _set_env(key: str, value: str) -> None:
    """Update the in-memory env cache and the process environment.
    Does NOT write to disk — call _flush_env() once at the end."""
    _env_cache[key] = value
    os.environ[key] = value


def _flush_env() -> None:
    """Write the accumulated env cache to .env in one atomic write."""
    _write_env(_env_cache)


def _section(title: str) -> None:
    console.print()
    console.print(Rule(f"[bold cyan]{title}[/bold cyan]"))
    console.print()


# ---------------------------------------------------------------------------
# Connection testers
# ---------------------------------------------------------------------------

def _test_anthropic(key: str) -> tuple[bool, str]:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
        )
        return True, "claude-haiku-4-5-20251001"
    except Exception as exc:
        return False, str(exc)[:120]


def _test_openai(key: str, base_url: str = "") -> tuple[bool, str]:
    try:
        import openai
        kwargs: dict[str, Any] = {"api_key": key}
        if base_url:
            kwargs["base_url"] = base_url
        client = openai.OpenAI(**kwargs)
        models = client.models.list()
        names = [m.id for m in list(models)[:3]]
        return True, ", ".join(names)
    except Exception as exc:
        return False, str(exc)[:120]


def _test_groq(key: str) -> tuple[bool, str]:
    try:
        import httpx
        r = httpx.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.status_code == 200:
            ids = [m["id"] for m in r.json().get("data", [])[:3]]
            return True, ", ".join(ids)
        return False, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, str(exc)[:120]


def _test_voyage(key: str) -> tuple[bool, str]:
    try:
        import voyageai
        client = voyageai.Client(api_key=key)
        client.embed(["test"], model="voyage-3-lite", input_type="document")
        return True, "voyage-3-lite"
    except Exception as exc:
        return False, str(exc)[:120]


def _test_aws(profile: str = "", region: str = "") -> tuple[bool, str]:
    cmd = ["aws", "sts", "get-caller-identity", "--output", "text"]
    if profile:
        cmd += ["--profile", profile]
    if region:
        cmd += ["--region", region]
    ok, out = _run(cmd, timeout=20)
    return ok, out[:120] if ok else out[:120]


def _test_kubectl() -> tuple[bool, str]:
    ok, out = _run(["kubectl", "get", "nodes", "--no-headers"], timeout=15)
    if ok:
        lines = [l for l in out.splitlines() if l.strip()]
        return True, f"{len(lines)} node(s) ready"
    return False, out[:120]


# ---------------------------------------------------------------------------
# Wizard sections
# ---------------------------------------------------------------------------

class SetupWizard:
    def __init__(self) -> None:
        self.env     = _read_env()
        self.results: dict[str, tuple[str, str]] = {}  # name → (status, detail)

    def _record(self, name: str, ok: bool, detail: str) -> None:
        self.results[name] = ("ok" if ok else "fail", detail)

    # ── 1. AI Provider ──────────────────────────────────────────────────────

    def configure_llm(self) -> None:
        _section("Step 1 — AI Provider")
        console.print("  Which AI provider(s) do you want to use?\n")
        console.print("  [1] Anthropic Claude [bold](recommended)[/bold]")
        console.print("  [2] OpenAI / GPT")
        console.print("  [3] Groq [dim](free tier)[/dim]")
        console.print("  [4] Any OpenAI-compatible API [dim](Ollama, Together, etc.)[/dim]")
        console.print("  [5] Skip")
        console.print()

        choices_raw = _ask("Enter numbers separated by commas (e.g. 1,3)", default="1")
        choices = {c.strip() for c in choices_raw.split(",")}

        if "1" in choices:
            self._setup_anthropic()
        if "2" in choices:
            self._setup_openai()
        if "3" in choices:
            self._setup_groq()
        if "4" in choices:
            self._setup_custom_openai()

        # Voyage AI for semantic memory
        console.print()
        if _confirm("Enable semantic memory search with Voyage AI? (free, recommended)"):
            self._setup_voyage()

    def _setup_anthropic(self) -> None:
        console.print()
        console.print("  [bold]Anthropic Claude[/bold]  →  https://console.anthropic.com/keys")
        existing = self.env.get("ANTHROPIC_API_KEY", "")
        if existing:
            console.print(f"  [dim]Found existing key: {existing[:12]}…[/dim]")
            if not _confirm("  Replace it?", default=False):
                with console.status("  Testing existing key…"):
                    ok, detail = _test_anthropic(existing)
                icon = _OK if ok else _FAIL
                console.print(f"  {icon} Anthropic: {detail}")
                self._record("Anthropic Claude", ok, detail)
                return

        key = _ask("Anthropic API key (sk-ant-…)", password=True)
        if not key:
            console.print(f"  {_SKIP} Anthropic skipped")
            return

        with console.status("  Testing connection…"):
            ok, detail = _test_anthropic(key)

        if ok:
            _set_env("ANTHROPIC_API_KEY", key)
            console.print(f"  {_OK} Anthropic connected — {detail}")
            self._record("Anthropic Claude", True, detail)
        else:
            console.print(f"  {_FAIL} Connection failed: {detail}")
            if _confirm("  Save key anyway?", default=False):
                _set_env("ANTHROPIC_API_KEY", key)
            self._record("Anthropic Claude", False, detail)

    def _setup_openai(self) -> None:
        console.print()
        console.print("  [bold]OpenAI[/bold]  →  https://platform.openai.com/api-keys")
        key = _ask("OpenAI API key (sk-…)", password=True)
        if not key:
            console.print(f"  {_SKIP} OpenAI skipped")
            return

        with console.status("  Testing connection…"):
            ok, detail = _test_openai(key)

        if ok:
            _set_env("OPENAI_API_KEY", key)
            console.print(f"  {_OK} OpenAI connected — {detail}")
            self._record("OpenAI", True, detail)
        else:
            console.print(f"  {_FAIL} Connection failed: {detail}")
            if _confirm("  Save key anyway?", default=False):
                _set_env("OPENAI_API_KEY", key)
            self._record("OpenAI", False, detail)

    def _setup_groq(self) -> None:
        console.print()
        console.print("  [bold]Groq[/bold]  →  https://console.groq.com/keys  (free)")
        key = _ask("Groq API key (gsk_…)", password=True)
        if not key:
            console.print(f"  {_SKIP} Groq skipped")
            return

        with console.status("  Testing connection…"):
            ok, detail = _test_groq(key)

        if ok:
            _set_env("GROQ_API_KEY", key)
            console.print(f"  {_OK} Groq connected — {detail}")
            self._record("Groq", True, detail)
        else:
            console.print(f"  {_FAIL} Connection failed: {detail}")
            if _confirm("  Save key anyway?", default=False):
                _set_env("GROQ_API_KEY", key)
            self._record("Groq", False, detail)

    def _setup_custom_openai(self) -> None:
        console.print()
        console.print("  [bold]Custom OpenAI-compatible API[/bold]  (Ollama, Together, Mistral, etc.)")
        name     = _ask("Integration name (e.g. Ollama, Together)", default="Custom")
        base_url = _ask("Base URL (e.g. http://localhost:11434/v1)", default="")
        key      = _ask("API key (leave blank for Ollama)", password=True)

        env_key = f"{name.upper().replace(' ', '_')}_API_KEY"
        env_url = f"{name.upper().replace(' ', '_')}_BASE_URL"

        with console.status("  Testing connection…"):
            ok, detail = _test_openai(key or "ollama", base_url)

        if ok:
            if key:
                _set_env(env_key, key)
            if base_url:
                _set_env(env_url, base_url)
            console.print(f"  {_OK} {name} connected — {detail}")
            self._record(name, True, detail)
        else:
            console.print(f"  {_FAIL} Connection failed: {detail}")
            if _confirm("  Save config anyway?", default=False):
                if key:
                    _set_env(env_key, key)
                if base_url:
                    _set_env(env_url, base_url)
            self._record(name, False, detail)

    def _setup_voyage(self) -> None:
        console.print()
        console.print("  [bold]Voyage AI[/bold]  →  https://www.voyageai.com  (200M tokens/month free)")
        existing = self.env.get("VOYAGE_API_KEY", "")
        if existing:
            console.print(f"  [dim]Found existing key: {existing[:12]}…[/dim]")
            if not _confirm("  Replace it?", default=False):
                self._record("Voyage AI (memory)", True, "existing key")
                return

        key = _ask("Voyage API key (pa-…)", password=True)
        if not key:
            console.print(f"  {_SKIP} Voyage skipped — using keyword matching for memory")
            return

        with console.status("  Testing connection…"):
            ok, detail = _test_voyage(key)

        if ok:
            _set_env("VOYAGE_API_KEY", key)
            console.print(f"  {_OK} Voyage AI connected — {detail}")
            self._record("Voyage AI (memory)", True, detail)
        else:
            console.print(f"  {_FAIL} Connection failed: {detail}")
            if _confirm("  Save key anyway?", default=False):
                _set_env("VOYAGE_API_KEY", key)
            self._record("Voyage AI (memory)", False, detail)

    # ── 2. AWS / Kubernetes ──────────────────────────────────────────────────

    def configure_aws(self) -> None:
        _section("Step 2 — AWS / Kubernetes")

        if not _confirm("Connect AWS and/or Kubernetes?", default=False):
            console.print(f"  {_SKIP} AWS/K8s skipped")
            return

        console.print()
        console.print("  How do you want to authenticate?\n")
        console.print("  [1] AWS Named Profile [bold](recommended)[/bold]")
        console.print("  [2] Access Keys + Secret [dim](less secure)[/dim]")
        console.print("  [3] Skip — already configured / use existing kubeconfig")
        console.print()
        choice = _ask("Choose", default="1")

        if choice == "1":
            self._aws_profile()
        elif choice == "2":
            self._aws_keys()
        else:
            console.print(f"  {_SKIP} Using existing AWS/kubeconfig")
            ok, detail = _test_kubectl()
            icon = _OK if ok else _WARN
            console.print(f"  {icon} kubectl: {detail}")
            self._record("Kubernetes", ok, detail)

    def _aws_profile(self) -> None:
        profile = _ask("AWS profile name", default="agentic-os")
        region  = _ask("AWS region", default="ap-south-1")
        cluster = _ask("EKS cluster name (leave blank to skip EKS)", default="")

        with console.status("  Verifying AWS identity…"):
            ok, detail = _test_aws(profile, region)

        if not ok:
            console.print(f"  {_FAIL} AWS auth failed: {detail}")
            console.print(f"  [dim]Run: aws configure --profile {profile}[/dim]")
            self._record("AWS", False, detail)
        else:
            _set_env("AWS_PROFILE", profile)
            _set_env("AWS_DEFAULT_REGION", region)
            console.print(f"  {_OK} AWS connected — {detail[:80]}")
            self._record("AWS", True, detail[:60])

            if cluster:
                _set_env("EKS_CLUSTER", cluster)
                with console.status("  Updating kubeconfig…"):
                    ok2, out2 = _run([
                        "aws", "eks", "update-kubeconfig",
                        "--name", cluster,
                        "--region", region,
                        "--profile", profile,
                    ], timeout=30)
                if ok2:
                    console.print(f"  {_OK} kubeconfig updated for {cluster}")
                    ok3, detail3 = _test_kubectl()
                    icon = _OK if ok3 else _FAIL
                    console.print(f"  {icon} kubectl: {detail3}")
                    self._record("Kubernetes (EKS)", ok3, detail3)
                else:
                    console.print(f"  {_FAIL} kubeconfig update failed: {out2[:100]}")
                    self._record("Kubernetes (EKS)", False, out2[:60])

    def _aws_keys(self) -> None:
        console.print()
        console.print(
            f"  {_WARN} [yellow]Access keys are less secure than named profiles.[/yellow]\n"
            "  [dim]Prefer: aws configure --profile agentic-os[/dim]"
        )
        if not _confirm("  Continue with access keys?", default=False):
            return

        access_key = _ask("AWS Access Key ID", password=True)
        secret_key = _ask("AWS Secret Access Key", password=True)
        region     = _ask("Region", default="ap-south-1")

        _set_env("AWS_ACCESS_KEY_ID", access_key)
        _set_env("AWS_SECRET_ACCESS_KEY", secret_key)
        _set_env("AWS_DEFAULT_REGION", region)

        # Test immediately
        ok, detail = _run(
            ["aws", "sts", "get-caller-identity", "--output", "text"],
            timeout=20,
        )
        icon = _OK if ok else _FAIL
        console.print(f"  {icon} AWS: {detail[:100]}")
        self._record("AWS", ok, detail[:60])

        if ok:
            cluster = _ask("EKS cluster name (leave blank to skip)", default="")
            if cluster:
                with console.status("  Updating kubeconfig…"):
                    ok2, out2 = _run([
                        "aws", "eks", "update-kubeconfig",
                        "--name", cluster, "--region", region,
                    ], timeout=30)
                if ok2:
                    ok3, d3 = _test_kubectl()
                    console.print(f"  {_OK if ok3 else _FAIL} kubectl: {d3}")
                    self._record("Kubernetes (EKS)", ok3, d3)
                else:
                    console.print(f"  {_FAIL} kubeconfig: {out2[:80]}")
                    self._record("Kubernetes (EKS)", False, out2[:60])

    # ── 3. Gmail ─────────────────────────────────────────────────────────────

    def configure_gmail(self) -> None:
        _section("Step 3 — Gmail (optional)")

        if not _confirm("Connect Gmail?", default=False):
            console.print(f"  {_SKIP} Gmail skipped")
            return

        console.print("""
  To connect Gmail you need a Google OAuth app:

  1. Go to [link]https://console.cloud.google.com/apis/credentials[/link]
  2. Create a project (or select existing)
  3. Enable the Gmail API
  4. Create credentials → OAuth 2.0 Client ID → Desktop App
  5. Download the JSON and note the client_id and client_secret
""")

        client_id     = _ask("Google OAuth client_id", password=False)
        client_secret = _ask("Google OAuth client_secret", password=True)

        if not client_id or not client_secret:
            console.print(f"  {_SKIP} Gmail skipped — no credentials provided")
            return

        _set_env("GMAIL_CLIENT_ID", client_id)
        _set_env("GMAIL_CLIENT_SECRET", client_secret)

        console.print()
        console.print("  [dim]Launching OAuth browser flow…[/dim]")
        try:
            from agent.skills.gmail import GmailSkill
            skill = GmailSkill()
            # Trigger auth flow — it will open a browser
            result = skill.execute({"mode": "list", "max_results": 1})
            if result.get("emails"):
                console.print(f"  {_OK} Gmail connected — auth successful")
                self._record("Gmail", True, "OAuth complete")
            else:
                console.print(f"  {_WARN} Gmail auth done but no emails returned — may need scopes")
                self._record("Gmail", True, "connected")
        except Exception as exc:
            console.print(f"  {_FAIL} Gmail auth failed: {str(exc)[:120]}")
            console.print("  [dim]You can retry later with: agent gmail list[/dim]")
            self._record("Gmail", False, str(exc)[:60])

    # ── 4. Slack alerting ─────────────────────────────────────────────────────

    def configure_slack(self) -> None:
        _section("Step 4 — Slack Alerts")
        console.print("  Get Slack alerts when pods crash, resources spike, or security issues appear.\n")

        if not _confirm("Enable Slack alerts?", default=False):
            console.print(f"  {_SKIP} Slack skipped")
            return

        console.print("""
  To create a Slack webhook:
  1. Go to https://api.slack.com/apps  → Create App → Incoming Webhooks
  2. Enable Incoming Webhooks
  3. Add New Webhook to Workspace → pick a channel
  4. Copy the Webhook URL (starts with https://hooks.slack.com/…)
""")

        webhook = _ask("Slack Webhook URL (https://hooks.slack.com/…)", password=True)
        if not webhook:
            console.print(f"  {_SKIP} Slack skipped — no webhook provided")
            return

        _set_env("SLACK_WEBHOOK_URL", webhook)
        _set_env("SLACK_ENABLED", "true")

        default_ch  = _ask("Default alert channel", default="#alerts")
        critical_ch = _ask("Critical incidents channel", default="#incidents")
        _set_env("SLACK_DEFAULT_CHANNEL",  default_ch)
        _set_env("SLACK_CRITICAL_CHANNEL", critical_ch)

        # Test immediately
        console.print()
        with console.status("  Testing webhook…"):
            try:
                import os, httpx
                from datetime import datetime, timezone
                payload = {
                    "text": "✅ InfraGPT connected to Slack!",
                    "blocks": [{
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "✅ *InfraGPT Slack integration working!*\n"
                                f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
                                "_You'll receive alerts here when pods crash, resources spike, or security issues are found._"
                            ),
                        },
                    }],
                }
                r = httpx.post(webhook, json=payload, timeout=8)
                ok = r.status_code == 200
                detail = "test message sent" if ok else f"HTTP {r.status_code}"
            except Exception as exc:
                ok = False
                detail = str(exc)[:120]

        icon = _OK if ok else _FAIL
        console.print(f"  {icon} Slack: {detail}")
        self._record("Slack Alerts", ok, detail)

    # ── 5. GitHub Webhook ────────────────────────────────────────────────────

    def configure_github_webhooks(self) -> None:
        _section("Step 5 — GitHub Webhook Deployments")
        console.print(
            "  Auto-deploy when you push to GitHub — each push triggers an\n"
            "  AI safety analysis and Slack approval before deploying.\n"
        )

        if not _confirm("Configure GitHub webhook deployments?", default=False):
            console.print(f"  {_SKIP} GitHub webhooks skipped")
            return

        # ── Step 5a: Generate webhook secret ──────────────────────────────
        import secrets as _secrets
        webhook_secret = _secrets.token_hex(16)  # 32 hex chars
        _set_env("GITHUB_WEBHOOK_SECRET", webhook_secret)

        console.print()
        console.print(Panel(
            f"[bold yellow]Webhook secret (save this — shown once):[/bold yellow]\n\n"
            f"  [bold cyan]{webhook_secret}[/bold cyan]\n\n"
            "[dim]Saved to .env as GITHUB_WEBHOOK_SECRET[/dim]",
            border_style="yellow",
            padding=(1, 2),
        ))
        console.print()

        # ── Step 5b: Collect mappings ──────────────────────────────────────
        console.print(
            "  Add your GitHub repos and their Kubernetes deployments.\n"
            "  Each push to a mapped repo triggers an AI deployment approval via Slack.\n"
        )

        from agent.core.models import WebhookMapping, WebhookConfig
        from agent.integrations.mapping_loader import MappingLoader

        loader   = MappingLoader()
        mappings: list[WebhookMapping] = []

        while True:
            console.print(Rule("[dim]Add mapping[/dim]"))

            repo = _ask("GitHub repo (e.g. username/repo-name)")
            if not repo:
                break
            if "/" not in repo:
                console.print(f"  {_WARN} Repo must be in 'owner/name' format")
                continue

            branch      = _ask("Branch to watch", default="main")
            deployment  = _ask("Kubernetes deployment name")
            namespace   = _ask("Kubernetes namespace", default="default")
            image_prefix = _ask(
                "Image prefix (ECR URL without tag,\n"
                "    e.g. 123456.dkr.ecr.us-east-1.amazonaws.com/app)",
                default="",
            )
            auto_approve = _confirm("Auto-approve LOW risk deploys?", default=False)

            # Validate
            console.print()
            mapping = WebhookMapping(
                repo                  = repo,
                branch                = branch,
                deployment            = deployment,
                namespace             = namespace,
                image_prefix          = image_prefix,
                auto_approve_low_risk = auto_approve,
            )

            with console.status("  Validating against cluster…"):
                result = loader.validate_mapping(mapping)

            if result.valid:
                console.print(f"  {_OK} Cluster validation passed")
            else:
                for err in result.errors:
                    console.print(f"  {_WARN} {err}")
                if not _confirm("  Save mapping anyway?", default=True):
                    console.print("  [dim]Mapping discarded.[/dim]")
                    console.print()
                    if not _confirm("Add another repo?", default=False):
                        break
                    continue

            loader.add_mapping(mapping)
            mappings.append(mapping)
            console.print(
                f"  {_OK} Mapping saved: "
                f"[cyan]{repo}:{branch}[/cyan] → "
                f"[green]{deployment}/{namespace}[/green]"
            )
            console.print()

            if not _confirm("Add another repo?", default=False):
                break

        if not mappings:
            console.print(f"  {_SKIP} No mappings configured")
            self._record("GitHub Webhooks", False, "no mappings")
            return

        # ── Step 5c: Show instructions panel ──────────────────────────────
        repos_list = "\n".join(
            f"  •  github.com/{m.repo}/settings/hooks" for m in mappings
        )
        console.print()
        console.print(Panel(
            "[bold]GITHUB WEBHOOK SETUP[/bold]\n\n"
            "For EACH GitHub repo you mapped, do this:\n\n"
            "  1. Go to the repo → Settings → Webhooks\n"
            f"{repos_list}\n\n"
            "  2. Click [bold]Add webhook[/bold]\n"
            "  3. [bold]Payload URL:[/bold]\n"
            "       http://YOUR-SERVER-IP:8080/webhook/github\n"
            "  4. [bold]Content type:[/bold] application/json\n"
            f"  5. [bold]Secret:[/bold] {webhook_secret}\n"
            "  6. [bold]Events:[/bold] Just the push event\n"
            "  7. Click [bold]Add webhook[/bold]\n\n"
            "Then start the listener:\n"
            "  [bold cyan]agent deploy webhook-start --port 8080[/bold cyan]",
            title="[bold green]Next Steps[/bold green]",
            border_style="green",
            padding=(1, 2),
        ))
        console.print()

        self._record("GitHub Webhooks", True, f"{len(mappings)} mapping(s) saved")

    # ── 6. Custom integrations ────────────────────────────────────────────────

    def configure_custom(self) -> None:
        _section("Step 6 — Custom Integrations")

        if not _confirm("Add any other API integrations? (GitHub, Jira, etc.)", default=False):
            console.print(f"  {_SKIP} No custom integrations")
            return

        console.print('  [dim]Type "done" as the name to finish.[/dim]\n')

        while True:
            name = _ask("Integration name (e.g. GitHub, Slack, Jira)")
            if not name or name.lower() == "done":
                break

            key      = _ask(f"{name} API key / token", password=True)
            base_url = _ask(f"{name} base URL (leave blank if not needed)", default="")

            env_key = f"{name.upper().replace(' ', '_').replace('-', '_')}_API_KEY"
            env_url = f"{name.upper().replace(' ', '_').replace('-', '_')}_BASE_URL"

            if key:
                _set_env(env_key, key)
                console.print(f"  {_OK} Saved {env_key}")
            if base_url:
                _set_env(env_url, base_url)
                console.print(f"  {_OK} Saved {env_url}")

            self._record(name, True, "key saved")
            console.print()

    # ── 7. Default settings ───────────────────────────────────────────────────

    def configure_defaults(self) -> None:
        _section("Step 7 — Default Settings")

        model = _ask(
            "Default AI model",
            default=self.env.get("LLM_MODEL", "claude-haiku-4-5-20251001"),
        )
        _set_env("LLM_MODEL", model)

        ns = _ask("Default Kubernetes namespace", default="all")
        _set_env("K8S_DEFAULT_NAMESPACE", ns)

        vault = _ask("Memory vault path", default=self.env.get("VAULT_PATH", "data/vault"))
        _set_env("VAULT_PATH", vault)

        console.print(f"  {_OK} Settings saved")

    # ── Summary ───────────────────────────────────────────────────────────────

    def show_summary(self) -> None:
        console.print()
        console.print()

        if not self.results:
            console.print(Panel(
                "[dim]Nothing configured.[/dim]\n\nRun [bold]agent setup[/bold] to configure.",
                border_style="dim",
            ))
            return

        rows: list[tuple[str, str, str]] = []
        all_ok = True
        for name, (status, detail) in self.results.items():
            if status == "ok":
                icon   = "[bold green]✓[/bold green]"
                label  = "[green]Connected[/green]"
            else:
                icon   = "[bold red]✗[/bold red]"
                label  = "[red]Failed[/red]"
                all_ok = False
            rows.append((icon, name, label))

        tbl = Table(
            show_header=False,
            box=None,
            padding=(0, 2),
        )
        tbl.add_column(width=3)
        tbl.add_column(min_width=28)
        tbl.add_column(width=14)

        for icon, name, label in rows:
            tbl.add_row(icon, name, label)

        footer = "\n  [bold]Run:[/bold] [cyan]agent --help[/cyan]  to get started"
        if not all_ok:
            footer += "\n  [dim]Re-run[/dim] [cyan]agent setup[/cyan] [dim]to fix failed integrations[/dim]"

        console.print(Panel(
            tbl,
            title="[bold white]  SETUP COMPLETE  [/bold white]",
            subtitle=footer,
            border_style="bold green" if all_ok else "yellow",
            padding=(1, 3),
        ))
        console.print()

    # ── Main entry ────────────────────────────────────────────────────────────

    def run(self) -> None:
        global _env_cache
        # Load the existing .env into the cache ONCE — all _set_env calls
        # update this dict; _flush_env() writes it to disk in one shot.
        _env_cache = _read_env()

        console.print()
        console.print(Panel(
            "[bold cyan]AI Agentic OS — Setup Wizard[/bold cyan]\n\n"
            "[dim]Configures your AI providers, cloud connections, and integrations.\n"
            "Takes about 2-5 minutes. Press Ctrl+C at any time to stop.[/dim]",
            border_style="cyan",
            padding=(1, 3),
        ))
        console.print()

        try:
            self.configure_llm()
            self.configure_aws()
            self.configure_gmail()
            self.configure_slack()
            self.configure_github_webhooks()
            self.configure_custom()
            self.configure_defaults()
        except KeyboardInterrupt:
            console.print("\n\n  [yellow]Setup interrupted.[/yellow]")
            console.print("  [dim]Run[/dim] [cyan]agent setup[/cyan] [dim]to continue.[/dim]\n")
        finally:
            _flush_env()  # single atomic write — no scrambled values

        self.show_summary()
