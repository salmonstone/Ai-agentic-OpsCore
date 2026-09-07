"""
Centralized safety/policy engine — the one place every skill's "is this safe
to do without asking a human" decision goes through.

Before this existed, several skills had independent, inconsistent answers to
the same question:
  - Jenkins (skills/jenkins.py):  a (problem_type, fix_action) whitelist plus
    a NEVER_AUTO blocklist and confidence/risk checks
  - K8s healer (skills/healer.py): _is_safe_command() — a denylisted-token
    check on the raw kubectl command string
  - Security (skills/security.py) and Cost (skills/cost.py): their own
    inline, per-check auto_fixable rules

This module doesn't discard that domain knowledge — it relocates the
cross-domain parts into one auditable, inspectable registry (`agent policy
list`), and keeps the command-content guard as a second, independent layer
that runs regardless of domain: a fix_command that looks destructive is
flagged even if the domain-level policy would have approved it.

Design principle: default-deny. A (domain, category) pair with no explicit
entry is CRITICAL, never silently treated as safe.
"""
from __future__ import annotations

from enum import Enum

from agent.observability.logging import get_logger

log = get_logger(__name__)


class RiskLevel(str, Enum):
    SAFE     = "safe"       # read-only — never gated at all
    LOW      = "low"        # narrow, reversible — auto-approved only at high confidence
    MEDIUM   = "medium"     # a real change — always asks a human
    HIGH     = "high"       # broad blast radius — always asks, flagged prominently
    CRITICAL = "critical"   # never auto-applied under any circumstance


# domain -> {(category, fix_action): RiskLevel}. "*" as fix_action matches any
# fix_action for that category. Anything with no matching entry defaults to
# CRITICAL — an unrecognized combination must never silently fall through to
# "probably fine".
#
# Populated from the REAL existing behavior already in each skill (Jenkins'
# _AUTO_FIX_RULES/_NEVER_AUTO, Security's is_fixable(), Cost's per-check
# auto_fixable flags) — not invented from scratch.
POLICY: dict[str, dict[tuple[str, str], RiskLevel]] = {
    "jenkins": {
        ("FLAKY_TEST",       "RETRIGGER_BUILD"):       RiskLevel.LOW,
        ("STUCK_IN_QUEUE",   "CANCEL_AND_RETRIGGER"):  RiskLevel.LOW,
        ("BUILD_TIMEOUT",    "CANCEL_AND_RETRIGGER"):  RiskLevel.LOW,
        ("AGENT_OFFLINE",    "*"): RiskLevel.MEDIUM,
        ("DOCKER_ERROR",     "*"): RiskLevel.MEDIUM,
        ("NETWORK_ERROR",    "*"): RiskLevel.MEDIUM,
        ("CREDENTIAL_EXPIRED",     "*"): RiskLevel.CRITICAL,
        ("DISK_FULL",              "*"): RiskLevel.CRITICAL,
        ("BAD_JENKINSFILE_SYNTAX", "*"): RiskLevel.CRITICAL,
        ("OUT_OF_MEMORY",           "*"): RiskLevel.CRITICAL,
    },
    "security": {
        ("network",   "*"): RiskLevel.LOW,      # apply a default-deny NetworkPolicy — additive, reversible
        ("runtime",   "*"): RiskLevel.MEDIUM,   # patching securityContext restarts the pod
        ("privilege", "*"): RiskLevel.MEDIUM,   # same — restarts the pod
        ("exposure",  "*"): RiskLevel.MEDIUM,   # changing a service type can break routing
        ("secret",    "*"): RiskLevel.CRITICAL, # never auto-touch a live credential
        ("rbac",      "*"): RiskLevel.CRITICAL, # revoking permissions can lock someone out
    },
    "cost": {
        ("ebs_gp2_to_gp3",           "*"): RiskLevel.LOW,
        ("cloudwatch_log_retention", "*"): RiskLevel.LOW,
        ("ecr_untagged_images",      "*"): RiskLevel.LOW,
        ("idle_ec2",           "*"): RiskLevel.MEDIUM,
        ("unattached_eip",     "*"): RiskLevel.MEDIUM,
        ("idle_load_balancer", "*"): RiskLevel.MEDIUM,
        ("open_security_group", "*"): RiskLevel.HIGH,
        ("idle_rds",             "*"): RiskLevel.CRITICAL,  # never auto-touch a database
    },
}

