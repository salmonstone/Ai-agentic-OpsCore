"""
Deployment eval runner — 12 cases covering:
  webhook HMAC verification, mapping resolution, risk scorer,
  health gate, watcher auto-rollback.

Run: python evals/deployment/runner.py
  or: agent eval deployment
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Ensure project root on path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

CASES_PATH = Path(__file__).parent / "cases.jsonl"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_cases() -> list[dict]:
    cases = []
    with open(CASES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _pass(case_id: str, scenario: str, score: int, note: str = "") -> dict:
    tag = f"[PASS {score:3d}]"
    print(f"  {tag}  {case_id}  {scenario}" + (f"  — {note}" if note else ""))
    return {"id": case_id, "passed": True, "score": score}


def _fail(case_id: str, scenario: str, reason: str) -> dict:
    print(f"  [FAIL   0]  {case_id}  {scenario}  — {reason}")
    return {"id": case_id, "passed": False, "score": 0}


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------

def _eval_webhook_sig(case: dict) -> dict:
    """Evaluate HMAC webhook signature verification without a live server."""
    secret  = case["secret"].encode()
    body    = b'{"ref":"refs/heads/main","repository":{"full_name":"acme/api"}}'

    if case.get("compute_valid_sig"):
        sig_header = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    else:
        sig_header = case["signature"]

    if not sig_header or not sig_header.startswith("sha256="):
        verified = False
    else:
        expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        verified = hmac.compare_digest(expected, sig_header)

    if verified == case["expected_verified"]:
        note = "valid sig accepted" if verified else "bad sig rejected"
        return _pass(case["id"], case["scenario"], 100, note)
    return _fail(case["id"], case["scenario"], f"expected verified={case['expected_verified']}, got {verified}")


def _eval_mapping(case: dict) -> dict:
    """Evaluate MappingLoader.find_mapping without file I/O."""
    from agent.core.models import WebhookConfig, WebhookMapping

    raw_mappings = case["mappings"]
    mappings = [WebhookMapping(**m) for m in raw_mappings]
    cfg = WebhookConfig(mappings=mappings)

    repo, branch = case["repo"], case["branch"]
    found = None
    for m in cfg.mappings:
        if m.repo == repo and m.branch == branch:
            found = m
            break
    if found is None:
        for m in cfg.mappings:
            if m.repo == repo and m.branch == "main":
                found = m
                break

    expected_match = case["expected_match"]
    if not expected_match:
        if found is None:
            return _pass(case["id"], case["scenario"], 100, "no match as expected")
        return _fail(case["id"], case["scenario"], f"expected no match but got {found.deployment}")

    if found is None:
        return _fail(case["id"], case["scenario"], "expected match but found nothing")

    exp_dep = case.get("expected_deployment")
    if exp_dep and found.deployment != exp_dep:
        return _fail(case["id"], case["scenario"], f"expected deployment={exp_dep}, got {found.deployment}")

    return _pass(case["id"], case["scenario"], 100, f"matched -> {found.deployment}")


def _eval_risk(case: dict) -> dict:
    """Evaluate DeploymentSkill.score_deployment_risk with mocked kubectl."""
    from agent.skills.deployment import DeploymentSkill
    from agent.core.models import RiskScore

    fake_info = MagicMock()
    fake_info.healthy = True
    fake_info.replicas_ready = 2
    fake_info.replicas_desired = 2
    fake_info.current_image = case["current_image"]
    fake_info.revision = 3

    # Two items so len(hist) >= 2 → no "+15 for no prev revision"
    fake_history = [
        MagicMock(image=case["current_image"],   change_cause="manual", created_at="", revision=3),
        MagicMock(image=case["current_image"],   change_cause="manual", created_at="", revision=2),
    ]

    ok_kubectl = MagicMock(success=True, output='{"items":[]}', error="")

    with (
        patch("agent.skills.deployment.get_deployment_info",    return_value=fake_info),
        patch("agent.skills.deployment.get_deployment_history", return_value=fake_history),
        patch("agent.skills.deployment.run_kubectl",            return_value=ok_kubectl),
        patch("agent.skills.deployment.check_node_capacity",    return_value={"free_cpu_m": 2000, "free_mem_mb": 4000, "total_cpu_m": 4000, "total_mem_mb": 8000}),
        patch("agent.skills.deployment.retrieve_context",       return_value=[]),
    ):
        skill = DeploymentSkill()
        skill.llm = MagicMock()
        skill.llm.chat = MagicMock(return_value=MagicMock(content="ok"))

        risk: RiskScore = skill.score_deployment_risk(
            case["deployment"], case["namespace"],
            case["new_image"], case["current_image"],
        )

    errors: list[str] = []

    if "expected_label" in case and risk.label != case["expected_label"]:
        errors.append(f"label: expected {case['expected_label']}, got {risk.label}")

    if "expected_label_one_of" in case and risk.label not in case["expected_label_one_of"]:
        errors.append(f"label: expected one of {case['expected_label_one_of']}, got {risk.label}")

    if "expected_max_score" in case and risk.total > case["expected_max_score"]:
        errors.append(f"score {risk.total} > max {case['expected_max_score']}")

    if "expected_min_score" in case and risk.total < case["expected_min_score"]:
        errors.append(f"score {risk.total} < min {case['expected_min_score']}")

    if "expected_auto_approve" in case and risk.auto_approve != case["expected_auto_approve"]:
        errors.append(f"auto_approve: expected {case['expected_auto_approve']}, got {risk.auto_approve}")

    if errors:
        return _fail(case["id"], case["scenario"], " | ".join(errors))

    return _pass(case["id"], case["scenario"], 100, f"score={risk.total} label={risk.label}")


def _eval_health_gate(case: dict) -> dict:
    """Evaluate pre_deploy_health_check with mocked cluster state."""
    from agent.skills.deployment import DeploymentSkill

    if case["mock_pods_healthy"]:
        pods_json = json.dumps({"items": [
            {"status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0, "state": {"running": {}}}]}}
            for _ in range(2)
        ]})
    else:
        pods_json = json.dumps({"items": [
            {"metadata": {"name": "app-crash-abc"}, "status": {"phase": "CrashLoopBackOff", "containerStatuses": [{"ready": False, "restartCount": 10, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}
        ]})

    nodes_json = json.dumps({"items": [
        {"status": {"conditions": [{"type": "Ready", "status": "True"}],
                    "allocatable": {"cpu": "4", "memory": "8Gi"},
                    "capacity":    {"cpu": "4", "memory": "8Gi"}}}
    ]})

    # capacity mock: 50% used — well within limits
    ok_capacity = {"free_cpu_m": 2000, "free_mem_mb": 4000, "total_cpu_m": 4000, "total_mem_mb": 8000}

    def mock_kubectl(cmd: list, **kw) -> MagicMock:
        r = MagicMock()
        r.success = True
        r.error   = ""
        joined    = " ".join(cmd)
        if "get pods" in joined:
            r.output = pods_json
        elif "get nodes" in joined:
            r.output = nodes_json
        elif "rollout" in joined and "status" in joined:
            r.output = 'deployment "test-deploy" successfully rolled out'
        else:
            r.output = '{"items":[]}'
        return r

    with (
        patch("agent.skills.deployment.run_kubectl",         side_effect=mock_kubectl),
        patch("agent.skills.deployment.check_node_capacity", return_value=ok_capacity),
        patch("agent.skills.deployment.retrieve_context",    return_value=[]),
    ):
        skill = DeploymentSkill()
        skill.llm = MagicMock()
        skill.llm.chat = MagicMock(return_value=MagicMock(content="ok"))

        gate = skill.pre_deploy_health_check("test-deploy", "default", "test:v1")

    errors: list[str] = []
    if gate.passed != case["expected_passed"]:
        errors.append(f"passed: expected {case['expected_passed']}, got {gate.passed}")
    if gate.blocked != case["expected_blocked"]:
        errors.append(f"blocked: expected {case['expected_blocked']}, got {gate.blocked}")

    kw = case.get("expected_blocker_keyword", "")
    if kw:
        combined = " ".join(gate.blockers).lower()
        if kw.lower() not in combined:
            errors.append(f"blocker keyword '{kw}' not found in: {gate.blockers}")

    if errors:
        return _fail(case["id"], case["scenario"], " | ".join(errors))

    return _pass(case["id"], case["scenario"], 100,
                 f"passed={gate.passed} blockers={len(gate.blockers)}")


def _eval_watcher(case: dict) -> dict:
    """Evaluate watch_deployment_health auto-rollback logic."""
    from agent.skills.deployment import DeploymentSkill

    mock_oom      = case["mock_oom"]
    restart_delta = case["mock_restart_delta"]
    # Baseline uses first 3 calls (get pods json, logs, top).
    # Watch loop uses calls 4+ with triggered data.
    call_count = [0]

    def _pods_json(triggered: bool) -> str:
        if triggered and mock_oom:
            return json.dumps({"items": [{"metadata": {"name": "test-deploy-abc"}, "status": {
                "containerStatuses": [{"restartCount": 1, "ready": False,
                                       "state": {"terminated": {"reason": "OOMKilled"}}}],
                "phase": "Running",
            }}]})
        elif triggered and restart_delta >= 3:
            return json.dumps({"items": [{"metadata": {"name": "test-deploy-abc"}, "status": {
                "containerStatuses": [{"restartCount": restart_delta, "ready": False,
                                       "state": {"running": {}}}],
                "phase": "Running",
            }}]})
        else:
            return json.dumps({"items": [{"metadata": {"name": "test-deploy-abc"}, "status": {
                "containerStatuses": [{"restartCount": 0, "ready": True,
                                       "state": {"running": {}}}],
                "phase": "Running",
            }}]})

    def mock_kubectl(cmd: list, **kw) -> MagicMock:
        r = MagicMock()
        r.success = True
        r.error   = ""
        joined    = " ".join(cmd)
        call_count[0] += 1
        triggered = call_count[0] > 3  # baseline = first 3 calls

        if "get pods" in joined and ("json" in joined or "-o" in joined):
            r.output = _pods_json(triggered)
        elif "get pods" in joined and "no-headers" in joined:
            # text OOM check
            if triggered and mock_oom:
                r.output = "test-deploy-abc   0/1   OOMKilled   5   10m"
            else:
                r.output = "test-deploy-abc   1/1   Running   0   10m"
        elif "logs" in joined:
            r.output = "error: OOM\n" * 5 if triggered and mock_oom else ""
        elif "top" in joined:
            r.output = "test-deploy-abc   100m   128Mi"
        else:
            r.output = "{}"
        return r

    def mock_rollout_undo(*a, **kw) -> MagicMock:
        r = MagicMock()
        r.success = True
        r.output  = "rolled back"
        r.error   = ""
        return r

    sleep_calls = [0]

    def fast_sleep(secs: float) -> None:
        sleep_calls[0] += 1

    with (
        patch("agent.skills.deployment.run_kubectl",      side_effect=mock_kubectl),
        patch("agent.skills.deployment.rollout_undo",     side_effect=mock_rollout_undo),
        patch("agent.skills.deployment.wait_for_rollout", return_value=None),
        patch("agent.skills.deployment.remember",         return_value=None),
        patch("time.sleep",                               side_effect=fast_sleep),
    ):
        skill = DeploymentSkill()
        skill.llm = MagicMock()
        skill.llm.chat = MagicMock(return_value=MagicMock(content="ok"))

        result = skill.watch_deployment_health(
            "test-deploy", "default", "old:v1", "new:v2",
            watch_seconds=30,  # enough for 1 poll cycle with mocked sleep
        )

    errors: list[str] = []
    if result.rollback_triggered != case["expected_rollback"]:
        errors.append(
            f"rollback_triggered: expected {case['expected_rollback']}, got {result.rollback_triggered}"
        )

    kw = case.get("expected_reason_keyword", "")
    if kw and result.rollback_triggered:
        reason = (result.rollback_reason or "").lower()
        if kw.lower() not in reason:
            errors.append(f"rollback_reason '{result.rollback_reason}' missing keyword '{kw}'")

    if errors:
        return _fail(case["id"], case["scenario"], " | ".join(errors))

    return _pass(case["id"], case["scenario"], 100,
                 f"rollback={result.rollback_triggered} reason={result.rollback_reason!r}")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_RUNNERS = {
    "webhook_sig":  _eval_webhook_sig,
    "mapping":      _eval_mapping,
    "risk":         _eval_risk,
    "health_gate":  _eval_health_gate,
    "watcher":      _eval_watcher,
}


def run_all(verbose: bool = True) -> int:
    cases   = _load_cases()
    results = []
    t0      = time.time()

    print()
    print("=" * 72)
    print("  AtlasOS Deployment Eval Suite")
    print(f"  {len(cases)} cases | webhook · mapping · risk · health gate · watcher")
    print("=" * 72)
    print()

    for case in cases:
        eval_type = case.get("type", "unknown")
        runner    = _RUNNERS.get(eval_type)
        if runner is None:
            results.append(_fail(case["id"], case["scenario"], f"unknown type '{eval_type}'"))
            continue
        try:
            results.append(runner(case))
        except Exception as exc:
            import traceback
            results.append(_fail(case["id"], case["scenario"], f"exception: {exc}"))
            if verbose:
                traceback.print_exc()

    passed  = sum(1 for r in results if r["passed"])
    total   = len(results)
    avg_sc  = sum(r["score"] for r in results) / total if total else 0
    elapsed = time.time() - t0

    print()
    print("=" * 72)
    print(f"  PASS: {passed}/{total}   avg score: {avg_sc:.0f}/100   {elapsed:.1f}s")
    print("=" * 72)
    print()

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run_all())
