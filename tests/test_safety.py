"""Tests for core/safety.py — the single place auto-fix risk decisions are made."""
import pytest

from agent.core import safety
from agent.core.safety import RiskLevel


# --- command_is_dangerous -------------------------------------------------

@pytest.mark.parametrize("command", [
    # TLS's legitimate delete_secret fix runs exactly this. A bare "delete"
    # token once blocked it — these must stay allowed.
    "kubectl delete secret my-tls-cert -n default",
    "kubectl delete pod crashing-pod-abc -n infragpt",
    "kubectl rollout restart deployment/api -n prod",
    "kubectl scale deployment/api --replicas=3",
    "kubectl patch deployment api -p '{}'",
])
def test_narrow_reversible_commands_are_not_dangerous(command):
    assert safety.command_is_dangerous(command) is None


@pytest.mark.parametrize("command, token", [
    ("kubectl delete namespace prod", "delete namespace"),
    ("kubectl delete ns staging", "delete ns "),
    ("kubectl delete node ip-10-0-10-11", "delete node"),
    ("kubectl delete pvc data-elasticsearch-0", "delete pvc"),
    ("kubectl delete crd certificates.cert-manager.io", "delete crd"),
    ("kubectl delete clusterrolebinding admin", "delete clusterrole"),
    ("kubectl delete pod x --force --grace-period=0", "--force"),
    ("kubectl drain ip-10-0-10-11 --ignore-daemonsets", "drain"),
    ("kubectl cordon ip-10-0-10-11", "cordon"),
    ("terraform destroy -auto-approve", "destroy"),
])
def test_high_blast_radius_commands_are_dangerous(command, token):
    assert safety.command_is_dangerous(command) == token


def test_dangerous_check_is_case_insensitive():
    assert safety.command_is_dangerous("KUBECTL DELETE NAMESPACE PROD") == "delete namespace"


# --- name_suggests_destructive -------------------------------------------

@pytest.mark.parametrize("name", [
    "destroy pipliine",          # the real job on this Jenkins instance
    "infra-teardown",
    "Nuke-Staging",
    "purge-old-builds",
    "decommission-cluster",
])
def test_destructive_names_are_flagged(name):
    assert safety.name_suggests_destructive(name) is not None


@pytest.mark.parametrize("name", ["infragpt", "backend-api/main", "deploy-prod", "build"])
def test_ordinary_names_are_not_flagged(name):
    assert safety.name_suggests_destructive(name) is None


# --- assess / is_auto_approved -------------------------------------------

def test_whitelisted_low_risk_fix_is_auto_approved_at_high_confidence():
    assert safety.is_auto_approved("jenkins", "FLAKY_TEST", "RETRIGGER_BUILD", "high")


def test_low_risk_fix_is_downgraded_below_high_confidence():
    # A LOW-risk action must never auto-run on a guess.
    assert safety.assess("jenkins", "FLAKY_TEST", "RETRIGGER_BUILD", "medium") == RiskLevel.MEDIUM
    assert not safety.is_auto_approved("jenkins", "FLAKY_TEST", "RETRIGGER_BUILD", "medium")


@pytest.mark.parametrize("category", [
    "CREDENTIAL_EXPIRED", "DISK_FULL", "BAD_JENKINSFILE_SYNTAX", "OUT_OF_MEMORY",
])
def test_never_auto_jenkins_categories_are_critical(category):
    assert safety.assess("jenkins", category, "RETRIGGER_BUILD", "high") == RiskLevel.CRITICAL


@pytest.mark.parametrize("category", ["secret", "rbac"])
def test_security_credential_and_rbac_fixes_are_never_auto(category):
    assert safety.assess("security", category) == RiskLevel.CRITICAL


def test_cost_never_auto_touches_a_database():
    assert safety.assess("cost", "idle_rds") == RiskLevel.CRITICAL


def test_unknown_combination_defaults_to_critical():
    # Default-deny: nothing unlisted may silently fall through to "safe".
    assert safety.assess("jenkins", "SOMETHING_NEW", "RETRIGGER_BUILD", "high") == RiskLevel.CRITICAL
    assert safety.assess("no-such-domain", "anything") == RiskLevel.CRITICAL
    assert not safety.is_auto_approved("no-such-domain", "anything")


def test_only_safe_and_low_are_ever_auto_approved():
    for domain, rules in safety.POLICY.items():
        for (category, action), level in rules.items():
            approved = safety.is_auto_approved(domain, category, action, "high")
            assert approved == (level in (RiskLevel.SAFE, RiskLevel.LOW)), (domain, category, action)
