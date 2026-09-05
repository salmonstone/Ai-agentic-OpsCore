"""
Interactive setup wizard for AI Agentic OS.

Walks the user through configuring every integration step-by-step.
Writes results to .env safely (never overwrites keys that already work).
"""
from __future__ import annotations

import json
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


def _list_aws_profiles() -> list[str]:
    """Return AWS CLI profile names found in ~/.aws/credentials and ~/.aws/config."""
    import configparser

    profiles: list[str] = []

    cred_path = Path.home() / ".aws" / "credentials"
    if cred_path.exists():
        cp = configparser.ConfigParser()
        try:
            cp.read(cred_path)
            profiles.extend(cp.sections())
        except Exception:
            pass

    config_path = Path.home() / ".aws" / "config"
    if config_path.exists():
        cp = configparser.ConfigParser()
        try:
            cp.read(config_path)
            for section in cp.sections():
                name = section[8:] if section.startswith("profile ") else section
                if name not in profiles:
                    profiles.append(name)
        except Exception:
            pass

    return profiles


def _find_working_profile() -> str | None:
    """Return the first AWS CLI profile with valid credentials, or None.

    Bootstrapping IAM Identity Center via its APIs (sso-admin/identitystore)
    needs SOME working AWS credentials to call them with — there's no way to
    call AWS APIs with zero prior credentials. Any already-working profile
    (however it authenticates) is fine for this one-time provisioning step.
    """
    for p in _list_aws_profiles():
        ok, _ = _run(["aws", "sts", "get-caller-identity", "--profile", p, "--output", "text"], timeout=15)
        if ok:
            return p
    return None


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

        console.print(
            "  AtlasOS supports three ways to authenticate with AWS.\n"
            "  [bold]IAM Role[/bold] is recommended for production and EC2/EKS instances.\n"
            "  [bold]Access Keys[/bold] work for local development.\n"
            "  [bold]SSO Profile[/bold] works for teams using AWS SSO.\n"
        )

        if not _confirm("Connect AWS and/or Kubernetes?", default=False):
            console.print(f"  {_SKIP} AWS/K8s skipped")
            return

        console.print()
        console.print("  How is AtlasOS running?\n")
        console.print("  [1] On an EC2 instance or EKS node — use IAM Role [bold](recommended)[/bold]")
        console.print("  [2] On my local laptop — use Access Keys or SSO")
        console.print("  [3] Skip AWS setup for now")
        console.print()

        while True:
            choice = _ask("Choose", default="1")
            if choice in ("1", "2", "3"):
                break
            console.print(f"  {_FAIL} Please enter 1, 2, or 3.")

        if choice == "1":
            self._setup_iam_role()
        elif choice == "2":
            console.print()
            console.print("  [1] AWS Access Key + Secret Key")
            console.print("  [2] AWS SSO Named Profile")
            console.print()
            while True:
                sub = _ask("Choose", default="2")
                if sub in ("1", "2"):
                    break
                console.print(f"  {_FAIL} Please enter 1 or 2.")
            if sub == "1":
                self._setup_access_key()
            else:
                self._setup_sso_profile()
        else:
            console.print(f"  {_SKIP} AWS setup skipped")
            self._record("AWS", False, "skipped")

    def _setup_iam_role(self) -> None:
        console.print()
        console.print(
            "  [bold]IAM Role authentication[/bold] uses the role already attached\n"
            "  to your EC2 instance or EKS node.\n"
            "  No credentials needed. No keys to manage.\n"
            "  [green]Most secure option.[/green]\n"
        )
        region  = _ask("Enter your AWS region (e.g. us-east-1)", default="us-east-1")
        cluster = _ask("Enter your EKS cluster name", default="")

        _set_env("AWS_AUTH_METHOD", "iam_role")
        _set_env("AWS_REGION", region)
        if cluster:
            _set_env("EKS_CLUSTER_NAME", cluster)
        # Clear any leftover static-key/profile config so a stale value from
        # an earlier setup run doesn't silently override the role.
        _set_env("AWS_ACCESS_KEY_ID", "")
        _set_env("AWS_SECRET_ACCESS_KEY", "")
        _set_env("AWS_PROFILE", "")

        self._verify_aws_setup("IAM Role", region, cluster)

    def _setup_access_key(self) -> None:
        console.print()
        console.print(
            f"  {_WARN} [yellow]Access Keys are less secure than IAM Roles.[/yellow]\n"
            "  Keys will be stored in your .env file.\n"
            "  [bold red]NEVER commit .env to Git.[/bold red]\n"
            "  Consider switching to IAM Role when possible (agent aws switch-auth).\n"
        )
        access_key = _ask("AWS Access Key ID", password=True)
        secret_key = _ask("AWS Secret Access Key", password=True)
        region     = _ask("AWS Region", default="us-east-1")
        cluster    = _ask("EKS Cluster Name", default="")

        if not access_key or not secret_key:
            console.print(f"  {_SKIP} Skipped — no key entered")
            self._record("AWS", False, "no key entered")
            return

        _set_env("AWS_AUTH_METHOD", "access_key")
        _set_env("AWS_ACCESS_KEY_ID", access_key)
        _set_env("AWS_SECRET_ACCESS_KEY", secret_key)
        _set_env("AWS_REGION", region)
        if cluster:
            _set_env("EKS_CLUSTER_NAME", cluster)
        _set_env("AWS_PROFILE", "")

        self._verify_aws_setup("Access Key", region, cluster)

    def _bootstrap_sso_backend(self) -> bool:
        """
        Auto-provision what `aws configure sso` needs before it can work:
        an IAM Identity Center instance (standalone accounts only — member
        accounts in an AWS Organization need an org admin instead), a
        permission set, and an account assignment for the caller.

        Uses whatever AWS credentials already work (any existing profile) to
        call the sso-admin/identitystore APIs — there is no way to bootstrap
        this with zero prior AWS credentials. The one thing that can't be
        automated at all: the AWS access portal URL has no API, so the user
        still grabs that from the console once, no matter what.

        Returns True if the instance + permission set are ready (even if the
        user/assignment step needs manual finishing), False only when there
        were no working credentials to bootstrap with at all.
        """
        console.print()
        console.print("  [bold]Checking IAM Identity Center...[/bold]")

        bootstrap_profile = _find_working_profile()
        if not bootstrap_profile:
            console.print(
                f"  {_WARN} No working AWS credentials found to provision this automatically.\n"
                "  [dim]Set up an Access Key first (this wizard's Access Key option), "
                "then re-run 'agent aws switch-auth' to add SSO on top of it.[/dim]"
            )
            return False

        console.print(f"  [dim]Using existing profile '{bootstrap_profile}' to provision Identity Center...[/dim]")

        # 1. Identity Center instance
        ok, out = _run(["aws", "sso-admin", "list-instances", "--profile", bootstrap_profile,
                         "--output", "json"], timeout=15)
        instances = []
        if ok:
            try:
                instances = json.loads(out).get("Instances", [])
            except Exception:
                pass

        if instances:
            instance_arn       = instances[0]["InstanceArn"]
            identity_store_id  = instances[0]["IdentityStoreId"]
            console.print(f"  {_OK} IAM Identity Center already enabled")
        else:
            console.print("  [cyan]Enabling IAM Identity Center (standalone account)...[/cyan]")
            ok2, out2 = _run(["aws", "sso-admin", "create-instance", "--profile", bootstrap_profile,
                               "--output", "json"], timeout=30)
            if not ok2:
                console.print(f"  {_FAIL} Couldn't enable Identity Center: {out2[:200]}")
                console.print(
                    "  [dim]If this account is a member of an AWS Organization, only the "
                    "org admin can enable Identity Center. Otherwise enable it via the "
                    "console: IAM Identity Center → Enable.[/dim]"
                )
                return False
            try:
                data2 = json.loads(out2)
                instance_arn      = data2["InstanceArn"]
                identity_store_id = data2["IdentityStoreId"]
            except Exception:
                console.print(f"  {_FAIL} Unexpected response enabling Identity Center.")
                return False
            console.print(f"  {_OK} IAM Identity Center enabled")

        # 2. Permission set
        ps_name = "AtlasOSAccess"
        ps_arn: str | None = None
        ok3, out3 = _run(["aws", "sso-admin", "list-permission-sets", "--instance-arn", instance_arn,
                           "--profile", bootstrap_profile, "--output", "json"], timeout=15)
        if ok3:
            try:
                for arn in json.loads(out3).get("PermissionSets", []):
                    ok4, out4 = _run([
                        "aws", "sso-admin", "describe-permission-set",
                        "--instance-arn", instance_arn, "--permission-set-arn", arn,
                        "--profile", bootstrap_profile, "--output", "json",
                    ], timeout=15)
                    if ok4 and json.loads(out4).get("PermissionSet", {}).get("Name") == ps_name:
                        ps_arn = arn
                        break
            except Exception:
                pass

        if ps_arn:
            console.print(f"  {_OK} Permission set '{ps_name}' already exists")
        else:
            console.print(f"  [cyan]Creating permission set '{ps_name}'...[/cyan]")
            ok5, out5 = _run([
                "aws", "sso-admin", "create-permission-set",
                "--instance-arn", instance_arn, "--name", ps_name, "--session-duration", "PT4H",
                "--profile", bootstrap_profile, "--output", "json",
            ], timeout=20)
            if not ok5:
                console.print(f"  {_FAIL} Couldn't create permission set: {out5[:200]}")
                return False
            try:
                ps_arn = json.loads(out5)["PermissionSet"]["PermissionSetArn"]
            except Exception:
                console.print(f"  {_FAIL} Unexpected response creating permission set.")
                return False
            ok6, out6 = _run([
                "aws", "sso-admin", "attach-managed-policy-to-permission-set",
                "--instance-arn", instance_arn, "--permission-set-arn", ps_arn,
                "--managed-policy-arn", "arn:aws:iam::aws:policy/AdministratorAccess",
                "--profile", bootstrap_profile,
            ], timeout=20)
            if ok6:
                console.print(f"  {_OK} Permission set '{ps_name}' created with AdministratorAccess")
            else:
                console.print(f"  {_WARN} Permission set created but policy attach failed: {out6[:150]}")

        # 3. Identity Center user + account assignment for the caller
        email = _ask("Your email (for the Identity Center user)", default="")
        if not email:
            console.print(
                f"  {_SKIP} No email given — skipping user/assignment. "
                "Add yourself as a user in the console before running 'aws configure sso'."
            )
            return True  # instance + permission set are still ready and reusable

        user_id: str | None = None
        ok7, out7 = _run(["aws", "identitystore", "list-users", "--identity-store-id", identity_store_id,
                           "--profile", bootstrap_profile, "--output", "json"], timeout=15)
        if ok7:
            try:
                for u in json.loads(out7).get("Users", []):
                    if any(e.get("Value", "").lower() == email.lower() for e in u.get("Emails", [])):
                        user_id = u["UserId"]
                        break
            except Exception:
                pass

        if user_id:
            console.print(f"  {_OK} Identity Center user found for {email}")
        else:
            username = email.split("@")[0]
            console.print(f"  [cyan]Creating Identity Center user for {email}...[/cyan]")
            ok8, out8 = _run([
                "aws", "identitystore", "create-user", "--identity-store-id", identity_store_id,
                "--user-name", username,
                "--name", f"GivenName={username},FamilyName=user",
                "--display-name", username,
                "--emails", f"Value={email},Type=work,Primary=true",
                "--profile", bootstrap_profile, "--output", "json",
            ], timeout=20)
            if not ok8:
                console.print(f"  {_FAIL} Couldn't create Identity Center user: {out8[:200]}")
                console.print("  [dim]Add yourself as a user manually in the console, then continue.[/dim]")
                return True
            try:
                user_id = json.loads(out8)["UserId"]
            except Exception:
                console.print(f"  {_FAIL} Unexpected response creating user.")
                return True
            console.print(f"  {_OK} Identity Center user created")

        acct_ok, acct_out = _run(["aws", "sts", "get-caller-identity", "--profile", bootstrap_profile,
                                   "--query", "Account", "--output", "text"], timeout=15)
        account_id = acct_out.strip() if acct_ok else ""

        if account_id and user_id:
            ok9, out9 = _run([
                "aws", "sso-admin", "create-account-assignment",
                "--instance-arn", instance_arn, "--target-id", account_id, "--target-type", "AWS_ACCOUNT",
                "--permission-set-arn", ps_arn, "--principal-type", "USER", "--principal-id", user_id,
                "--profile", bootstrap_profile, "--output", "json",
            ], timeout=20)
            if ok9 or "conflict" in out9.lower() or "already" in out9.lower():
                console.print(f"  {_OK} Account access assigned")
            else:
                console.print(f"  {_WARN} Account assignment may have failed: {out9[:150]}")

        console.print(
            f"\n  {_OK} Identity Center is ready. Grab your portal URL from:\n"
            "  [dim]AWS Console → IAM Identity Center → Dashboard → 'AWS access portal URL'[/dim]\n"
        )
        return True

    def _setup_sso_profile(self) -> None:
        profiles = _list_aws_profiles()

        if profiles:
            console.print("  Found AWS profiles on this machine:")
            for i, p in enumerate(profiles, 1):
                console.print(f"    [{i}] use existing profile named '{p}'")
            console.print(f"    [bold cyan]N[/bold cyan] create a brand-new profile")
            console.print()
            while True:
                pick = _ask(
                    "Type a NUMBER to reuse a profile above, or the LETTER N to create a new one",
                    default="1",
                )
                pick = pick.strip()
                if pick.isdigit() and 1 <= int(pick) <= len(profiles):
                    profile = profiles[int(pick) - 1]
                    break
                if pick.lower() in ("n", "new"):
                    profile = _ask("Name for the new profile", default="agentic-os")
                    break
                console.print(
                    f"  {_WARN} '{pick}' isn't one of the options above — "
                    f"type a number (1-{len(profiles)}) or the letter N."
                )
        else:
            console.print(f"  {_WARN} No AWS CLI profiles found on this machine.")
            profile = _ask("New profile name (from ~/.aws/config)", default="agentic-os")

        just_configured = False
        if profile not in profiles:
            console.print(f"\n  Profile '{profile}' doesn't exist yet.")

            if _confirm(
                "  Auto-provision IAM Identity Center (permission set + account "
                "access) now?", default=True,
            ):
                self._bootstrap_sso_backend()

            if _confirm(f"  Run 'aws configure sso --profile {profile}' now?", default=True):
                console.print(
                    "  [dim]This opens a browser to sign in. Need the portal URL? "
                    "AWS Console → IAM Identity Center → Dashboard → "
                    "'AWS access portal URL' (top of page).[/dim]"
                )
                subprocess.run(["aws", "configure", "sso", "--profile", profile])
                just_configured = True
            else:
                console.print(
                    f"  {_SKIP} Skipped — run it yourself later: aws configure sso --profile {profile}"
                )
                self._record("AWS", False, "profile not configured")
                return

        if not just_configured:
            console.print(f"  [dim]Running: aws sso login --profile {profile}[/dim]")
            subprocess.run(["aws", "sso", "login", "--profile", profile])

        region  = _ask("AWS Region", default="us-east-1")
        cluster = _ask("EKS Cluster Name", default="")

        _set_env("AWS_AUTH_METHOD", "sso_profile")
        _set_env("AWS_PROFILE", profile)
        _set_env("AWS_REGION", region)
        if cluster:
            _set_env("EKS_CLUSTER_NAME", cluster)
        _set_env("AWS_ACCESS_KEY_ID", "")
        _set_env("AWS_SECRET_ACCESS_KEY", "")

        self._verify_aws_setup("SSO Profile", region, cluster)

    # ── Shared verification for ALL three AWS auth methods ─────────────────

    def _verify_aws_setup(self, method_label: str, region: str, cluster: str) -> None:
        """
        Runs the same 5 checks regardless of method. Relies on _set_env()
        having already applied the relevant AWS_* vars to os.environ (it
        mutates the live process env, not just the .env file), so every
        subprocess call below — 'aws' CLI and kubectl alike — naturally
        picks up whichever method was just configured.
        """
        console.print()
        console.print(f"  [bold]Verifying {method_label} setup…[/bold]\n")

        results: list[tuple[str, bool]] = []

        # 1. AWS identity
        ok1, out1 = _run(["aws", "sts", "get-caller-identity", "--output", "json"], timeout=20)
        identity: dict = {}
        if ok1:
            try:
                identity = json.loads(out1)
            except Exception:
                ok1 = False
        if ok1:
            console.print(f"  {_OK} AWS identity verified")
            console.print(f"      Account: {identity.get('Account', '?')}")
            console.print(f"      ARN:     {identity.get('Arn', '?')}")
        else:
            console.print(f"  {_FAIL} AWS identity check failed")
            console.print(f"      {out1[:200]}")
            if method_label == "IAM Role":
                console.print(
                    "      [dim]No role attached? EC2 Console → your instance → "
                    "Actions → Security → Modify IAM role → attach a role with "
                    "EKS/EC2/cost-explorer permissions.[/dim]"
                )
        results.append(("AWS identity verified", ok1))

        if not ok1:
            self._record("AWS", False, out1[:150])
            return

        account_id = identity.get("Account", "?")
        arn        = identity.get("Arn", "?")

        if not cluster:
            console.print(f"\n  {_SKIP} No EKS cluster name given — skipping cluster checks.")
            self._record("AWS", True, f"{method_label}: identity ok, no cluster configured")
            return

        # 2. EKS access
        ok2, out2 = _run([
            "aws", "eks", "describe-cluster", "--name", cluster, "--region", region,
            "--query", "cluster.{status:status,version:version,endpoint:endpoint}",
            "--output", "json",
        ], timeout=20)
        console.print(f"  {_OK if ok2 else _FAIL} EKS cluster accessible" if ok2
                       else f"  {_FAIL} EKS cluster check failed: {out2[:150]}")
        results.append(("EKS cluster accessible", ok2))

        # 3. Kubeconfig update
        with console.status("  Updating kubeconfig…"):
            ok3, out3 = _run(["aws", "eks", "update-kubeconfig", "--name", cluster, "--region", region], timeout=30)
        console.print(f"  {_OK if ok3 else _FAIL} Kubeconfig updated" if ok3
                       else f"  {_FAIL} Kubeconfig update failed: {out3[:150]}")
        results.append(("Kubeconfig updated", ok3))

        # 4. kubectl connectivity
        ok4, out4 = _run(["kubectl", "get", "nodes", "--no-headers"], timeout=15)
        node_lines = [l for l in out4.splitlines() if l.strip()] if ok4 else []
        console.print(f"  {_OK} kubectl connectivity confirmed — {len(node_lines)} node(s) ready" if ok4
                       else f"  {_FAIL} kubectl connectivity failed: {out4[:150]}")
        results.append(("kubectl connectivity confirmed", ok4))

        # 5. K8s RBAC permissions
        ok5, out5 = _run(["kubectl", "auth", "can-i", "get", "pods", "--all-namespaces"], timeout=15)
        if ok5:
            console.print(f"  {_OK} K8s RBAC permissions verified")
        else:
            console.print(f"  {_FAIL} K8s RBAC permissions denied")
            console.print(
                "      [dim]This identity isn't recognized by the cluster's RBAC "
                "(aws-auth ConfigMap / EKS access entries). Run 'agent k8s scan' "
                "for the exact remediation command.[/dim]"
            )
        results.append(("K8s RBAC permissions verified", ok5))

        all_ok = all(ok for _, ok in results)

        summary = [
            f"  Method:    {method_label}",
            f"  Account:   {account_id}",
            f"  Identity:  {arn}",
            f"  Region:    {region}",
            f"  Cluster:   {cluster}",
        ]
        if ok4:
            summary.append(f"  Nodes:     {len(node_lines)} ready")
        summary.append("")
        summary += [f"  {_OK if ok else _FAIL} {label}" for label, ok in results]

        console.print()
        console.print(Panel(
            "\n".join(summary),
            title="[bold]AWS CONFIGURATION COMPLETE[/bold]" if all_ok
                  else "[bold]AWS CONFIGURATION — ISSUES FOUND[/bold]",
            border_style="green" if all_ok else "yellow",
            padding=(1, 2),
        ))
        console.print()

        self._record("AWS", all_ok, f"{method_label}: {arn}")

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

    # ── 6. Jenkins CI/CD ─────────────────────────────────────────────────────

    def configure_jenkins(self) -> None:
        _section("Step 6 — Jenkins CI/CD (optional)")
        console.print(
            "  Monitor Jenkins builds/agents/queue and auto-heal known-safe failures "
            "(flaky tests, stuck builds, stuck queue items).\n"
        )

        if not _confirm("Configure Jenkins integration?", default=False):
            console.print(f"  {_SKIP} Jenkins skipped")
            return

        url = _ask("Jenkins URL (e.g. https://jenkins.company.com)")
        if not url:
            console.print(f"  {_SKIP} Jenkins skipped — no URL provided")
            return

        user = _ask("Jenkins username")
        console.print(
            "  [dim]Need a token? Jenkins → click your username (top right) → "
            "Configure → API Token → Add new Token.[/dim]"
        )
        token = _ask("Jenkins API token (NOT your password)", password=True)

        if not user or not token:
            console.print(f"  {_SKIP} Jenkins skipped — username and API token are both required")
            return

        _set_env("JENKINS_URL", url.rstrip("/"))
        _set_env("JENKINS_USER", user)
        _set_env("JENKINS_API_TOKEN", token)

        console.print()
        with console.status("  Testing Jenkins connection…"):
            try:
                from agent.integrations import jenkins as jk
                info = jk.get_connection_info()
                ok = info.connected
                detail = (
                    f"version {info.version}, {info.num_executors} executor(s), "
                    f"{info.node_count} node(s)"
                    if ok else info.error
                )
            except Exception as exc:
                ok, detail = False, str(exc)[:150]

        icon = _OK if ok else _FAIL
        console.print(f"  {icon} Jenkins: {detail}")
        self._record("Jenkins", ok, detail)

        if not ok:
            return

        if _confirm(
            "\n  Enable Jenkins auto-healing? (flaky tests and stuck builds get "
            "auto-fixed without asking)", default=False,
        ):
            _set_env("JENKINS_AUTO_HEAL", "true")
            console.print(f"  {_OK} Auto-healing enabled — see `agent jenkins watch --auto-fix`")
        else:
            _set_env("JENKINS_AUTO_HEAL", "false")
            console.print(f"  {_SKIP} Auto-healing off — you'll review fixes with `agent jenkins heal`")

    # ── 7. Custom integrations ────────────────────────────────────────────────

    def configure_custom(self) -> None:
        _section("Step 7 — Custom Integrations")

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

    # ── 8. Default settings ───────────────────────────────────────────────────

    def configure_defaults(self) -> None:
        _section("Step 8 — Default Settings")

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
            self.configure_jenkins()
            self.configure_custom()
            self.configure_defaults()
        except KeyboardInterrupt:
            console.print("\n\n  [yellow]Setup interrupted.[/yellow]")
            console.print("  [dim]Run[/dim] [cyan]agent setup[/cyan] [dim]to continue.[/dim]\n")
        finally:
            _flush_env()  # single atomic write — no scrambled values

        self.show_summary()
