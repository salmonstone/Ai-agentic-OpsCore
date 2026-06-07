"""
TLS Monitor skill — auto-discovers every domain running in the cluster,
checks certificate health, diagnoses problems, and fixes only after
asking permission.

No domains are hardcoded — the skill reads all Ingress resources across
all namespaces and audits each one.
"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor, wait

from agent.core import llm
from agent.core.models import (
    CertInfo, IngressInfo, TLSMonitorDiagnosis, TLSProblemType, TLSSecretInfo,
)
from agent.integrations.kubectl import (
    check_ingress_controller,
    get_all_ingresses,
    get_cert_manager_certificates,
    get_cert_manager_issuers,
    get_tls_secret,
    run_tls_fix,
)
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SCAN_SYSTEM = """\
You are a Kubernetes TLS/SSL expert. You receive information about all ingresses and
certificates in a cluster. Return ONLY valid JSON — no markdown fences. Schema:
{
  "summary": "<2-3 sentence overview>",
  "diagnoses": [
    {
      "domain": "<domain>",
      "problem_type": "<TLSProblemType value>",
      "root_cause": "<technical cause>",
      "suggested_fix": "<plain-language fix>",
      "fix_command": "<kubectl command or null>",
      "fix_type": "<restart_cert|annotate_ingress|apply_manifest|none>",
      "confidence": "high|medium|low",
      "explanation": "<why this fix works>"
    }
  ]
}
Valid TLSProblemType values: CertExpired, CertExpiringSoon, CertManagerNotInstalled,
CertManagerNotRunning, CertificateNotReady, IssuerNotReady, AcmeChallengeFailing,
SecretMissing, SecretInvalid, HostnameMismatch, RateLimitHit, SelfSigned,
WrongIssuerRef, NoTLSConfigured, WrongDomain, IngressMisconfigured, Healthy, Unknown.
Focus on actionable, specific fixes.
"""


class TLSMonitorSkill(BaseSkill):
    """Auto-discover all cluster domains and audit their TLS certificate health."""

    @property
    def name(self) -> str:
        return "tls-monitor"

    @property
    def description(self) -> str:
        return "Auto-discover every domain in the cluster and audit TLS certificate health."

    def execute(self, action: str = "scan", **kwargs) -> dict:
        if action == "scan":
            return self.scan()
        if action == "fix":
            return self.fix(kwargs["diagnosis"], confirmed=kwargs.get("confirmed", False))
        return self.scan()

    # ------------------------------------------------------------------
    # scan — discover all domains, check each one
    # ------------------------------------------------------------------

    def scan(self) -> dict:
        from rich.console import Console
        from rich.panel  import Panel
        from rich.rule   import Rule
        from rich.progress import Progress, SpinnerColumn, TextColumn

        console = Console()
        console.print(Rule("[bold cyan]TLS Monitor — Auto-discovering all domains[/bold cyan]"))
        console.print()

        with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                      console=console, transient=True) as prog:
            t = prog.add_task("Discovering ingresses, secrets, and certificates...", total=None)

            # Parallel data collection
            with ThreadPoolExecutor(max_workers=4) as pool:
                f_ingresses = pool.submit(get_all_ingresses)
                f_certs     = pool.submit(get_cert_manager_certificates)
                f_issuers   = pool.submit(get_cert_manager_issuers)
                f_ctrl      = pool.submit(check_ingress_controller)
                done, _     = wait([f_ingresses, f_certs, f_issuers, f_ctrl], timeout=30)

            ingresses = f_ingresses.result() if f_ingresses in done else []
            cm_certs  = f_certs.result()     if f_certs  in done else []
            issuers   = f_issuers.result()   if f_issuers in done else []
            ctrl      = f_ctrl.result()      if f_ctrl   in done else {}

            prog.update(t, description="Fetching TLS secrets...")

            # Fetch TLS secret for each ingress that has one
            secret_cache: dict[str, TLSSecretInfo | None] = {}
            for ing in ingresses:
                if ing.tls_secret and ing.tls_enabled:
                    key = f"{ing.namespace}/{ing.tls_secret}"
                    if key not in secret_cache:
                        secret_cache[key] = get_tls_secret(ing.tls_secret, ing.namespace)

            prog.update(t, description="Analysing with Claude...")

            # Build cert-manager index by domain
            cm_by_domain: dict[str, CertInfo] = {}
            for c in cm_certs:
                cm_by_domain[c.domain] = c

            # Build prompt
            prompt = _build_scan_prompt(ingresses, secret_cache, cm_certs, issuers, ctrl)

            try:
                raw = asyncio.run(llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    system=_SCAN_SYSTEM,
                    json_mode=True,
                    max_tokens=2000,
                ))
                parsed = _parse_json(raw.content)
            except Exception as exc:
                log.warning("tls_monitor.scan.llm_failed", error=str(exc))
                parsed = {"summary": "LLM unavailable — raw data shown below.",
                          "diagnoses": []}

        summary    = parsed.get("summary", "")
        raw_diags  = parsed.get("diagnoses", [])

        # Enrich diagnoses with actual data
        diagnoses: list[TLSMonitorDiagnosis] = []
        for d in raw_diags:
            domain = d.get("domain", "")
            ing    = next((i for i in ingresses if i.domain == domain), None)
            if ing is None and ingresses:
                ing = ingresses[0]  # fallback
            if ing is None:
                continue

            key      = f"{ing.namespace}/{ing.tls_secret}" if ing.tls_secret else ""
            cert_inf = secret_cache.get(key)

            diagnoses.append(TLSMonitorDiagnosis(
                domain          = domain,
                ingress         = ing,
                cert_info       = cert_inf,
                problem_type    = d.get("problem_type", TLSProblemType.UNKNOWN),
                root_cause      = d.get("root_cause", ""),
                suggested_fix   = d.get("suggested_fix", ""),
                fix_command     = d.get("fix_command"),
                fix_type        = d.get("fix_type", "none"),
                confidence      = d.get("confidence", "low"),
                explanation     = d.get("explanation", ""),
                claude_analysis = summary,
            ))

        # Render results
        _render_scan(console, ingresses, diagnoses, secret_cache, ctrl, summary)

        return {
            "ingresses":  len(ingresses),
            "diagnoses":  diagnoses,
            "summary":    summary,
            "controller": ctrl,
        }

    # ------------------------------------------------------------------
    # fix — apply a specific fix after permission granted
    # ------------------------------------------------------------------

    def fix(self, diagnosis: TLSMonitorDiagnosis, confirmed: bool = False) -> dict:
        from rich.console import Console
        console = Console()

        if not confirmed:
            console.print(
                f"\n  [bold]Fix:[/bold] {diagnosis.suggested_fix}\n"
                f"  [dim]Command:[/dim] [cyan]{diagnosis.fix_command or 'n/a'}[/cyan]\n"
            )
            return {"ok": False, "reason": "not_confirmed"}

        fix_type = diagnosis.fix_type
        ing      = diagnosis.ingress

        params: dict = {
            "namespace": ing.namespace,
            "ingress":   ing.name,
            "name":      ing.tls_secret or "",
        }

        if fix_type == "none" and diagnosis.fix_command:
            # Generic kubectl apply
            from agent.integrations.kubectl import apply_fix
            r = apply_fix(diagnosis.fix_command)
            ok = r.success
        else:
            r  = run_tls_fix(fix_type, params)
            ok = r.success

        if ok:
            console.print(f"  [bold green]✓ Fix applied for {diagnosis.domain}[/bold green]")
        else:
            console.print(f"  [bold red]✗ Fix failed:[/bold red] {r.error[:120]}")

        return {"ok": ok, "domain": diagnosis.domain, "output": r.output}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_scan_prompt(
    ingresses:    list[IngressInfo],
    secret_cache: dict[str, TLSSecretInfo | None],
    cm_certs:     list[CertInfo],
    issuers:      list[str],
    ctrl:         dict,
) -> str:
    lines = ["=== CLUSTER TLS STATE ===", ""]

    lines.append(f"Ingress controller: {ctrl.get('type','unknown')} "
                 f"status={ctrl.get('status','unknown')} "
                 f"ns={ctrl.get('namespace','?')}")
    lines.append(f"Available ClusterIssuers: {issuers or ['none']}")
    lines.append(f"cert-manager Certificates: {len(cm_certs)}")
    lines.append("")

    for ing in ingresses:
        lines.append(f"--- INGRESS: {ing.namespace}/{ing.name} ---")
        lines.append(f"  domain:    {ing.domain or '(no host)'}")
        lines.append(f"  address:   {ing.address or '<pending>'}")
        lines.append(f"  tls:       {ing.tls_enabled}")
        lines.append(f"  tls_secret: {ing.tls_secret or 'none'}")
        lines.append(f"  backend:   {ing.backend_service or '?'}")
        lines.append(f"  age:       {ing.age}")

        if ing.tls_secret and ing.tls_enabled:
            key = f"{ing.namespace}/{ing.tls_secret}"
            si  = secret_cache.get(key)
            if si:
                lines.append(f"  cert_type:        {si.cert_type}")
                lines.append(f"  cert_issuer:      {si.issuer}")
                lines.append(f"  cert_domain:      {si.domain}")
                lines.append(f"  cert_expiry:      {si.expiry_date}")
                lines.append(f"  days_until_expiry: {si.days_until_expiry}")
                lines.append(f"  is_expired:       {si.is_expired}")
                lines.append(f"  is_expiring_soon: {si.is_expiring_soon}")
            else:
                lines.append("  cert_secret: SECRET NOT FOUND")

        # cert-manager Certificate for this domain
        cm = next((c for c in cm_certs if c.domain == ing.domain), None)
        if cm:
            lines.append(f"  cert_manager_ready:   {cm.ready}")
            lines.append(f"  cert_manager_status:  {cm.status}")
            lines.append(f"  cert_manager_message: {cm.message}")
            lines.append(f"  cert_manager_expiry:  {cm.expiry}")
            lines.append(f"  cert_manager_issuer:  {cm.issuer}")
        lines.append("")

    return "\n".join(lines)


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text  = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(text)


_SEVERITY_MAP = {
    "CertExpired":            ("critical", "bold red"),
    "CertExpiringSoon":       ("warning",  "yellow"),
    "CertManagerNotInstalled":("critical", "bold red"),
    "CertManagerNotRunning":  ("critical", "bold red"),
    "CertificateNotReady":    ("critical", "red"),
    "SecretMissing":          ("critical", "bold red"),
    "NoTLSConfigured":        ("warning",  "yellow"),
    "AcmeChallengeFailing":   ("critical", "red"),
    "IssuerNotReady":         ("critical", "red"),
    "HostnameMismatch":       ("warning",  "yellow"),
    "WrongDomain":            ("warning",  "yellow"),
    "IngressMisconfigured":   ("warning",  "yellow"),
    "SelfSigned":             ("info",     "cyan"),
    "Healthy":                ("info",     "green"),
    "Unknown":                ("info",     "dim"),
}


def _render_scan(
    console,
    ingresses:    list[IngressInfo],
    diagnoses:    list[TLSMonitorDiagnosis],
    secret_cache: dict,
    ctrl:         dict,
    summary:      str,
) -> None:
    from rich.panel import Panel
    from rich.table import Table
    from rich.rule  import Rule

    # Controller status
    ctrl_color = {"running": "green", "degraded": "yellow",
                  "down": "red", "not_found": "red"}.get(ctrl.get("status", ""), "dim")
    console.print(
        f"  Ingress controller: [{ctrl_color}]{ctrl.get('type','unknown')} "
        f"({ctrl.get('status','?')})[/{ctrl_color}]  "
        f"[dim]{ctrl.get('namespace','')}[/dim]"
    )
    console.print(f"  Domains found: [bold cyan]{len(ingresses)}[/bold cyan]")
    console.print()

    if summary:
        console.print(Panel(summary, title="[bold]AI Analysis[/bold]",
                            border_style="dim", padding=(0, 1)))
        console.print()

    if not ingresses:
        console.print("[dim]No Ingress resources found in any namespace.[/dim]")
        return

    # Domain table
    tbl = Table(show_header=True, header_style="bold magenta", show_lines=True)
    tbl.add_column("Domain",    min_width=26)
    tbl.add_column("Namespace", width=16)
    tbl.add_column("TLS",       justify="center", width=5)
    tbl.add_column("Secret",    width=22)
    tbl.add_column("Issuer",    width=18)
    tbl.add_column("Expires",   width=12)
    tbl.add_column("Status",    width=22)

    for ing in ingresses:
        key = f"{ing.namespace}/{ing.tls_secret}" if ing.tls_secret else ""
        si  = secret_cache.get(key) if key else None

        tls_icon   = "[green]✓[/green]" if ing.tls_enabled else "[red]✗[/red]"
        secret_str = ing.tls_secret or "[dim]none[/dim]"
        issuer_str = si.issuer[:16] if si and si.issuer else "[dim]—[/dim]"
        expiry_str = si.expiry_date if si and si.expiry_date else "[dim]—[/dim]"

        if si:
            if si.is_expired:
                exp_color = "bold red"
            elif si.is_expiring_soon:
                exp_color = "yellow"
            else:
                exp_color = "green"
            expiry_str = f"[{exp_color}]{expiry_str}[/{exp_color}]"

        # Find matching diagnosis
        diag = next((d for d in diagnoses if d.domain == ing.domain), None)
        if diag:
            sev, color = _SEVERITY_MAP.get(diag.problem_type, ("info", "dim"))
            status_str = f"[{color}]{diag.problem_type}[/{color}]"
        elif not ing.tls_enabled:
            status_str = "[yellow]No TLS[/yellow]"
        elif si and not si.is_expired and not si.is_expiring_soon:
            status_str = "[green]Healthy[/green]"
        else:
            status_str = "[dim]Unknown[/dim]"

        tbl.add_row(
            ing.domain or "[dim](no host)[/dim]",
            ing.namespace,
            tls_icon,
            secret_str,
            issuer_str,
            expiry_str,
            status_str,
        )

    console.print(tbl)
    console.print()

    # Issue details
    problems = [d for d in diagnoses
                if d.problem_type not in ("Healthy", "Unknown")]
    if not problems:
        console.print(Panel("[bold green]All domains have valid TLS certificates.[/bold green]",
                            border_style="green"))
        return

    console.print(Rule(f"[bold red]{len(problems)} issue(s) found[/bold red]"))
    console.print()

    for i, d in enumerate(problems, 1):
        _, color = _SEVERITY_MAP.get(d.problem_type, ("info", "dim"))
        divider  = "[dim]" + "-" * 56 + "[/dim]"
        lines = [
            f"[dim]Domain    :[/dim] [bold cyan]{d.domain}[/bold cyan]  "
            f"[dim]{d.ingress.namespace}/{d.ingress.name}[/dim]",
            f"[dim]Problem   :[/dim] [{color}]{d.problem_type}[/{color}]  "
            f"[dim]confidence={d.confidence}[/dim]",
            f"[dim]Root cause:[/dim] {d.root_cause}",
            divider,
            f"[dim]Fix       :[/dim] {d.suggested_fix}",
        ]
        if d.fix_command:
            lines += [divider,
                      f"[dim]Command   :[/dim] [bold cyan]{d.fix_command}[/bold cyan]"]
        if d.cert_info:
            si = d.cert_info
            lines.append(f"[dim]Cert      :[/dim] {si.cert_type}  "
                         f"issuer={si.issuer}  "
                         f"expires={si.expiry_date}  "
                         f"days_left={si.days_until_expiry}")

        console.print(Panel(
            "\n".join(lines),
            title=f"[{color}]{i}/{len(problems)}  {d.domain}[/{color}]",
            border_style=color,
            padding=(1, 2),
        ))
        console.print()

    console.print(
        "  [dim]To fix interactively:[/dim]  "
        "[bold]agent tls monitor --fix[/bold]"
    )
