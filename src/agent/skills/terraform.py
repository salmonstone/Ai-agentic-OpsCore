"""
Terraform Scan Skill — a review layer on top of `terraform plan`.

`terraform plan` tells you WHAT will change. This tells you what is DANGEROUS,
INSECURE, or EXPENSIVE about the configuration — in plain English, ranked — the
judgment a mechanical diff can't give you. Runs fully offline: reads .tf files,
needs no cluster, no AWS credentials, and no `terraform` binary.

Four layers, all from static config:
  1. Security   — open security groups, public S3, unencrypted storage, IAM "*"
                  wildcards, hardcoded secrets, publicly-accessible databases.
  2. Cost       — estimated monthly $ for billable resources (reuses ec2_pricing).
  3. Blast radius — stateful resources whose destroy/recreate loses data; real
                  create/update/delete/replace counts if a plan JSON is supplied.
  4. Claude review — connects the findings, prioritises the one that matters.

`--fix` is OPTIONAL and only PATCHES the .tf source (adds `encrypted = true`,
`deletion_protection = true`, …) with a .bak backup. It NEVER runs
`terraform apply` — applying stays Terraform's job.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import (
    TerraformReport, TfCostItem, TfFinding, TfModule, TfResource,
)
from agent.integrations import terraform as tf
from agent.integrations.ec2_pricing import get_instance_price, get_monthly_cost
from agent.memory.retrieval import remember
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_SYSTEM = """\
You are a senior infrastructure and cloud-security reviewer reading a Terraform
configuration. You are NOT re-doing `terraform plan` — the engineer already has
the mechanical diff. Your job is the judgment plan can't give:

1. Connect the findings. A security group open to 0.0.0.0/0 is bad; open to
   0.0.0.0/0 IN FRONT OF an unencrypted, publicly-accessible database is a
   breach waiting to happen. Say so.
2. Prioritise. Lead with the ONE finding that would hurt most in production,
   and say concretely what the blast radius is.
3. Be specific and short. Reference resources by address. No generic advice.

Write 6-10 sentences. Plain English. If the config is clean, say that plainly
and note the biggest latent risk as it grows."""

# Severity → points removed from the security score, and sort weight.
_SEV_PENALTY = {"critical": 25, "high": 15, "medium": 8, "low": 3, "info": 0}
_SEV_RANK    = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Resources that HOLD DATA — destroy/recreate is not free, it loses state.
_STATEFUL = {
    "aws_db_instance", "aws_rds_cluster", "aws_rds_cluster_instance",
    "aws_dynamodb_table", "aws_ebs_volume", "aws_s3_bucket",
    "aws_efs_file_system", "aws_elasticache_cluster",
    "aws_elasticache_replication_group", "aws_docdb_cluster",
    "aws_redshift_cluster", "aws_neptune_cluster", "aws_dax_cluster",
}

# Flat monthly estimates for billable resources we can't price per-instance.
_MONTHLY_FLAT = {
    "aws_nat_gateway": 32.0,        # base hourly; excludes data processing
    "aws_eip":         3.6,         # only billed while unattached
    "aws_lb":          18.0,
    "aws_alb":         18.0,
    "aws_elb":         18.0,
    "aws_cloudwatch_log_group": 0.0,
}

_EBS_PER_GB = 0.08                  # gp3 $/GB-month, us-east-1


def _provider_of(rtype: str) -> str:
    return rtype.split("_", 1)[0] if "_" in rtype else rtype


def _as_bool(v) -> bool | None:
    """Interpret a parsed HCL attribute as a literal bool, or None if unknown."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower() == "true"
    return None


class TerraformScanSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "terraform-scan"

    @property
    def description(self) -> str:
        return (
            "Review Terraform config offline for security, cost, and "
            "blast-radius risks — a judgment layer on top of `terraform plan`."
        )

    def execute(self, input_data: dict) -> dict:
        report = self.scan(
            input_data.get("path", "."),
            plan_file=input_data.get("plan"),
            with_ai=input_data.get("with_ai", True),
        )
        return report.model_dump()

    # ------------------------------------------------------------------
    # Main entry — gather → detect → cost → blast radius → Claude → remember
    # ------------------------------------------------------------------

    def scan(
        self,
        path: str = ".",
        plan_file: str | None = None,
        with_ai: bool = True,
    ) -> TerraformReport:
        root = Path(path)
        parsed = tf.load_dir(root)

        resources: list[TfResource] = []
        for r in parsed["resources"]:
            rtype = r.get("type", "")
            resources.append(TfResource(
                address  = f"{rtype}.{r.get('name', '')}",
                type     = rtype,
                name     = r.get("name", ""),
                file     = r.get("file", ""),
                line     = r.get("line", 0),
                provider = _provider_of(rtype),
                stateful = rtype in _STATEFUL,
            ))

        # Variable defaults let us resolve `region = var.aws_region` for costing.
        var_defaults: dict[str, object] = {}
        for v in parsed.get("variables", []):
            if "default" in v.get("attributes", {}):
                var_defaults[v.get("name", "")] = v["attributes"]["default"]

        modules = self._extract_modules(parsed.get("modules", []))

        findings: list[TfFinding] = []
        findings += self._detect_security(parsed["resources"])
        findings += self._detect_blast_radius(parsed["resources"])
        findings += self._detect_modules(parsed.get("modules", []))

        cost_items, total_cost = self._estimate_cost(
            parsed["resources"], parsed.get("providers", []), var_defaults,
        )
        mod_items, mod_cost = self._estimate_module_cost(
            parsed.get("modules", []), parsed.get("providers", []), var_defaults,
        )
        cost_items += mod_items
        total_cost += mod_cost
        cost_items.sort(key=lambda i: i.monthly_cost, reverse=True)

        # Local state is itself a risk — no locking, easy to lose/corrupt.
        remote_state = bool(parsed.get("backends"))
        if not remote_state and (resources or modules):
            findings.append(TfFinding(
                id="state-local",
                severity="medium",
                category="reliability",
                title="No remote backend — using local state",
                description=(
                    "State is stored locally. There is no locking (concurrent "
                    "applies can corrupt it) and no shared source of truth for a "
                    "team. A lost or clobbered state file orphans every resource."
                ),
                recommendation=(
                    "Configure a remote backend (e.g. S3 + DynamoDB lock table)."
                ),
            ))

        # Real blast radius if a plan JSON was supplied.
        plan_actions: dict = {}
        replace_addresses: list[str] = []
        if plan_file:
            try:
                changes = tf.parse_plan_json(Path(plan_file))
                for c in changes:
                    for a in c["actions"]:
                        plan_actions[a] = plan_actions.get(a, 0) + 1
                    if c["replace"]:
                        replace_addresses.append(c["address"])
                for addr in replace_addresses:
                    rtype = addr.split(".")[0] if "." in addr else ""
                    if rtype in _STATEFUL:
                        findings.append(TfFinding(
                            id=f"replace-{addr}",
                            severity="critical",
                            category="blast-radius",
                            title=f"Plan will DESTROY and recreate {addr}",
                            description=(
                                f"{addr} is a stateful resource the plan replaces "
                                f"(destroy → create). Its data does not survive."
                            ),
                            resource=addr,
                            recommendation=(
                                "Confirm data is backed up / migrated before apply, "
                                "or use create_before_destroy + a migration path."
                            ),
                        ))
            except Exception as exc:
                parsed.setdefault("parse_errors", []).append(
                    f"plan {plan_file}: {exc}"
                )

        # Score + rank.
        score = 100
        for f in findings:
            score -= _SEV_PENALTY.get(f.severity, 0)
        score = max(0, score)
        findings.sort(key=lambda f: (_SEV_RANK.get(f.severity, 9), f.category))

        report = TerraformReport(
            root                   = str(root),
            file_count             = parsed["file_count"],
            resource_count         = len(resources),
            providers              = sorted({p.get("name", "") for p in parsed["providers"] if p.get("name")}),
            resources              = resources,
            modules                = modules,
            findings               = findings,
            cost_items             = cost_items,
            estimated_monthly_cost = round(total_cost, 2),
            plan_actions           = plan_actions,
            replace_addresses      = replace_addresses,
            security_score         = score,
            remote_state           = remote_state,
            parse_errors           = parsed.get("parse_errors", []),
            generated_at           = datetime.now(timezone.utc).isoformat(),
        )

        if with_ai:
            report.claude_summary = self.analyze_with_claude(report)

        crit = report.critical_count
        high = report.high_count
        remember(
            content=(
                f"Terraform scan of {root}: {len(resources)} resources, "
                f"{len(modules)} module(s), "
                f"{len(findings)} findings ({crit} critical, {high} high), "
                f"security score {score}/100, "
                f"est ${total_cost:.0f}/mo. {report.claude_summary[:200]}"
            ),
            source="terraform-scan",
            metadata={
                "root":           str(root),
                "resources":      len(resources),
                "modules":        len(modules),
                "findings":       len(findings),
                "critical":       crit,
                "high":           high,
                "security_score": score,
                "monthly_cost":   round(total_cost, 2),
            },
        )
        log.info(
            "terraform.scan_done",
            root=str(root), resources=len(resources), findings=len(findings),
            critical=crit, score=score, monthly_cost=round(total_cost, 2),
        )
        return report

    # ------------------------------------------------------------------
    # Security detection — rule-based, from parsed attributes + raw source
    # ------------------------------------------------------------------

    def _detect_security(self, resources: list[dict]) -> list[TfFinding]:
        findings: list[TfFinding] = []
        n = 0

        def _f(**kw) -> TfFinding:
            nonlocal n
            n += 1
            kw.setdefault("id", f"sec-{n}")
            kw.setdefault("category", "security")
            return TfFinding(**kw)

        for r in resources:
            rtype = r.get("type", "")
            name  = r.get("name", "")
            addr  = f"{rtype}.{name}"
            attrs = r.get("attributes", {})
            subs  = r.get("blocks", [])
            raw   = r.get("raw", "")
            file  = r.get("file", "")
            line  = r.get("line", 0)

            # 1. Security group open to the world ---------------------------
            if rtype in ("aws_security_group", "aws_security_group_rule"):
                open_ports = self._open_ingress(rtype, attrs, subs)
                if open_ports:
                    ports = ", ".join(open_ports)
                    sev = "critical" if any(
                        p in ("22", "3389", "all", "0-65535") for p in open_ports
                    ) else "high"
                    findings.append(_f(
                        severity=sev,
                        title=f"{addr} allows 0.0.0.0/0 ingress ({ports})",
                        description=(
                            f"Ingress from 0.0.0.0/0 exposes {ports} to the entire "
                            f"internet. "
                            + ("SSH/RDP open to the world is a top breach vector."
                               if sev == "critical" else
                               "Anyone can reach this port.")
                        ),
                        resource=addr, file=file, line=line,
                        evidence=f"cidr_blocks includes 0.0.0.0/0 on port(s) {ports}",
                        recommendation=(
                            "Restrict cidr_blocks to known office/VPC ranges or "
                            "front with a load balancer / bastion."
                        ),
                    ))

            # 2. Public S3 bucket -------------------------------------------
            if rtype == "aws_s3_bucket":
                acl = attrs.get("acl", "")
                if acl in ("public-read", "public-read-write"):
                    findings.append(_f(
                        severity="critical",
                        title=f"{addr} is publicly readable (acl = {acl})",
                        description=(
                            f"Bucket ACL '{acl}' makes objects world-readable. "
                            "Public S3 buckets are a classic data-leak source."
                        ),
                        resource=addr, file=file, line=line,
                        evidence=f'acl = "{acl}"',
                        recommendation=(
                            "Set acl private and add an aws_s3_bucket_public_access_block."
                        ),
                    ))
            if rtype == "aws_s3_bucket_public_access_block":
                flags = ("block_public_acls", "block_public_policy",
                         "ignore_public_acls", "restrict_public_buckets")
                if any(_as_bool(attrs.get(fl)) is False for fl in flags):
                    findings.append(_f(
                        severity="high",
                        title=f"{addr} disables S3 public-access protection",
                        description=(
                            "One or more public-access-block flags are false, "
                            "re-opening the door to public bucket policies/ACLs."
                        ),
                        resource=addr, file=file, line=line,
                        recommendation="Set all four block/restrict flags to true.",
                    ))

            # 3. Unencrypted storage ----------------------------------------
            if rtype == "aws_ebs_volume":
                if _as_bool(attrs.get("encrypted")) is not True:
                    findings.append(_f(
                        severity="high",
                        title=f"{addr} EBS volume is not encrypted",
                        description="Data at rest on this volume is unencrypted.",
                        resource=addr, file=file, line=line,
                        recommendation="Add encrypted = true.",
                        auto_fixable=True, fix_attribute="encrypted = true",
                    ))
            if rtype in ("aws_db_instance", "aws_rds_cluster"):
                if _as_bool(attrs.get("storage_encrypted")) is not True:
                    findings.append(_f(
                        severity="high",
                        title=f"{addr} database storage is not encrypted",
                        description=(
                            "storage_encrypted is not set — the database's data at "
                            "rest is unencrypted. (Enabling it forces a replacement.)"
                        ),
                        resource=addr, file=file, line=line,
                        recommendation="Add storage_encrypted = true.",
                        auto_fixable=True, fix_attribute="storage_encrypted = true",
                    ))

            # 4. Publicly accessible database -------------------------------
            if rtype == "aws_db_instance":
                if _as_bool(attrs.get("publicly_accessible")) is True:
                    findings.append(_f(
                        severity="critical",
                        title=f"{addr} database is publicly accessible",
                        description=(
                            "publicly_accessible = true assigns the DB a public "
                            "endpoint reachable from the internet."
                        ),
                        resource=addr, file=file, line=line,
                        evidence="publicly_accessible = true",
                        recommendation=(
                            "Set publicly_accessible = false; reach it from within "
                            "the VPC or via a bastion/VPN."
                        ),
                    ))

            # 5. IAM wildcard policy (scan raw — policy is often heredoc/json)
            if rtype in ("aws_iam_policy", "aws_iam_role_policy",
                         "aws_iam_user_policy", "aws_iam_group_policy") or \
               "policy" in attrs:
                if _has_star_permission(raw):
                    findings.append(_f(
                        severity="high",
                        title=f"{addr} grants wildcard IAM permissions",
                        description=(
                            'The policy uses "Action": "*" and/or "Resource": "*", '
                            "granting far more than least-privilege requires."
                        ),
                        resource=addr, file=file, line=line,
                        evidence='"Action": "*" / "Resource": "*"',
                        recommendation=(
                            "Scope Action and Resource to the specific services/ARNs "
                            "this principal needs."
                        ),
                    ))

            # 6. Hardcoded secrets ------------------------------------------
            for secret in _hardcoded_secrets(raw):
                findings.append(_f(
                    severity="critical",
                    title=f"{addr} has a hardcoded secret ({secret})",
                    description=(
                        f"'{secret}' is set to a literal value in source. Secrets in "
                        ".tf files land in version control and state."
                    ),
                    resource=addr, file=file, line=line,
                    evidence=f"{secret} = \"<literal>\"",
                    recommendation=(
                        "Move it to a variable/secret manager "
                        "(var, aws_secretsmanager_secret, or TF_VAR_ env)."
                    ),
                ))

        return findings

    def _open_ingress(self, rtype: str, attrs: dict, subs: list[dict]) -> list[str]:
        """Return the port labels exposed to 0.0.0.0/0, or [] if none."""
        def _is_open(cidrs) -> bool:
            if isinstance(cidrs, list):
                return any(str(c) in ("0.0.0.0/0", "::/0") for c in cidrs)
            return str(cidrs) in ("0.0.0.0/0", "::/0")

        def _port_label(a: dict) -> str:
            fp, tp = a.get("from_port"), a.get("to_port")
            proto  = str(a.get("protocol", "")).lower()
            if proto in ("-1", "all") or (fp in (0, "0") and tp in (65535, "65535")):
                return "all"
            if fp is None:
                return "?"
            return str(fp) if fp == tp else f"{fp}-{tp}"

        ports: list[str] = []
        if rtype == "aws_security_group_rule":
            if str(attrs.get("type", "ingress")) == "ingress" and _is_open(
                attrs.get("cidr_blocks")
            ):
                ports.append(_port_label(attrs))
        else:  # aws_security_group with inline ingress blocks
            for b in subs:
                if b.get("kind") == "ingress" and _is_open(
                    b.get("attributes", {}).get("cidr_blocks")
                ):
                    ports.append(_port_label(b.get("attributes", {})))
        return ports

    # ------------------------------------------------------------------
    # Blast radius — stateful resources with weak destroy protection
    # ------------------------------------------------------------------

    def _detect_blast_radius(self, resources: list[dict]) -> list[TfFinding]:
        findings: list[TfFinding] = []
        n = 0
        for r in resources:
            rtype = r.get("type", "")
            if rtype not in _STATEFUL:
                continue
            addr  = f"{rtype}.{r.get('name', '')}"
            attrs = r.get("attributes", {})
            file  = r.get("file", "")
            line  = r.get("line", 0)

            if rtype in ("aws_db_instance", "aws_rds_cluster",
                         "aws_docdb_cluster", "aws_redshift_cluster",
                         "aws_neptune_cluster"):
                if _as_bool(attrs.get("deletion_protection")) is not True:
                    n += 1
                    findings.append(TfFinding(
                        id=f"blast-{n}",
                        severity="medium",
                        category="blast-radius",
                        title=f"{addr} has no deletion protection",
                        description=(
                            f"{addr} holds data but deletion_protection is not set. "
                            "A stray destroy — or a change that forces replacement — "
                            "wipes it with nothing to stop the apply."
                        ),
                        resource=addr, file=file, line=line,
                        recommendation="Add deletion_protection = true.",
                        auto_fixable=True, fix_attribute="deletion_protection = true",
                    ))
                if _as_bool(attrs.get("skip_final_snapshot")) is True:
                    n += 1
                    findings.append(TfFinding(
                        id=f"blast-{n}",
                        severity="medium",
                        category="blast-radius",
                        title=f"{addr} skips its final snapshot on destroy",
                        description=(
                            "skip_final_snapshot = true means destroying this "
                            "database leaves no recovery point."
                        ),
                        resource=addr, file=file, line=line,
                        recommendation=(
                            "Set skip_final_snapshot = false and a "
                            "final_snapshot_identifier."
                        ),
                    ))
        return findings

    # ------------------------------------------------------------------
    # Cost estimation — reuse ec2_pricing, best-effort with assumptions
    # ------------------------------------------------------------------

    def _resolve_region(self, providers: list[dict], var_defaults: dict) -> str:
        """Best-effort region: literal provider region, or a var default it points to."""
        for p in providers:
            reg = p.get("attributes", {}).get("region")
            if not isinstance(reg, str) or not reg:
                continue
            if reg.startswith("var."):
                val = var_defaults.get(reg[len("var."):])
                if isinstance(val, str) and val:
                    return val
            elif not reg.startswith("local."):
                return reg
        return "us-east-1"

    def _estimate_cost(
        self, resources: list[dict], providers: list[dict],
        var_defaults: dict | None = None,
    ) -> tuple[list[TfCostItem], float]:
        region = self._resolve_region(providers, var_defaults or {})

        items: list[TfCostItem] = []
        total = 0.0

        for r in resources:
            rtype = r.get("type", "")
            addr  = f"{rtype}.{r.get('name', '')}"
            attrs = r.get("attributes", {})

            count = attrs.get("count")
            mult  = count if isinstance(count, int) and count > 0 else 1
            mult_note = f" x{mult}" if mult > 1 else ""

            monthly = 0.0
            detail = ""
            assumptions = f"{region}, 730h/mo{mult_note}"

            if rtype == "aws_instance":
                itype = attrs.get("instance_type", "")
                pricing = get_instance_price(itype) if isinstance(itype, str) else None
                if pricing:
                    monthly = get_monthly_cost(pricing["hourly"], region) * mult
                    detail = itype
                else:
                    detail = itype or "unknown type"
                    assumptions += "; price unknown for this type"

            elif rtype in ("aws_db_instance", "aws_rds_cluster_instance"):
                iclass = str(attrs.get("instance_class", ""))
                base = iclass.replace("db.", "", 1)
                pricing = get_instance_price(base)
                if pricing:
                    # RDS runs pricier than raw EC2 — nudge up as a rough proxy.
                    monthly = get_monthly_cost(pricing["hourly"], region) * 1.25 * mult
                    detail = iclass
                    assumptions += "; RDS ~1.25x EC2 (rough)"
                else:
                    detail = iclass or "unknown class"
                    assumptions += "; price unknown for this class"

            elif rtype == "aws_ebs_volume":
                size = attrs.get("size")
                if isinstance(size, int):
                    monthly = size * _EBS_PER_GB * mult
                    detail = f"{size}GB"
                    assumptions += f"; ${_EBS_PER_GB}/GB gp3"

            elif rtype in _MONTHLY_FLAT:
                monthly = _MONTHLY_FLAT[rtype] * mult
                detail = "base rate"
                if rtype == "aws_eip":
                    assumptions += "; billed only while unattached"
                elif rtype == "aws_nat_gateway":
                    assumptions += "; excludes per-GB data processing"

            if monthly > 0:
                monthly = round(monthly, 2)
                total += monthly
                items.append(TfCostItem(
                    resource=addr, type=rtype, detail=detail,
                    monthly_cost=monthly, assumptions=assumptions,
                ))

        items.sort(key=lambda i: i.monthly_cost, reverse=True)
        return items, total

    # ------------------------------------------------------------------
    # Modules — where the real infrastructure usually lives
    # ------------------------------------------------------------------

    def _extract_modules(self, raw_modules: list[dict]) -> list[TfModule]:
        out: list[TfModule] = []
        for m in raw_modules:
            attrs = m.get("attributes", {})
            source = str(attrs.get("source", "") or "")
            version = str(attrs.get("version", "") or "")
            local = source.startswith(("./", "../", ".\\", "..\\"))
            pinned = local or bool(version) or "?ref=" in source
            out.append(TfModule(
                name=m.get("name", ""), source=source, version=version,
                pinned=pinned, local=local,
                file=m.get("file", ""), line=m.get("line", 0),
            ))
        return out

    def _detect_modules(self, raw_modules: list[dict]) -> list[TfFinding]:
        findings: list[TfFinding] = []
        n = 0

        def _f(**kw) -> TfFinding:
            nonlocal n
            n += 1
            kw.setdefault("id", f"mod-{n}")
            return TfFinding(**kw)

        for m in raw_modules:
            addr  = f"module.{m.get('name', '')}"
            attrs = m.get("attributes", {})
            raw   = m.get("raw", "")
            file  = m.get("file", "")
            line  = m.get("line", 0)
            source  = str(attrs.get("source", "") or "")
            version = str(attrs.get("version", "") or "")
            local   = source.startswith(("./", "../", ".\\", "..\\"))

            if not local and not version and "?ref=" not in source:
                findings.append(_f(
                    severity="medium", category="reliability",
                    title=f"{addr} is not version-pinned",
                    description=(
                        f"Module '{source or m.get('name', '')}' has no version "
                        "constraint. A later apply can silently pull a new module "
                        "version, changing infrastructure with no code change."
                    ),
                    resource=addr, file=file, line=line,
                    recommendation='Pin it: version = "x.y.z" (or a git ?ref=).',
                ))

            if re.search(r'\b\w*public_access\s*=\s*true', raw):
                findings.append(_f(
                    severity="high", category="security",
                    title=f"{addr} exposes a public endpoint",
                    description=(
                        "A *_public_access input is true — the endpoint this module "
                        "creates is reachable from the internet (open to 0.0.0.0/0 "
                        "unless access CIDRs are restricted)."
                    ),
                    resource=addr, file=file, line=line,
                    evidence="*_public_access = true",
                    recommendation=(
                        "Prefer private access (VPN/bastion), or restrict the public "
                        "access CIDRs to known ranges."
                    ),
                ))

            if re.search(r'\bpublicly_accessible\s*=\s*true', raw):
                findings.append(_f(
                    severity="high", category="security",
                    title=f"{addr} sets publicly_accessible = true",
                    description="A resource this module creates gets a public endpoint.",
                    resource=addr, file=file, line=line,
                    recommendation="Set publicly_accessible = false.",
                ))

            if "0.0.0.0/0" in raw:
                findings.append(_f(
                    severity="high", category="security",
                    title=f"{addr} wires 0.0.0.0/0 into a module input",
                    description=(
                        "An open CIDR is passed to this module — likely a security "
                        "group or ingress rule open to the entire internet."
                    ),
                    resource=addr, file=file, line=line, evidence="0.0.0.0/0",
                    recommendation="Restrict the CIDR to known office/VPC ranges.",
                ))

            if _has_star_permission(raw):
                findings.append(_f(
                    severity="high", category="security",
                    title=f"{addr} passes a wildcard IAM policy",
                    description='A policy input uses "Action":"*" and "Resource":"*".',
                    resource=addr, file=file, line=line,
                    recommendation="Scope the policy to least privilege.",
                ))

            for secret in _hardcoded_secrets(raw):
                findings.append(_f(
                    severity="critical", category="security",
                    title=f"{addr} has a hardcoded secret ({secret})",
                    description=f"'{secret}' is a literal value in a module input.",
                    resource=addr, file=file, line=line,
                    recommendation="Move it to a variable / secret manager.",
                ))

        return findings

    def _estimate_module_cost(
        self, raw_modules: list[dict], providers: list[dict], var_defaults: dict,
    ) -> tuple[list[TfCostItem], float]:
        """Best-effort cost for well-known modules (EKS, VPC NAT)."""
        region = self._resolve_region(providers, var_defaults or {})
        items: list[TfCostItem] = []
        total = 0.0

        for m in raw_modules:
            addr   = f"module.{m.get('name', '')}"
            source = str(m.get("attributes", {}).get("source", "") or "").lower()
            raw    = m.get("raw", "")

            # EKS control plane + managed node groups
            if "/eks" in source:
                cp = round(get_monthly_cost(0.10, region), 2)   # $0.10/hr
                total += cp
                items.append(TfCostItem(
                    resource=addr, type="module (eks)", detail="control plane",
                    monthly_cost=cp,
                    assumptions=f"{region}, $0.10/hr EKS control plane",
                ))
                itypes  = re.findall(r'instance_types\s*=\s*\[\s*"([^"]+)"', raw)
                desired = [int(x) for x in re.findall(r'desired_size\s*=\s*(\d+)', raw)]
                if itypes and desired:
                    itype = itypes[0]
                    nodes = sum(desired)
                    pricing = get_instance_price(itype)
                    if pricing:
                        nc = round(get_monthly_cost(pricing["hourly"], region) * nodes, 2)
                        total += nc
                        items.append(TfCostItem(
                            resource=addr, type="module (eks nodes)",
                            detail=f"{nodes}x {itype}", monthly_cost=nc,
                            assumptions=f"{region}, desired_size total={nodes}",
                        ))

            # VPC NAT gateway
            if "/vpc" in source and re.search(r'enable_nat_gateway\s*=\s*true', raw):
                single = bool(re.search(r'single_nat_gateway\s*=\s*true', raw))
                nc = round(_MONTHLY_FLAT["aws_nat_gateway"], 2)   # floor at one NAT
                total += nc
                items.append(TfCostItem(
                    resource=addr, type="module (vpc nat)",
                    detail="1 NAT gateway" + ("" if single else " (1+, per-AZ)"),
                    monthly_cost=nc,
                    assumptions=(
                        f"{region}, $32/mo base"
                        + ("; single_nat_gateway" if single else "; per-AZ, floor 1")
                    ),
                ))

        return items, total

    # ------------------------------------------------------------------
    # Claude review
    # ------------------------------------------------------------------

    def analyze_with_claude(self, report: TerraformReport) -> str:
        if not report.resources and not report.modules:
            return (
                "No Terraform resources or modules found under this path — nothing "
                "to review. Point the scan at a directory containing .tf files."
            )

        lines = [
            "=== TERRAFORM CONFIG REVIEW ===",
            f"Path: {report.root}",
            f"Resources: {report.resource_count} across {report.file_count} file(s)",
            f"Modules: {len(report.modules)}"
            + (" (module-only config — real resources live in the modules)"
               if report.modules and not report.resources else ""),
            f"Providers: {', '.join(report.providers) or 'none'}",
            f"Remote state: {'yes' if report.remote_state else 'NO (local state)'}",
            f"Estimated cost: ${report.estimated_monthly_cost:,.2f}/month",
            f"Security score: {report.security_score}/100",
        ]
        if report.modules:
            lines.append("")
            lines.append("=== MODULES ===")
            for mod in report.modules[:12]:
                pin = f"@{mod.version}" if mod.version else (
                    "(local)" if mod.local else "(UNPINNED)")
                lines.append(f"module.{mod.name} = {mod.source} {pin}")
        lines.append("")
        lines.append("=== FINDINGS (ranked) ===")
        if report.findings:
            for f in report.findings[:20]:
                loc = f" [{f.file}]" if f.file else ""
                lines.append(f"[{f.severity.upper()}] {f.title}{loc}")
                if f.evidence:
                    lines.append(f"    evidence: {f.evidence}")
        else:
            lines.append("(no rule-based findings)")

        if report.plan_actions:
            lines.append("")
            lines.append("=== PLAN BLAST RADIUS ===")
            lines.append(", ".join(f"{k}={v}" for k, v in report.plan_actions.items()))
            if report.replace_addresses:
                lines.append(f"REPLACED (destroy+recreate): {', '.join(report.replace_addresses)}")

        if report.cost_items:
            lines.append("")
            lines.append("=== TOP COST DRIVERS ===")
            for c in report.cost_items[:8]:
                lines.append(f"{c.resource} ({c.detail}) — ${c.monthly_cost:,.2f}/mo")

        try:
            resp = run_sync(llm.chat(
                messages=[context.user_message("\n".join(lines))],
                system=_SYSTEM,
                max_tokens=800,
            ))
            return resp.content.strip()
        except Exception as exc:
            log.warning("terraform.claude_failed", error=str(exc))
            return f"AI analysis unavailable: {exc}"

    # ------------------------------------------------------------------
    # --fix — PATCH the .tf source only. Never runs `terraform apply`.
    # ------------------------------------------------------------------

    def apply_fixes(self, report: TerraformReport) -> list[dict]:
        """
        Insert curated additive attributes (encrypted = true, deletion_protection
        = true, …) into the .tf files for auto-fixable findings. Writes a .bak
        backup per file before editing. Returns a list of applied/skipped results.

        Deliberately conservative: only findings the detector marked
        auto_fixable, and only if the resource block doesn't already carry the
        attribute. Ambiguous fixes (CIDR narrowing, flipping ACLs) stay advisory.
        """
        results: list[dict] = []
        root = Path(report.root)
        # Cache original text per file so multiple fixes to one file compound.
        cache: dict[Path, str] = {}
        backed_up: set[Path] = set()

        for f in report.findings:
            if not (f.auto_fixable and f.fix_attribute and f.file and f.resource):
                continue

            fpath = (root / f.file) if not Path(f.file).is_absolute() else Path(f.file)
            if not fpath.exists():
                results.append({"finding": f.id, "status": "skipped",
                                "reason": f"file not found: {fpath}"})
                continue

            try:
                text = cache.get(fpath) or fpath.read_text(encoding="utf-8")
            except Exception as exc:
                results.append({"finding": f.id, "status": "error", "reason": str(exc)})
                continue

            rtype, _, rname = f.resource.partition(".")
            attr_key = f.fix_attribute.split("=")[0].strip()
            new_text, applied = _insert_attribute(text, rtype, rname, attr_key,
                                                   f.fix_attribute)
            if not applied:
                results.append({"finding": f.id, "status": "skipped",
                                "reason": "block not found or attribute already set"})
                continue

            if fpath not in backed_up:
                try:
                    fpath.with_suffix(fpath.suffix + ".bak").write_text(
                        text, encoding="utf-8"
                    )
                    backed_up.add(fpath)
                except Exception as exc:
                    results.append({"finding": f.id, "status": "error",
                                    "reason": f"backup failed: {exc}"})
                    continue

            cache[fpath] = new_text
            results.append({"finding": f.id, "status": "applied",
                            "file": str(fpath), "change": f.fix_attribute})

        # Flush edited files to disk.
        for fpath, new_text in cache.items():
            try:
                fpath.write_text(new_text, encoding="utf-8")
            except Exception as exc:
                results.append({"finding": "*", "status": "error",
                                "reason": f"write failed {fpath}: {exc}"})

        applied_n = sum(1 for r in results if r["status"] == "applied")
        if applied_n:
            remember(
                content=(
                    f"Patched {applied_n} Terraform finding(s) in {root} "
                    f"(added encryption/deletion-protection attributes; .bak backups "
                    f"written). Did NOT run terraform apply."
                ),
                source="terraform-fix",
                metadata={"root": str(root), "applied": applied_n},
            )
            log.info("terraform.fixes_applied", root=str(root), applied=applied_n)
        return results


