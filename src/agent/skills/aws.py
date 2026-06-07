from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import AwsDiagnosis, AwsProblemType, AwsResource, AwsResourceType
from agent.core.parsing import LLMParseError, parse_llm_json
from agent.integrations.aws import (
    get_alb_detail,
    get_ec2_detail,
    get_rds_detail,
    get_unhealthy_alb,
    get_unhealthy_ec2,
    get_unhealthy_rds,
    run_aws_fix,
)
from agent.memory.retrieval import remember, retrieve_context
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

_MAX_PARALLEL_DIAGNOSES = 5   # max concurrent LLM calls
_MAX_RESOURCES_PER_SCAN = 20  # cost guard — never diagnose more than this at once
_RECHECK_WAIT_SECS      = 15  # how long to wait before rechecking after apply_fix

# ---------------------------------------------------------------------------
# Statuses unambiguous from AWS API alone — no LLM needed for confidence
# ---------------------------------------------------------------------------

_HIGH_CONFIDENCE_PROBLEMS = {
    AwsProblemType("Ec2Stopped"),
    AwsProblemType("RdsStopped"),
    AwsProblemType("AlbNoTargets"),
}

_SAFE_TO_AUTO_FIX = {
    AwsProblemType("Ec2Stopped"),
    AwsProblemType("Ec2StatusFailed"),
    AwsProblemType("RdsStopped"),
}

_UNSAFE_REASONS = {
    AwsProblemType("RdsStorageFull"):     "Storage resize needs human decision on new size",
    AwsProblemType("RdsConnectionsAtMax"): "Parameter group change needs human review",
    AwsProblemType("AlbUnhealthyTargets"): "ALB issues need application-level investigation",
    AwsProblemType("AlbNoTargets"):        "No targets — application deployment issue",
    AwsProblemType("Ec2CpuCreditExhausted"): "CPU credit exhaustion — instance type change needed",
    AwsProblemType("Unknown"):             "Problem unclear — manual investigation required",
}