# Command-content guard — regardless of domain or confidence, a fix_command
# containing one of these should never auto-execute.
#
# NOT a bare "delete" — verified against a real existing pattern this would
# otherwise break: TLS's delete_secret fix type legitimately runs
# `kubectl delete secret <name>` to force cert-manager to regenerate a bad
# cert, and that command reaches THIS function (kubectl.apply_fix, shared by
# ingress/tls/security fixes). A single named secret or pod is narrow and
# reversible; deleting a namespace/node/PV/cluster-scoped RBAC object is not.
# So "delete" here is scoped to specific high-blast-radius resource KINDS,
# not the verb alone — narrower than skills/healer.py's own _UNSAFE_TOKENS
# (which blocks bare "delete" for its own, more limited pod-healing scope;
# that file is unchanged, this is a deliberately different, wider-audience list).
_DANGEROUS_TOKENS = (
    "delete namespace", "delete ns ", "delete node",
    "delete pv ", "delete pvc", "delete crd",
    "delete clusterrole", "delete clusterrolebinding",
    "--force", "drain", "cordon", "uncordon", "wipe", "destroy",
)


def assess(domain: str, category: str, fix_action: str = "*", confidence: str = "high") -> RiskLevel:
    """The one place every skill asks 'how risky is this fix' — default-deny
    for anything not explicitly whitelisted below CRITICAL."""
    rules = POLICY.get(domain, {})
    level = rules.get((category, fix_action)) or rules.get((category, "*")) or RiskLevel.CRITICAL
    # Low/medium confidence never auto-applies, even for an otherwise-LOW-risk action.
    if confidence != "high" and level == RiskLevel.LOW:
        level = RiskLevel.MEDIUM
    log.info("safety.assess", domain=domain, category=category, fix_action=fix_action,
             confidence=confidence, risk_level=level.value)
    return level


def is_auto_approved(domain: str, category: str, fix_action: str = "*", confidence: str = "high") -> bool:
    """True only for RiskLevel.SAFE/LOW at high confidence — the only tier
    ever allowed to execute without a human confirming first."""
    return assess(domain, category, fix_action, confidence) in (RiskLevel.SAFE, RiskLevel.LOW)


# A resource/job NAME suggesting destructive intent — independent of what
# its actual steps do. A job literally called "destroy pipeline" deserves
# extra scrutiny before triggering, and critically: an ordinary "yes, do it"
# confirmation is NOT enough for this — it must require a SEPARATE, explicit
# acknowledgment of the destructive name, so a single confirm=True (which a
# natural-language "yes trigger it" satisfies) can never be sufficient on
# its own for something named like this.
_DESTRUCTIVE_NAME_TOKENS = (
    "destroy", "teardown", "tear-down", "nuke", "purge", "wipe", "decommission",
)


def name_suggests_destructive(name: str) -> str | None:
    """Returns the matched token if a resource/job name itself suggests
    destructive intent, else None."""
    low = name.lower()
    for token in _DESTRUCTIVE_NAME_TOKENS:
        if token in low:
            return token
    return None


def command_is_dangerous(command: str) -> str | None:
    """
    Returns the matched dangerous token if the raw command looks destructive,
    else None. Runs regardless of domain, confidence, or whether a human
    already confirmed it — a last-line-of-defense check independent of the
    policy table above, since a domain/category pair being "approved" says
    nothing about whether the specific generated command is what it claims.
    """
    low = command.lower()
    for token in _DANGEROUS_TOKENS:
        if token in low:
            return token
    return None