# ---------------------------------------------------------------------------
# Raw-source scanners (policy JSON + secrets are unreliable via the parser)
# ---------------------------------------------------------------------------

def _has_star_permission(raw: str) -> bool:
    """True if the block's raw source grants Action=* AND Resource=* in a policy."""
    r = raw.replace(" ", "").replace("'", '"')
    has_action = '"Action":"*"' in r or '"Action":["*"]' in r
    has_resource = '"Resource":"*"' in r or '"Resource":["*"]' in r
    return has_action and has_resource


_SECRET_KEYS = ("password", "secret_key", "secret", "access_key",
                "private_key", "token", "api_key")
_REF_PREFIXES = ("var.", "local.", "data.", "module.", "aws_", "random_")


def _hardcoded_secrets(raw: str) -> list[str]:
    """Return secret-ish attribute names assigned a literal string in raw source."""
    found: list[str] = []
    for key in _SECRET_KEYS:
        # key = "literal"   (not key = var.x, not an empty string)
        m = re.search(rf'\b{re.escape(key)}\s*=\s*"([^"]*)"', raw)
        if not m:
            continue
        val = m.group(1).strip()
        if not val:
            continue
        if any(val.startswith(p) for p in _REF_PREFIXES):
            continue
        if "${" in val:                      # interpolated — not a raw literal
            continue
        found.append(key)
    return found


def _insert_attribute(
    text: str, rtype: str, rname: str, attr_key: str, attr_line: str,
) -> tuple[str, bool]:
    """
    Insert `attr_line` just inside `resource "rtype" "rname" {`.

    Returns (new_text, applied). No-op (applied=False) if the block can't be
    located or the attribute is already present in that block.
    """
    header = re.compile(
        rf'resource\s+"{re.escape(rtype)}"\s+"{re.escape(rname)}"\s*\{{'
    )
    m = header.search(text)
    if not m:
        return text, False

    # Find the matching close brace to bound the block.
    open_idx = text.index("{", m.start())
    depth = 0
    i = open_idx
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = text[open_idx:i]
    if re.search(rf'\b{re.escape(attr_key)}\s*=', block):
        return text, False                    # already set — don't duplicate

    # Match the indentation of the first attribute line inside the block.
    indent = "  "
    nl = text.find("\n", open_idx)
    if nl != -1:
        after = text[nl + 1:]
        im = re.match(r"[ \t]+", after)
        if im:
            indent = im.group(0)

    insertion = f"\n{indent}{attr_line}"
    return text[:open_idx + 1] + insertion + text[open_idx + 1:], True