_SYSTEM = """You are an AWS cloud engineer and SRE expert.
Analyze the AWS resource information and diagnose the root cause.
Return JSON only with this exact structure:
{
  "problem_type": one of exactly these strings: "Ec2Stopped" | "Ec2StatusFailed" | "Ec2CpuCreditExhausted" | "RdsStopped" | "RdsStorageFull" | "RdsConnectionsAtMax" | "AlbUnhealthyTargets" | "AlbNoTargets" | "Unknown",
  "root_cause": "string — clear one-paragraph explanation of the root cause",
  "suggested_fix": "string — what to do to resolve this",
  "fix_command": "string — exact aws CLI command to run, or null if no single command applies",
  "confidence": "high" | "medium" | "low",
  "explanation": "string — detailed technical explanation"
}

Rules for problem_type:
- "Ec2Stopped": instance is in stopped/stopping state
- "Ec2StatusFailed": instance is running but failing system or instance status checks
- "Ec2CpuCreditExhausted": T-series instance has exhausted its CPU credit balance
- "RdsStopped": RDS instance is stopped or stopping
- "RdsStorageFull": RDS instance has storage-full status or storage nearly at capacity
- "RdsConnectionsAtMax": RDS is rejecting connections due to max_connections limit
- "AlbUnhealthyTargets": ALB target group has one or more unhealthy targets
- "AlbNoTargets": ALB target group has zero registered targets
- "Unknown": resource is healthy, problem is unclear, or does not fit any category above

Rules for fix_command:
- EC2 stopped:      "aws ec2 start-instances --instance-ids <id> --region <region>"
- EC2 status failed:"aws ec2 reboot-instances --instance-ids <id> --region <region>"
- RDS stopped:      "aws rds start-db-instance --db-instance-identifier <id>"
- ALB issues:       null (require manual investigation of the application)
- RDS storage full: null (requires storage modification — provide instructions in suggested_fix)
- RDS connections:  null (requires parameter group change — provide instructions in suggested_fix)

Rules for confidence:
- "high": root cause is unambiguous (stopped, no targets)
- "medium": likely root cause but evidence is incomplete
- "low": resource appears healthy or evidence is too sparse

If the resource appears healthy, set problem_type to "Unknown", confidence to "low", fix_command to null."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_problem_type(raw: str) -> AwsProblemType:
    try:
        return AwsProblemType(raw)
    except ValueError:
        return AwsProblemType.UNKNOWN


def _gather_detail(resource: AwsResource) -> str:
    """Fetch resource-specific detail from AWS API."""
    try:
        if resource.resource_type == AwsResourceType.EC2:
            d = get_ec2_detail(resource.id, resource.region)
        elif resource.resource_type == AwsResourceType.RDS:
            d = get_rds_detail(resource.id, resource.region)
        elif resource.resource_type == AwsResourceType.ALB:
            d = get_alb_detail(resource.id, resource.region)
        else:
            d = {}
        return d.get("detail", "(no detail available)")
    except Exception as exc:
        log.warning("aws.gather_detail.failed",
                    resource_id=resource.id, error=str(exc)[:120])
        return f"(detail fetch failed: {exc})"


def _recheck_resource(resource_id: str, resource_type: AwsResourceType,
                      region: str) -> str:
    """
    Wait, then return the current status of the resource.
    Used after apply_fix to verify the fix actually worked.
    """
    import subprocess
    time.sleep(_RECHECK_WAIT_SECS)

    try:
        if resource_type == AwsResourceType.EC2:
            r = subprocess.run(
                ["aws", "ec2", "describe-instances",
                 "--instance-ids", resource_id,
                 "--region", region,
                 "--query", "Reservations[0].Instances[0].State.Name",
                 "--output", "text"],
                capture_output=True, text=True, timeout=15,
            )
            return r.stdout.strip() if r.returncode == 0 else "unknown"

        if resource_type == AwsResourceType.RDS:
            r = subprocess.run(
                ["aws", "rds", "describe-db-instances",
                 "--db-instance-identifier", resource_id,
                 "--region", region,
                 "--query", "DBInstances[0].DBInstanceStatus",
                 "--output", "text"],
                capture_output=True, text=True, timeout=15,
            )
            return r.stdout.strip() if r.returncode == 0 else "unknown"

    except Exception as exc:
        log.warning("aws.recheck.failed", resource_id=resource_id, error=str(exc))

    return "unknown"


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class AwsSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "aws-diagnose"

    @property
    def description(self) -> str:
        return "Scan AWS account for unhealthy resources and diagnose root causes using Claude."

    def execute(self, input_data: dict) -> dict:
        mode = input_data.get("mode", "scan")
        if mode == "scan":
            region   = input_data.get("region", "ap-south-1")
            services = input_data.get("services", ["ec2", "rds", "alb"])
            limit    = input_data.get("limit", _MAX_RESOURCES_PER_SCAN)
            diagnoses = self.scan_account(region, services, limit=limit)
            return {"diagnoses": [d.model_dump() for d in diagnoses]}
        if mode == "diagnose":
            resource  = AwsResource(**input_data["resource"])
            diagnosis = self.diagnose_resource(resource)
            return diagnosis.model_dump()
        if mode == "fix":
            diagnosis = AwsDiagnosis(**input_data["diagnosis"])
            result    = self.apply_fix(diagnosis)
            return result
        if mode == "auto-fix":
            region   = input_data.get("region", "ap-south-1")
            services = input_data.get("services", ["ec2", "rds", "alb"])
            limit    = input_data.get("limit", _MAX_RESOURCES_PER_SCAN)
            return self.auto_fix_all(region, services, limit=limit)
        raise ValueError(f"Unknown mode: {mode!r}")

    # ------------------------------------------------------------------
    # scan_account — parallel diagnosis with cost guard
    # ------------------------------------------------------------------

    def scan_account(
        self,
        region:   str = "ap-south-1",
        services: list[str] | None = None,
        limit:    int = _MAX_RESOURCES_PER_SCAN,
    ) -> list[AwsDiagnosis]:
        if services is None:
            services = ["ec2", "rds", "alb"]

        resources: list[AwsResource] = []
        if "ec2" in services: resources += get_unhealthy_ec2(region)
        if "rds" in services: resources += get_unhealthy_rds(region)
        if "alb" in services: resources += get_unhealthy_alb(region)

        # Cost guard — never fire more than `limit` LLM calls per scan
        if len(resources) > limit:
            log.warning("aws.scan_account.capped",
                        total=len(resources), limit=limit)
            resources = resources[:limit]

        log.info("aws.scan_account",
                 region=region, unhealthy=len(resources), parallel=_MAX_PARALLEL_DIAGNOSES)

        if not resources:
            return []

        # Parallel diagnosis — up to _MAX_PARALLEL_DIAGNOSES at once
        diagnoses: list[AwsDiagnosis] = []
        with ThreadPoolExecutor(max_workers=_MAX_PARALLEL_DIAGNOSES) as pool:
            futures = {
                pool.submit(self.diagnose_resource, r): r
                for r in resources
            }
            for future in as_completed(futures):
                resource = futures[future]
                try:
                    diagnoses.append(future.result())
                except Exception as exc:
                    log.error("aws.scan_account.diagnose_failed",
                              resource_id=resource.id, error=str(exc)[:200])
                    # Add a placeholder so the scan doesn't silently skip resources
                    diagnoses.append(AwsDiagnosis(
                        resource_id=resource.id,
                        resource_name=resource.name,
                        resource_type=resource.resource_type,
                        region=resource.region,
                        problem_type=AwsProblemType.UNKNOWN,
                        root_cause=f"Diagnosis failed: {exc}",
                        suggested_fix="Check resource manually in AWS console.",
                        fix_command=None,
                        confidence="low",
                        explanation="Automated diagnosis encountered an error.",
                    ))

        # Sort: critical (high confidence) first
        diagnoses.sort(
            key=lambda d: (
                0 if d.problem_type in _HIGH_CONFIDENCE_PROBLEMS else 1,
                d.confidence != "high",
            )
        )
        return diagnoses

    # ------------------------------------------------------------------
    # auto_fix_all — scan + fix safe problems automatically
    # ------------------------------------------------------------------

    def auto_fix_all(
        self,
        region:   str = "ap-south-1",
        services: list[str] | None = None,
        limit:    int = _MAX_RESOURCES_PER_SCAN,
    ) -> dict:
        """
        Scan all unhealthy resources and automatically fix safe ones.

        Safe = Ec2Stopped, Ec2StatusFailed, RdsStopped
        Unsafe = anything that needs human judgment (storage, ALB, param groups)

        Returns a full report of what was fixed, skipped, and failed.
        """
        from rich.console import Console
        from rich.table   import Table
        from rich.rule    import Rule

        console = Console()
        console.print(Rule("[bold cyan]AWS Auto-Fix[/bold cyan]"))

        # Step 1 — scan
        console.print(f"[dim]Scanning {region} for unhealthy resources...[/dim]")
        diagnoses = self.scan_account(region, services, limit=limit)

        if not diagnoses:
            console.print("[green]No unhealthy resources found.[/green]")
            return {"fixed": [], "skipped": [], "failed": [], "total": 0}

        fixed:   list[dict] = []
        skipped: list[dict] = []
        failed:  list[dict] = []

        # Step 2 — for each diagnosis decide: fix or skip
        for diagnosis in diagnoses:
            resource_label = (
                f"{diagnosis.resource_type.value} "
                f"{diagnosis.resource_name} ({diagnosis.resource_id})"
            )

            if diagnosis.problem_type in _SAFE_TO_AUTO_FIX and diagnosis.fix_command:
                console.print(
                    f"[yellow]→ Auto-fixing[/yellow] {resource_label} "
                    f"[dim]({diagnosis.problem_type.value})[/dim]"
                )
                result = self.apply_fix(diagnosis)

                if result["success"]:
                    console.print(
                        f"  [green]✓ Fixed[/green] → new status: "
                        f"[bold]{result.get('new_status', 'unknown')}[/bold]"
                    )
                    fixed.append({
                        "resource_id":   diagnosis.resource_id,
                        "resource_name": diagnosis.resource_name,
                        "resource_type": diagnosis.resource_type.value,
                        "problem_type":  diagnosis.problem_type.value,
                        "command":       result["command"],
                        "new_status":    result.get("new_status"),
                    })
                else:
                    console.print(
                        f"  [red]✗ Failed[/red]: {result.get('error', 'unknown error')[:120]}"
                    )
                    failed.append({
                        "resource_id":   diagnosis.resource_id,
                        "resource_name": diagnosis.resource_name,
                        "resource_type": diagnosis.resource_type.value,
                        "problem_type":  diagnosis.problem_type.value,
                        "error":         result.get("error", ""),
                    })

            else:
                reason = _UNSAFE_REASONS.get(
                    diagnosis.problem_type,
                    "No safe auto-fix available",
                )
                console.print(
                    f"[dim]  SKIP[/dim] {resource_label} "
                    f"[dim]— {reason}[/dim]"
                )
                skipped.append({
                    "resource_id":   diagnosis.resource_id,
                    "resource_name": diagnosis.resource_name,
                    "resource_type": diagnosis.resource_type.value,
                    "problem_type":  diagnosis.problem_type.value,
                    "reason":        reason,
                    "suggested_fix": diagnosis.suggested_fix,
                })

        # Step 3 — summary table
        console.print(Rule())
        tbl = Table(show_header=True, header_style="bold magenta")
        tbl.add_column("Resource",     width=28)
        tbl.add_column("Problem",      width=22)
        tbl.add_column("Result",       width=10)
        tbl.add_column("New Status / Reason")

        for r in fixed:
            tbl.add_row(
                f"{r['resource_type']} {r['resource_name']}",
                r["problem_type"],
                "[green]FIXED[/green]",
                r.get("new_status") or "—",
            )
        for r in skipped:
            tbl.add_row(
                f"{r['resource_type']} {r['resource_name']}",
                r["problem_type"],
                "[yellow]SKIPPED[/yellow]",
                r["reason"],
            )
        for r in failed:
            tbl.add_row(
                f"{r['resource_type']} {r['resource_name']}",
                r["problem_type"],
                "[red]FAILED[/red]",
                (r.get("error") or "")[:60],
            )

        console.print(tbl)
        console.print(
            f"\n[bold]Summary:[/bold] "
            f"[green]{len(fixed)} fixed[/green]  "
            f"[yellow]{len(skipped)} skipped[/yellow]  "
            f"[red]{len(failed)} failed[/red]"
        )

        log.info(
            "aws.auto_fix_all.done",
            region=region,
            fixed=len(fixed),
            skipped=len(skipped),
            failed=len(failed),
        )

        return {
            "fixed":   fixed,
            "skipped": skipped,
            "failed":  failed,
            "total":   len(diagnoses),
        }

    # ------------------------------------------------------------------
    # diagnose_resource
    # ------------------------------------------------------------------

    def diagnose_resource(self, resource: AwsResource) -> AwsDiagnosis:
        detail   = _gather_detail(resource)
        memories = retrieve_context(
            f"{resource.name} {resource.status} {resource.resource_type.value}"
        )

        user_text = (
            f"Resource ID  : {resource.id}\n"
            f"Name         : {resource.name}\n"
            f"Type         : {resource.resource_type.value}\n"
            f"Status       : {resource.status}\n"
            f"Region       : {resource.region}\n"
            f"Metadata     : {resource.metadata}\n\n"
            f"--- DETAIL ---\n{detail}"
        )

        system_prompt = context.build_system_prompt(_SYSTEM, memories)

        llm_response = run_sync(
            llm.chat(
                messages=[context.user_message(user_text)],
                system=system_prompt,
                json_mode=True,
                max_tokens=2048,
            )
        )

        try:
            parsed = parse_llm_json(llm_response.content)
        except LLMParseError as exc:
            log.error("aws.diagnose_resource.parse_failed",
                      resource_id=resource.id,
                      error=str(exc.cause),
                      raw=exc.raw[:200])
            # Graceful degradation — don't crash the whole scan
            parsed = {
                "problem_type": "Unknown",
                "root_cause":   f"LLM response could not be parsed. Status: {resource.status}.",
                "suggested_fix": "Check resource manually in AWS console.",
                "fix_command":  None,
                "confidence":   "low",
                "explanation":  "Automated diagnosis failed — inspect resource manually.",
            }

        problem_type = _to_problem_type(parsed.get("problem_type", "Unknown"))

        # Override confidence for unambiguous statuses
        confidence = parsed.get("confidence", "low")
        if problem_type in _HIGH_CONFIDENCE_PROBLEMS:
            confidence = "high"

        diagnosis = AwsDiagnosis(
            resource_id=resource.id,
            resource_name=resource.name,
            resource_type=resource.resource_type,
            region=resource.region,
            problem_type=problem_type,
            root_cause=parsed["root_cause"],
            suggested_fix=parsed["suggested_fix"],
            fix_command=parsed.get("fix_command"),
            confidence=confidence,
            explanation=parsed["explanation"],
        )

        remember(
            content=(
                f"AWS {resource.resource_type.value} {resource.name} ({resource.id}) "
                f"in {resource.region} — status {resource.status} — "
                f"diagnosed as {diagnosis.problem_type.value}: {diagnosis.root_cause}. "
                f"Fix: {diagnosis.suggested_fix}"
            ),
            source="aws-diagnose",
            metadata={
                "resource_id":   resource.id,
                "resource_name": resource.name,
                "resource_type": resource.resource_type.value,
                "region":        resource.region,
                "status":        resource.status,
                "problem_type":  diagnosis.problem_type.value,
                "confidence":    diagnosis.confidence,
            },
        )

        log.info(
            "aws.diagnose_resource.done",
            resource_id=resource.id,
            resource_type=resource.resource_type.value,
            problem_type=diagnosis.problem_type.value,
            confidence=diagnosis.confidence,
        )

        return diagnosis

    # ------------------------------------------------------------------
    # apply_fix — runs command + rechecks resource after
    # ------------------------------------------------------------------

    def apply_fix(self, diagnosis: AwsDiagnosis) -> dict:
        if not diagnosis.fix_command:
            log.warning("aws.apply_fix.no_command",
                        resource_id=diagnosis.resource_id)
            return {
                "success":     False,
                "reason":      "No fix_command available for this problem type.",
                "suggested":   diagnosis.suggested_fix,
                "new_status":  None,
            }

        log.info("aws.apply_fix",
                 resource_id=diagnosis.resource_id,
                 command=diagnosis.fix_command)

        result = run_aws_fix(diagnosis.fix_command)

        new_status: str | None = None
        if result.success:
            # Recheck — verify the fix actually worked
            new_status = _recheck_resource(
                diagnosis.resource_id,
                diagnosis.resource_type,
                diagnosis.region,
            )
            log.info("aws.apply_fix.recheck",
                     resource_id=diagnosis.resource_id,
                     new_status=new_status)

        full_error = result.error or ""
        remember(
            content=(
                f"Applied AWS fix for {diagnosis.resource_name} ({diagnosis.resource_id}) "
                f"in {diagnosis.region}: `{diagnosis.fix_command}` — "
                f"{'succeeded' if result.success else 'failed'}"
                + (f" → new status: {new_status}" if new_status else "")
                + (f": {full_error[:400]}" if full_error else "")
            ),
            source="aws-fix",
            metadata={
                "resource_id":   diagnosis.resource_id,
                "resource_name": diagnosis.resource_name,
                "resource_type": diagnosis.resource_type.value,
                "region":        diagnosis.region,
                "command":       diagnosis.fix_command,
                "success":       result.success,
                "new_status":    new_status,
            },
        )

        log.info(
            "aws.apply_fix.result",
            resource_id=diagnosis.resource_id,
            success=result.success,
            new_status=new_status,
            duration_ms=result.duration_ms,
        )

        return {
            "success":    result.success,
            "command":    diagnosis.fix_command,
            "new_status": new_status,
            "error":      full_error[:400] if full_error else None,
        }
