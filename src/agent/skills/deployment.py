"""
Deployment Management skill — rollback, scale, deploy, restart with Claude safety gates.

Every write operation is analyzed by Claude before execution.
Production namespaces require name confirmation.
Auto-rollback fires if new pods crash within 60s of deploy.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import (
    CheckResult,
    DeploymentAction,
    DeploymentInfo,
    DeployReport,
    HealthGateResult,
    PendingDeploy,
    Revision,
    RiskScore,
    WatchResult,
    WatchSample,
    WebhookEvent,
    WebhookMapping,
)
from agent.integrations.kubectl import (
    check_image_exists,
    check_node_capacity,
    get_deployment_history,
    get_deployment_info,
    get_previous_revision,
    restart_deployment,
    rollout_undo,
    run_kubectl,
    scale_deployment,
    set_image,
    wait_for_rollout,
)
from agent.memory.retrieval import remember, retrieve_context
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_PROD_NAMESPACES = {"prod", "production", "live", "prd"}

_SAFETY_SYSTEM = """You are a senior SRE evaluating a Kubernetes deployment operation for production safety.
Be concise and direct. Return ONLY valid JSON — no markdown, no extra text.

Analyze the operation and return:
{
  "is_safe": true or false,
  "risk_level": "low" or "medium" or "high",
  "downtime_estimate": "string like ~0s, ~15s, ~2min",
  "warnings": ["list", "of", "specific", "warnings"],
  "analysis": "2-3 sentence plain English explanation of risks and recommendation"
}

Guidelines:
- RollingUpdate strategy with replicas >= 2: low downtime risk
- Recreate strategy: always causes downtime
- Rolling back when current version is crashing: low risk (recovery)
- Rolling back healthy version without clear reason: medium/high risk
- Scaling to 0: high risk, mention explicitly
- Insufficient node capacity: high risk warning
- No resource limits on deployment: medium risk warning
"""


def _is_production(namespace: str) -> bool:
    ns = namespace.lower()
    return any(p in ns for p in _PROD_NAMESPACES)


class DeploymentSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "deployment"

    @property
    def description(self) -> str:
        return "Rollback, scale, deploy, and restart Kubernetes deployments with Claude safety analysis."

    def execute(self, input_data: dict) -> dict:
        action = input_data.get("action", "")
        dep    = input_data.get("deployment", "")
        ns     = input_data.get("namespace", "default")
        if action == "rollback":
            return self.analyze_rollback(dep, ns).model_dump()
        if action == "scale":
            return self.analyze_scale(dep, ns, input_data.get("replicas", 1)).model_dump()
        return {"error": "unknown action"}

    # ------------------------------------------------------------------
    # Analyze methods — call Claude, return DeploymentAction (no writes)
    # ------------------------------------------------------------------

    def analyze_rollback(
        self,
        deployment: str,
        namespace: str,
        to_revision: int | None = None,
    ) -> DeploymentAction:
        info = get_deployment_info(deployment, namespace)
        if info is None:
            return DeploymentAction(
                action_type="rollback", deployment=deployment, namespace=namespace,
                is_safe=False, risk_level="high",
                claude_analysis=f"Deployment '{deployment}' not found in namespace '{namespace}'.",
                warnings=[f"Deployment not found: {deployment}"],
            )

        prev = (
            next((r for r in get_deployment_history(deployment, namespace) if r.revision_number == to_revision), None)
            if to_revision else
            get_previous_revision(deployment, namespace)
        )

        if prev is None:
            return DeploymentAction(
                action_type="rollback", deployment=deployment, namespace=namespace,
                is_safe=False, risk_level="high",
                current_state=f"revision {info.revision}, image {info.current_image}",
                proposed_state="no previous revision available",
                claude_analysis="No previous revision found — rollback is not possible.",
                warnings=["No previous revision to roll back to."],
                command="",
            )

        past_context = retrieve_context(f"deployment rollback {deployment}")
        cmd = f"kubectl rollout undo deployment/{deployment} -n {namespace}"
        if to_revision:
            cmd += f" --to-revision={to_revision}"

        analysis = self._call_claude(f"""
Kubernetes rollback analysis:
Deployment: {deployment} | Namespace: {namespace}
Strategy: {info.strategy} | Replicas: {info.replicas_ready}/{info.replicas_desired}
Current: revision {info.revision}, image: {info.current_image}
Rollback target: revision {prev.revision_number}, image: {prev.image}
Current healthy: {info.healthy}
Change cause (current): {prev.change_cause}
Past context: {past_context[:500] if past_context else 'none'}
""")

        return DeploymentAction(
            action_type      = "rollback",
            deployment       = deployment,
            namespace        = namespace,
            current_state    = f"revision {info.revision}, image: {info.current_image}, {info.replicas_ready}/{info.replicas_desired} ready",
            proposed_state   = f"revision {prev.revision_number}, image: {prev.image}",
            is_safe          = analysis.get("is_safe", True),
            risk_level       = analysis.get("risk_level", "low"),
            downtime_estimate = analysis.get("downtime_estimate", "~15s"),
            warnings         = analysis.get("warnings", []),
            claude_analysis  = analysis.get("analysis", ""),
            command          = cmd,
            is_production    = _is_production(namespace),
        )

    def analyze_scale(
        self,
        deployment: str,
        namespace: str,
        target_replicas: int,
    ) -> DeploymentAction:
        info = get_deployment_info(deployment, namespace)
        if info is None:
            return DeploymentAction(
                action_type="scale", deployment=deployment, namespace=namespace,
                is_safe=False, risk_level="high",
                claude_analysis=f"Deployment '{deployment}' not found.",
                warnings=[f"Deployment not found: {deployment}"],
            )

        current = info.replicas_desired
        cmd     = f"kubectl scale deployment/{deployment} -n {namespace} --replicas={target_replicas}"

        # Check capacity if scaling up
        capacity = {}
        if target_replicas > current:
            extra = target_replicas - current
            capacity = check_node_capacity(
                cpu_needed_m      = extra * 100,
                memory_needed_mb  = extra * 128,
            )

        # Check HPA
        hpa_r = run_kubectl(["get", "hpa", "-n", namespace, "--no-headers"], timeout=10)
        hpa_conflict = hpa_r.success and deployment in hpa_r.output

        analysis = self._call_claude(f"""
Kubernetes scale analysis:
Deployment: {deployment} | Namespace: {namespace}
Current replicas: {current} → Target replicas: {target_replicas}
Strategy: {info.strategy}
Cluster capacity: {json.dumps(capacity) if capacity else 'not checked (scaling down)'}
HPA conflict: {hpa_conflict}
Scale to zero: {target_replicas == 0}
""")

        warnings = analysis.get("warnings", [])
        if target_replicas == 0:
            warnings.insert(0, "SCALING TO ZERO — this stops ALL pods. Service will be unavailable.")
        if hpa_conflict:
            warnings.append(f"HPA exists for {deployment} — manual scale may be overridden immediately.")
        if capacity and not capacity.get("has_capacity"):
            warnings.append(
                f"Insufficient cluster capacity — free CPU: {capacity['free_cpu_m']}m, free mem: {capacity['free_mem_mb']}Mi"
            )

        direction = "up" if target_replicas > current else ("to zero" if target_replicas == 0 else "down")
        return DeploymentAction(
            action_type      = "scale",
            deployment       = deployment,
            namespace        = namespace,
            current_state    = f"{current} replicas ({info.replicas_ready} ready)",
            proposed_state   = f"{target_replicas} replicas",
            is_safe          = analysis.get("is_safe", True) and target_replicas > 0,
            risk_level       = "high" if target_replicas == 0 else analysis.get("risk_level", "low"),
            downtime_estimate = analysis.get("downtime_estimate", "~0s"),
            warnings         = warnings,
            claude_analysis  = analysis.get("analysis", ""),
            command          = cmd,
            is_production    = _is_production(namespace),
        )

    def analyze_deploy(
        self,
        deployment: str,
        namespace: str,
        new_image: str,
    ) -> DeploymentAction:
        info = get_deployment_info(deployment, namespace)
        if info is None:
            return DeploymentAction(
                action_type="deploy", deployment=deployment, namespace=namespace,
                is_safe=False, risk_level="high",
                claude_analysis=f"Deployment '{deployment}' not found.",
                warnings=[f"Deployment not found: {deployment}"],
            )

        container = info.containers[0] if info.containers else deployment
        image_ok  = check_image_exists(new_image)
        cmd       = f"kubectl set image deployment/{deployment} {container}={new_image} -n {namespace}"

        # Check resource limits
        dep_r = run_kubectl(["get", "deployment", deployment, "-n", namespace, "-o", "json"])
        has_limits = False
        if dep_r.success:
            try:
                for c in json.loads(dep_r.output)["spec"]["template"]["spec"]["containers"]:
                    if c.get("resources", {}).get("limits"):
                        has_limits = True
                        break
            except Exception:
                pass

        analysis = self._call_claude(f"""
Kubernetes deploy analysis:
Deployment: {deployment} | Namespace: {namespace}
Current image: {info.current_image}
New image: {new_image}
Image verified in cluster: {image_ok}
Strategy: {info.strategy}
Replicas: {info.replicas_desired}
Resource limits set: {has_limits}
Container: {container}
""")

        warnings = analysis.get("warnings", [])
        if not image_ok:
            warnings.insert(0, f"Image '{new_image}' not verified — ensure registry is accessible before deploying.")
        if not has_limits:
            warnings.append("No resource limits set — pod may consume unlimited CPU/memory.")

        return DeploymentAction(
            action_type      = "deploy",
            deployment       = deployment,
            namespace        = namespace,
            current_state    = f"image: {info.current_image}",
            proposed_state   = f"image: {new_image}",
            is_safe          = analysis.get("is_safe", True),
            risk_level       = analysis.get("risk_level", "low"),
            downtime_estimate = analysis.get("downtime_estimate", "~15s"),
            warnings         = warnings,
            claude_analysis  = analysis.get("analysis", ""),
            command          = cmd,
            is_production    = _is_production(namespace),
        )

    def analyze_restart(self, deployment: str, namespace: str) -> DeploymentAction:
        info = get_deployment_info(deployment, namespace)
        if info is None:
            return DeploymentAction(
                action_type="restart", deployment=deployment, namespace=namespace,
                is_safe=False, risk_level="high",
                claude_analysis=f"Deployment '{deployment}' not found.",
                warnings=[f"Deployment not found: {deployment}"],
            )

        cmd = f"kubectl rollout restart deployment/{deployment} -n {namespace}"
        analysis = self._call_claude(f"""
Kubernetes rolling restart analysis:
Deployment: {deployment} | Namespace: {namespace}
Strategy: {info.strategy} | Replicas: {info.replicas_desired}
Current health: {info.replicas_ready}/{info.replicas_desired} ready
Image: {info.current_image}
""")

        return DeploymentAction(
            action_type      = "restart",
            deployment       = deployment,
            namespace        = namespace,
            current_state    = f"{info.replicas_ready}/{info.replicas_desired} ready, image: {info.current_image}",
            proposed_state   = "rolling restart — same image, fresh containers",
            is_safe          = analysis.get("is_safe", True),
            risk_level       = analysis.get("risk_level", "low"),
            downtime_estimate = analysis.get("downtime_estimate", "~15s per pod"),
            warnings         = analysis.get("warnings", []),
            claude_analysis  = analysis.get("analysis", ""),
            command          = cmd,
            is_production    = _is_production(namespace),
        )

    # ------------------------------------------------------------------
    # Execute — called ONLY after user approval
    # ------------------------------------------------------------------

    def execute_action(self, action: DeploymentAction, auto_rollback: bool = True) -> bool:
        dep = action.deployment
        ns  = action.namespace

        log.info("deployment.execute", action=action.action_type, deployment=dep, namespace=ns)

        if action.action_type == "rollback":
            r = rollout_undo(dep, ns)
        elif action.action_type == "scale":
            replicas = int(action.proposed_state.split()[0])
            r = scale_deployment(dep, ns, replicas)
        elif action.action_type == "deploy":
            new_image = action.proposed_state.replace("image: ", "")
            info = get_deployment_info(dep, ns)
            container = info.containers[0] if info and info.containers else dep
            r = set_image(dep, ns, container, new_image)
        elif action.action_type == "restart":
            r = restart_deployment(dep, ns)
        else:
            log.warning("deployment.execute.unknown_action", action=action.action_type)
            return False

        if not r.success:
            log.warning("deployment.execute.kubectl_failed", error=r.error)
            remember(
                content=f"Deployment {action.action_type} FAILED: {dep} [{ns}] — {r.error[:200]}",
                source="deployment",
                metadata={"deployment": dep, "namespace": ns, "action": action.action_type, "success": False},
            )
            return False

        # Wait for rollout (skip for scale-to-zero)
        replicas_target = 0
        if action.action_type == "scale":
            try:
                replicas_target = int(action.proposed_state.split()[0])
            except Exception:
                pass

        if action.action_type != "scale" or replicas_target > 0:
            wait_for_rollout(dep, ns, timeout=120)

        # Auto-rollback safety net for deploy
        if action.action_type == "deploy" and auto_rollback:
            success = self.auto_rollback_on_failure(dep, ns)
            if not success:
                return False

        remember(
            content=(
                f"Deployment {action.action_type} succeeded: {dep} [{ns}] "
                f"— {action.current_state} → {action.proposed_state}"
            ),
            source="deployment",
            metadata={"deployment": dep, "namespace": ns, "action": action.action_type, "success": True},
        )
        return True

    def auto_rollback_on_failure(self, deployment: str, namespace: str, wait_secs: int = 60) -> bool:
        """Wait wait_secs, check pod health. Auto-rollback if crashing."""
        log.info("deployment.auto_rollback_watch", deployment=deployment, wait_secs=wait_secs)
        time.sleep(wait_secs)

        info = get_deployment_info(deployment, namespace)
        if info and info.healthy:
            log.info("deployment.auto_rollback_not_needed", deployment=deployment)
            return True

        log.warning("deployment.auto_rollback_firing", deployment=deployment)
        r = rollout_undo(deployment, namespace)
        if r.success:
            wait_for_rollout(deployment, namespace, timeout=120)
            remember(
                content=f"AUTO-ROLLBACK fired for {deployment} [{namespace}] — new pods were unhealthy after deploy.",
                source="deployment",
                metadata={"deployment": deployment, "namespace": namespace, "action": "auto-rollback"},
            )
        return False  # signal to caller that auto-rollback fired

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _call_claude(self, prompt: str) -> dict:
        try:
            resp = run_sync(llm.chat(
                messages=[context.user_message(prompt)],
                system=_SAFETY_SYSTEM,
                json_mode=True,
                max_tokens=512,
            ))
            raw = resp.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except Exception as exc:
            log.warning("deployment.claude_failed", error=str(exc))
            return {
                "is_safe": True, "risk_level": "medium",
                "downtime_estimate": "unknown",
                "warnings": ["AI analysis unavailable — review manually before proceeding."],
                "analysis": f"Claude analysis unavailable: {exc}",
            }

    # ------------------------------------------------------------------
    # Risk Scorer
    # ------------------------------------------------------------------

    def score_deployment_risk(
        self,
        deployment: str,
        namespace: str,
        new_image: str,
        current_image: str = "",
    ) -> RiskScore:
        factors: list[tuple[str, int]] = []
        total = 0

        def _add(label: str, pts: int) -> None:
            nonlocal total
            factors.append((label, pts))
            total += pts

        # Namespace risk
        ns = namespace.lower()
        if any(p in ns for p in ("prod", "production", "live", "prd")):
            _add("Production namespace", 40)
        elif "staging" in ns:
            _add("Staging namespace", 20)
        elif any(p in ns for p in ("dev", "test", "local")):
            pass  # +0
        else:
            _add("Non-standard namespace", 10)

        # Image change risk
        if new_image:
            tag = new_image.split(":")[-1] if ":" in new_image else "latest"
            name = new_image.split(":")[0]
            cur_name = current_image.split(":")[0] if current_image else ""
            cur_tag  = current_image.split(":")[-1] if ":" in current_image else ""

            if tag == "latest":
                _add("'latest' tag — no pinned version", 25)
            elif not re.match(r"^[a-zA-Z0-9._-]+$", tag):
                _add("Unusual image tag format", 15)

            if name != cur_name and cur_name:
                _add("Different image name (not just a tag bump)", 40)
            elif cur_tag and tag != cur_tag:
                # Try semver comparison
                def _parts(t: str) -> list[int]:
                    t = t.lstrip("v")
                    try:
                        return [int(x) for x in t.split(".")[:3]]
                    except ValueError:
                        return []

                np, cp = _parts(tag), _parts(cur_tag)
                if not np or not cp:
                    _add("Unknown/unverified tag change", 35)
                elif len(np) >= 1 and len(cp) >= 1 and np[0] != cp[0]:
                    _add("Major version bump", 30)
                elif len(np) >= 2 and len(cp) >= 2 and np[1] != cp[1]:
                    _add("Minor version bump", 15)
                else:
                    _add("Patch version bump", 5)
            elif not cur_tag:
                _add("No previous image to compare", 10)

        # Cluster state risk
        cap = check_node_capacity(0, 0)
        if cap.get("free_cpu_m", 9999) < 500 or cap.get("free_mem_mb", 9999) < 256:
            _add("Cluster capacity low", 15)

        pods_r = run_kubectl(["get", "pods", "-n", namespace, "--no-headers"], timeout=10)
        if pods_r.success:
            bad = sum(
                1 for l in pods_r.output.splitlines()
                if any(s in l for s in ("CrashLoopBackOff", "Error", "OOMKilled"))
            )
            if bad:
                _add(f"{bad} unhealthy pod(s) in namespace", 20)

        # History risk
        _past_mems = retrieve_context(f"deployment {deployment} {namespace}")
        past = " ".join(getattr(m, "content", str(m)) for m in _past_mems) if _past_mems else ""
        if past:
            if "FAILED" in past or "auto-rollback" in past.lower():
                _add("Last deploy to this deployment failed", 20)
        else:
            _add("No deploy history found", 10)

        hist = get_deployment_history(deployment, namespace)
        if len(hist) < 2:
            _add("No previous revision (no rollback available)", 15)

        # Cap at 100
        total = min(total, 100)

        if total <= 20:
            label = "low"
        elif total <= 40:
            label = "medium"
        elif total <= 60:
            label = "high"
        else:
            label = "critical"

        # Recommendations
        rec_parts = []
        if total >= 40:
            rec_parts.append("Consider deploying to staging first.")
        if any("latest" in f for f, _ in factors):
            rec_parts.append("Pin image to a specific tag.")
        if any("capacity" in f.lower() for f, _ in factors):
            rec_parts.append("Check node capacity before proceeding.")
        recommendation = " ".join(rec_parts) or "Proceed with standard approval."

        # Auto-approve only for low risk + non-production
        from agent.config import settings
        auto_approve = (
            label == "low"
            and not any(p in namespace.lower() for p in ("prod", "production", "live", "prd"))
            and settings.auto_approve_low_risk
        )

        return RiskScore(
            total          = total,
            label          = label,
            factors        = factors,
            recommendation = recommendation,
            auto_approve   = auto_approve,
        )

    # ------------------------------------------------------------------
    # Pre-Deploy Health Gate
    # ------------------------------------------------------------------

    def pre_deploy_health_check(
        self,
        deployment: str,
        namespace: str,
        new_image: str,
    ) -> HealthGateResult:
        import time as _time
        start_ms = int(_time.monotonic() * 1000)

        checks:   list[CheckResult] = []
        warnings: list[str]         = []
        blockers: list[str]         = []

        # Check 1: Cluster capacity
        try:
            cap = check_node_capacity(200, 256)
            free_cpu = cap.get("free_cpu_m", 0)
            free_mem = cap.get("free_mem_mb", 0)
            total_cpu = cap.get("total_cpu_m", max(free_cpu, 1))
            total_mem = cap.get("total_mem_mb", max(free_mem, 1))
            cpu_pct = int((1 - free_cpu / max(total_cpu, 1)) * 100)
            mem_pct = int((1 - free_mem / max(total_mem, 1)) * 100)

            if cpu_pct > 90 or mem_pct > 90:
                checks.append(CheckResult(
                    name="Cluster capacity", passed=False, severity="block",
                    message=f"CPU {cpu_pct}% used, Mem {mem_pct}% used — < 10% headroom",
                    detail="Insufficient capacity for new pods.",
                ))
                blockers.append(f"Cluster capacity critical — CPU {cpu_pct}%, Mem {mem_pct}%")
            elif cpu_pct > 80 or mem_pct > 80:
                checks.append(CheckResult(
                    name="Cluster capacity", passed=True, severity="warn",
                    message=f"CPU {cpu_pct}% used, Mem {mem_pct}% used — headroom tight",
                ))
                warnings.append("Cluster capacity is tight — monitor closely after deploy.")
            else:
                checks.append(CheckResult(
                    name="Cluster capacity", passed=True, severity="pass",
                    message=f"CPU {cpu_pct}% used, Mem {mem_pct}% used ({100-cpu_pct}% free)",
                ))
        except Exception as exc:
            checks.append(CheckResult(
                name="Cluster capacity", passed=True, severity="pass",
                message="Capacity check skipped (metrics-server unavailable)",
                detail=str(exc)[:80],
            ))

        # Check 2: Namespace pod health
        pods_r = run_kubectl(["get", "pods", "-n", namespace, "-o", "json"], timeout=15)
        try:
            pods_data = json.loads(pods_r.output).get("items", []) if pods_r.success else []
            total_pods   = len(pods_data)
            crashing     = [
                p["metadata"]["name"] for p in pods_data
                if any(
                    cs.get("state", {}).get("waiting", {}).get("reason", "") in
                    ("CrashLoopBackOff", "Error", "OOMKilled", "ImagePullBackOff")
                    for cs in p.get("status", {}).get("containerStatuses", [])
                )
            ]
            pending_pods = [
                p["metadata"]["name"] for p in pods_data
                if p.get("status", {}).get("phase") == "Pending"
            ]
            ready_pods = total_pods - len(crashing) - len(pending_pods)

            if crashing:
                checks.append(CheckResult(
                    name="Pod health", passed=False, severity="block",
                    message=f"{len(crashing)} pod(s) crashing: {', '.join(crashing[:3])}",
                ))
                blockers.append(f"Unhealthy pods in namespace: {', '.join(crashing[:3])}")
            elif pending_pods:
                checks.append(CheckResult(
                    name="Pod health", passed=True, severity="warn",
                    message=f"{ready_pods}/{total_pods} ready, {len(pending_pods)} pending",
                ))
                warnings.append(f"{len(pending_pods)} pod(s) still pending in namespace.")
            else:
                checks.append(CheckResult(
                    name="Pod health", passed=True, severity="pass",
                    message=f"{ready_pods}/{total_pods} Running and Ready",
                ))
        except Exception:
            checks.append(CheckResult(
                name="Pod health", passed=True, severity="pass",
                message="Pod health check skipped",
            ))

        # Check 3: Active incidents
        incident_ctx = retrieve_context(f"incident {namespace} failed crashing")
        if incident_ctx and len(incident_ctx) > 20:
            checks.append(CheckResult(
                name="Active incidents", passed=True, severity="warn",
                message="Recent incident found in memory — review before deploying",
                detail=incident_ctx[:120],
            ))
            warnings.append("Recent incident detected in this namespace.")
        else:
            checks.append(CheckResult(
                name="Active incidents", passed=True, severity="pass",
                message="No recent incidents found",
            ))

        # Check 4: TLS certificate health
        tls_r = run_kubectl(
            ["get", "certificates", "-n", namespace, "-o", "json"], timeout=10
        )
        try:
            if tls_r.success:
                certs = json.loads(tls_r.output).get("items", [])
                expired, expiring_soon = [], []
                for cert in certs:
                    cname = cert["metadata"]["name"]
                    for cond in cert.get("status", {}).get("conditions", []):
                        if cond.get("type") == "Ready" and cond.get("status") != "True":
                            expired.append(cname)
                    not_after = cert.get("status", {}).get("notAfter", "")
                    if not_after:
                        try:
                            exp = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
                            days = (exp - datetime.now(timezone.utc)).days
                            if days < 0:
                                expired.append(cname)
                            elif days < 30:
                                expiring_soon.append(f"{cname}: {days}d")
                        except Exception:
                            pass

                if expired:
                    checks.append(CheckResult(
                        name="TLS certificates", passed=False, severity="block",
                        message=f"Expired cert(s): {', '.join(expired[:3])}",
                    ))
                    blockers.append(f"TLS certificate expired: {', '.join(expired[:3])}")
                elif expiring_soon:
                    checks.append(CheckResult(
                        name="TLS certificates", passed=True, severity="warn",
                        message=f"Expiring soon: {', '.join(expiring_soon[:3])}",
                    ))
                    warnings.append(f"TLS certificate expiring: {', '.join(expiring_soon[:3])}")
                else:
                    checks.append(CheckResult(
                        name="TLS certificates", passed=True, severity="pass",
                        message="All certificates valid",
                    ))
            else:
                checks.append(CheckResult(
                    name="TLS certificates", passed=True, severity="pass",
                    message="cert-manager not installed — skipped",
                ))
        except Exception:
            checks.append(CheckResult(
                name="TLS certificates", passed=True, severity="pass",
                message="TLS check skipped",
            ))

        # Check 5: Image verification
        img_ok = check_image_exists(new_image)
        if img_ok:
            checks.append(CheckResult(
                name="Image verified", passed=True, severity="pass",
                message=f"{new_image} confirmed in cluster",
            ))
        else:
            checks.append(CheckResult(
                name="Image verified", passed=True, severity="warn",
                message=f"{new_image} not found running in cluster — verify registry",
            ))
            warnings.append(f"Image '{new_image}' not verified in cluster.")

        # Check 6: Deploy frequency guard
        _recent_mems = retrieve_context(f"deployment deploy {deployment} {namespace} succeeded")
        recent_ctx = " ".join(getattr(m, "content", str(m)) for m in _recent_mems) if _recent_mems else ""
        in_progress_r = run_kubectl(
            ["rollout", "status", f"deployment/{deployment}", "-n", namespace,
             "--timeout=2s"], timeout=5
        )
        if in_progress_r.success and "successfully rolled out" not in in_progress_r.output.lower():
            checks.append(CheckResult(
                name="Deploy frequency", passed=False, severity="block",
                message="A rollout is currently IN PROGRESS — wait for it to complete",
            ))
            blockers.append("Rollout already in progress — cannot deploy concurrently.")
        elif recent_ctx and "succeeded" in recent_ctx.lower():
            checks.append(CheckResult(
                name="Deploy frequency", passed=True, severity="warn",
                message="Recent deploy found in memory — check cooldown",
            ))
            warnings.append("Recent deploy to this deployment — ensure cooldown has passed.")
        else:
            checks.append(CheckResult(
                name="Deploy frequency", passed=True, severity="pass",
                message="No recent deploys detected",
            ))

        duration_ms = int(_time.monotonic() * 1000) - start_ms
        blocked = len(blockers) > 0
        passed  = not blocked

        if blocked:
            recommendation = "Fix blockers before deploying: " + "; ".join(blockers[:2])
        elif warnings:
            recommendation = "Proceed with caution: " + "; ".join(warnings[:2])
        else:
            recommendation = "All checks passed — safe to deploy."

        return HealthGateResult(
            passed           = passed,
            blocked          = blocked,
            checks           = checks,
            warnings         = warnings,
            blockers         = blockers,
            recommendation   = recommendation,
            check_duration_ms = duration_ms,
        )

    # ------------------------------------------------------------------
    # Smart Post-Deploy Watcher
    # ------------------------------------------------------------------

    def watch_deployment_health(
        self,
        deployment: str,
        namespace: str,
        old_image: str,
        new_image: str,
        watch_seconds: int = 120,
        on_sample: "callable | None" = None,
    ) -> WatchResult:
        """Poll every 10s, auto-rollback on crash/OOM/restart spike."""
        samples:          list[WatchSample] = []
        baseline_errors   = 0
        baseline_memory   = 0
        baseline_restarts: dict[str, int] = {}
        start             = time.monotonic()

        def _snapshot() -> tuple[int, int, dict[str, int], int, int]:
            """Returns (ready, total, restart_map, error_lines, mem_mi)."""
            pods_r = run_kubectl(
                ["get", "pods", "-n", namespace, "-o", "json"], timeout=15
            )
            ready = total = 0
            restarts: dict[str, int] = {}
            mem_mi = 0

            if pods_r.success:
                try:
                    items = json.loads(pods_r.output).get("items", [])
                    dep_pods = [
                        p for p in items
                        if deployment in p["metadata"]["name"]
                    ]
                    total = len(dep_pods)
                    for p in dep_pods:
                        pname = p["metadata"]["name"]
                        css = p.get("status", {}).get("containerStatuses", [])
                        if all(cs.get("ready", False) for cs in css) and css:
                            ready += 1
                        restarts[pname] = sum(
                            cs.get("restartCount", 0) for cs in css
                        )
                except Exception:
                    pass

            # Log errors (last 15s)
            err_count = 0
            logs_r = run_kubectl(
                ["logs", f"deployment/{deployment}", "-n", namespace,
                 "--since=15s", "--tail=200"], timeout=10
            )
            if logs_r.success:
                err_count = sum(
                    1 for l in logs_r.output.splitlines()
                    if any(kw in l.upper() for kw in
                           ("ERROR", "FATAL", "EXCEPTION", "PANIC", "CRITICAL"))
                )

            # Memory (best-effort)
            top_r = run_kubectl(
                ["top", "pods", "-n", namespace, "--no-headers"], timeout=10
            )
            if top_r.success:
                try:
                    vals = []
                    for line in top_r.output.splitlines():
                        if deployment in line:
                            parts = line.split()
                            if len(parts) >= 3:
                                mem_str = parts[2].rstrip("Mi")
                                vals.append(int(mem_str))
                    if vals:
                        mem_mi = sum(vals) // len(vals)
                except Exception:
                    pass

            return ready, total, restarts, err_count, mem_mi

        # Baseline snapshot before watching
        b_ready, b_total, baseline_restarts, baseline_errors, baseline_memory = _snapshot()

        rollback_triggered = False
        rollback_reason: str | None = None

        while True:
            elapsed = int(time.monotonic() - start)
            if elapsed >= watch_seconds:
                break

            time.sleep(10)
            elapsed = int(time.monotonic() - start)

            ready, total, restarts, err_lines, mem_mi = _snapshot()

            # Restart delta vs baseline
            restart_delta = sum(
                max(0, restarts.get(p, 0) - baseline_restarts.get(p, 0))
                for p in restarts
            )

            # Error rate spike
            err_spike = (
                err_lines > max(baseline_errors * 5, 10)
                if baseline_errors > 0
                else err_lines > 20
            )

            # OOM/crash check
            pods_r2 = run_kubectl(
                ["get", "pods", "-n", namespace, "--no-headers"], timeout=10
            )
            oom_found = pods_r2.success and "OOMKilled" in pods_r2.output

            # Memory growth
            mem_delta_pct = (
                int((mem_mi - baseline_memory) / max(baseline_memory, 1) * 100)
                if baseline_memory > 0 else 0
            )

            event = "pass"
            msg   = ""

            if oom_found:
                event = "rollback"
                msg   = "OOMKilled detected — triggering auto-rollback"
            elif restart_delta >= 3:
                event = "rollback"
                msg   = f"Pod restart count exceeded threshold (delta={restart_delta})"
            elif err_spike:
                event = "rollback"
                msg   = f"Error rate spike detected ({err_lines} errors in 15s)"
            elif mem_delta_pct > 50:
                event = "warn"
                msg   = f"Memory growing ({mem_delta_pct}% increase)"
            elif ready < total and total > 0:
                event = "warn"
                msg   = f"Pods not all ready: {ready}/{total}"

            sample = WatchSample(
                elapsed_s     = elapsed,
                ready_count   = ready,
                total_count   = total,
                restart_delta = restart_delta,
                error_lines   = err_lines,
                memory_mi     = mem_mi,
                event         = event,
                message       = msg,
            )
            samples.append(sample)

            if on_sample:
                on_sample(sample)

            if event == "rollback":
                log.warning("deployment.watcher.auto_rollback", reason=msg)
                rollout_undo(deployment, namespace)
                wait_for_rollout(deployment, namespace, timeout=120)
                rollback_triggered = True
                rollback_reason    = msg
                remember(
                    content=f"AUTO-ROLLBACK: {deployment} [{namespace}] — {msg}",
                    source="deployment",
                    metadata={"deployment": deployment, "namespace": namespace,
                              "action": "auto-rollback", "reason": msg},
                )
                break

        final_ready, final_total, _, final_errors, _ = _snapshot()
        duration = int(time.monotonic() - start)

        return WatchResult(
            success              = not rollback_triggered and final_ready == final_total,
            rollback_triggered   = rollback_triggered,
            rollback_reason      = rollback_reason,
            samples              = samples,
            final_ready_count    = final_ready,
            errors_detected      = final_errors,
            memory_delta_percent = (
                int((samples[-1].memory_mi - baseline_memory) / max(baseline_memory, 1) * 100)
                if samples and baseline_memory > 0 else 0
            ),
            restart_count        = sum(s.restart_delta for s in samples),
            duration_seconds     = duration,
        )

    # ------------------------------------------------------------------
    # Deploy Report Generator
    # ------------------------------------------------------------------

    def generate_deploy_report(
        self,
        deployment: str,
        namespace: str,
        old_image: str,
        new_image: str,
        risk_score: "RiskScore",
        health_gate: "HealthGateResult",
        watch_result: "WatchResult",
        duration_seconds: int,
    ) -> DeployReport:
        from pathlib import Path

        if watch_result.rollback_triggered:
            status = "ROLLED_BACK"
        elif watch_result.success:
            status = "SUCCESS"
        else:
            status = "FAILED"

        # Claude summary
        summary_prompt = (
            f"Post-deployment report for '{deployment}' in namespace '{namespace}'.\n"
            f"Status: {status}\n"
            f"Image: {old_image} → {new_image}\n"
            f"Risk level: {risk_score.label} (score {risk_score.total}/100)\n"
            f"Duration: {duration_seconds}s\n"
            f"Pods healthy: {watch_result.final_ready_count}\n"
            f"Errors detected: {watch_result.errors_detected}\n"
            f"Rollback triggered: {watch_result.rollback_triggered}"
            + (f"\nRollback reason: {watch_result.rollback_reason}" if watch_result.rollback_reason else "")
            + (f"\nHealth gate warnings: {'; '.join(health_gate.warnings)}" if health_gate.warnings else "")
        )
        try:
            resp = run_sync(llm.chat(
                messages=[context.user_message(summary_prompt)],
                system=(
                    "You are an SRE writing a post-deployment report. "
                    "Given these stats, write a concise summary (max 3 sentences): "
                    "did the deploy succeed cleanly? Any warnings or concerns? "
                    "Any recommended follow-up? Be specific with numbers."
                ),
                max_tokens=200,
            ))
            claude_summary = resp.content.strip()
        except Exception as exc:
            claude_summary = f"Report generation failed: {exc}"

        ts  = datetime.now(timezone.utc).isoformat()
        rid = str(uuid.uuid4())[:8]

        report = DeployReport(
            id                 = rid,
            deployment         = deployment,
            namespace          = namespace,
            status             = status,
            old_image          = old_image,
            new_image          = new_image,
            risk_level         = risk_score.label,
            duration_seconds   = duration_seconds,
            pods_healthy       = watch_result.final_ready_count,
            errors_detected    = watch_result.errors_detected,
            rollback_triggered = watch_result.rollback_triggered,
            claude_summary     = claude_summary,
            timestamp          = ts,
        )

        # Persist to SQLite
        try:
            from agent.integrations.deploy_db import save_report
            save_report(report)
        except Exception as exc:
            log.warning("deployment.report.save_failed", error=str(exc))

        # Write markdown file
        try:
            rdir = Path("data/reports")
            rdir.mkdir(parents=True, exist_ok=True)
            ts_file = ts[:19].replace(":", "-").replace("T", "_")
            md_path = rdir / f"deploy-{ts_file}-{deployment}.md"
            md_path.write_text(
                f"# Deploy Report: {deployment}\n\n"
                f"**Status:** {status}  \n"
                f"**Timestamp:** {ts}  \n"
                f"**Image:** `{old_image}` → `{new_image}`  \n"
                f"**Risk:** {risk_score.label} ({risk_score.total}/100)  \n"
                f"**Duration:** {duration_seconds}s  \n"
                f"**Pods healthy:** {watch_result.final_ready_count}  \n"
                f"**Errors detected:** {watch_result.errors_detected}  \n\n"
                f"## AI Summary\n\n{claude_summary}\n\n"
                f"## Risk Factors\n\n"
                + "\n".join(f"- {f}: +{p}" for f, p in risk_score.factors)
                + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("deployment.report.markdown_failed", error=str(exc))

        remember(
            content=f"Deploy report {status}: {deployment} [{namespace}] {old_image}→{new_image} {duration_seconds}s",
            source="deployment",
            metadata={"deployment": deployment, "namespace": namespace,
                      "status": status, "risk": risk_score.label},
        )

        return report

    # ------------------------------------------------------------------
    # Webhook Deployment Analyzer
    # ------------------------------------------------------------------

    def analyze_webhook_deploy(
        self,
        mapping: "WebhookMapping",
        event: "WebhookEvent",
        new_image: str,
    ) -> PendingDeploy:
        from agent.integrations.deploy_db import save_pending

        info    = get_deployment_info(mapping.deployment, mapping.namespace)
        old_img = info.current_image if info else ""

        risk    = self.score_deployment_risk(
            mapping.deployment, mapping.namespace, new_image, old_img
        )
        gate    = self.pre_deploy_health_check(
            mapping.deployment, mapping.namespace, new_image
        )
        # Claude analysis for the Slack message
        analysis_dict = self._call_claude(
            f"Webhook deploy: {mapping.deployment} in {mapping.namespace}\n"
            f"Commit: {event.commit_message[:200]}\n"
            f"Author: {event.author}\n"
            f"New image: {new_image}\n"
            f"Old image: {old_img}\n"
            f"Risk: {risk.label} ({risk.total}/100)\n"
            f"Health gate: {'BLOCKED' if gate.blocked else 'PASS'}"
        )

        now = datetime.now(timezone.utc).isoformat()
        pid = str(uuid.uuid4())[:8]

        pending = PendingDeploy(
            id             = pid,
            repo           = event.repo,
            branch         = event.branch,
            deployment     = mapping.deployment,
            namespace      = mapping.namespace,
            new_image      = new_image,
            old_image      = old_img,
            risk_score     = risk.total,
            risk_label     = risk.label,
            status         = "pending",
            created_at     = now,
            commit_sha     = event.commit_sha,
            author         = event.author,
            commit_message = event.commit_message[:200],
        )

        try:
            save_pending(pending)
        except Exception as exc:
            log.warning("deployment.pending.save_failed", error=str(exc))

        # Build and send Slack approval message with interactive buttons
        try:
            from agent.integrations.slack import send_deploy_approval_request, is_configured
            if is_configured():
                send_deploy_approval_request(pending)
        except Exception as exc:
            log.warning("deployment.webhook.slack_failed", error=str(exc))

        log.info(
            "deployment.webhook.pending_created",
            id=pid, deployment=mapping.deployment,
            risk=risk.label, blocked=gate.blocked,
        )
        return pending

    # ------------------------------------------------------------------
    # Shared approve / reject — called by both CLI and Slack button handler
    # ------------------------------------------------------------------

    def execute_approve(
        self,
        deploy_id: str,
        watch_seconds: int = 120,
        on_status: "callable | None" = None,
    ) -> "DeployReport | None":
        """
        Full approve flow: health gate → deploy → watch → report → Slack result.
        `on_status(msg)` is called with status strings for live feedback (CLI/Slack).
        Returns a DeployReport on completion, or None if rejected by health gate.
        """
        import time as _time
        from agent.integrations.deploy_db import get_pending, update_pending_status
        from agent.integrations.kubectl import get_deployment_info, set_image, wait_for_rollout

        def _status(msg: str) -> None:
            log.info("deployment.approve.status", msg=msg)
            if on_status:
                on_status(msg)

        p = get_pending(deploy_id)
        if p is None:
            _status(f"No pending deploy found: {deploy_id}")
            return None
        if p.status != "pending":
            _status(f"Deploy {deploy_id} is already '{p.status}'")
            return None

        # Error-budget gate: block deploys when the SLO budget is depleted.
        # A failure in this check must NEVER block a deploy.
        try:
            from agent.skills.slo import check_deploy_allowed as _slo_check
            allowed, reason = _slo_check(p.deployment, p.namespace)
            if not allowed:
                log.warning("deploy.slo_blocked", reason=reason)
                update_pending_status(
                    deploy_id, "failed",
                    deployed_at=datetime.now(timezone.utc).isoformat(),
                )
                _status(reason)
                return None
        except Exception:
            pass  # SLO check failure never blocks a deploy

        _status(f"Running pre-deploy health gate for {p.deployment}/{p.namespace}…")
        gate = self.pre_deploy_health_check(p.deployment, p.namespace, p.new_image)

        if gate.blocked:
            update_pending_status(deploy_id, "failed",
                                  deployed_at=datetime.now(timezone.utc).isoformat())
            _status(f"BLOCKED by health gate: {'; '.join(gate.blockers)}")
            return None

        now = datetime.now(timezone.utc).isoformat()
        update_pending_status(deploy_id, "approved", approved_at=now)

        _status(f"Deploying {p.new_image} → {p.deployment}/{p.namespace}…")
        info      = get_deployment_info(p.deployment, p.namespace)
        container = info.containers[0] if info and info.containers else p.deployment

        start = _time.time()
        r = set_image(p.deployment, p.namespace, container, p.new_image)
        if not r.success:
            update_pending_status(deploy_id, "failed",
                                  deployed_at=datetime.now(timezone.utc).isoformat())
            _status(f"Image set failed: {r.error[:120]}")
            return None

        wait_for_rollout(p.deployment, p.namespace, timeout=120)
        _status(f"Rollout applied — watching for {watch_seconds}s…")

        watch_result = self.watch_deployment_health(
            p.deployment, p.namespace, p.old_image, p.new_image,
            watch_seconds=watch_seconds,
        )

        total_dur = int(_time.time() - start)
        update_pending_status(
            deploy_id,
            "deployed" if watch_result.success else "failed",
            deployed_at=datetime.now(timezone.utc).isoformat(),
        )

        from agent.core.models import RiskScore as _RS
        risk_obj = _RS(
            total=p.risk_score, label=p.risk_label,
            factors=[], recommendation="",
        )
        _status("Generating deploy report…")
        report = self.generate_deploy_report(
            p.deployment, p.namespace, p.old_image, p.new_image,
            risk_obj, gate, watch_result, total_dur,
        )

        # Send final Slack notification
        try:
            from agent.integrations.slack import send_alert_generic, is_configured
            if is_configured():
                icon = "✅" if report.status == "SUCCESS" else ("↩️" if report.status == "ROLLED_BACK" else "❌")
                send_alert_generic(
                    title=f"DEPLOY {report.status} — {p.deployment}",
                    message=(
                        f"{icon} *{report.status}*  "
                        f"`{p.old_image.split(':')[-1]} → {p.new_image.split(':')[-1]}`\n"
                        f"Pods: {watch_result.final_ready_count} healthy  "
                        f"Duration: {total_dur}s\n\n"
                        f"_{report.claude_summary}_"
                    ),
                    severity="info" if report.status == "SUCCESS" else "critical",
                    fields={"Risk": p.risk_label.upper(), "Deploy ID": deploy_id},
                )
        except Exception:
            pass

        _status(f"Done — {report.status}")
        return report

    def execute_reject(
        self,
        deploy_id: str,
        reason: str = "",
        on_status: "callable | None" = None,
    ) -> bool:
        """
        Reject a pending deploy and notify Slack. Returns True if rejected.
        """
        from agent.integrations.deploy_db import get_pending, update_pending_status

        p = get_pending(deploy_id)
        if p is None or p.status != "pending":
            if on_status:
                on_status(f"Deploy {deploy_id} not found or already '{getattr(p, 'status', '?')}'")
            return False

        update_pending_status(deploy_id, "rejected",
                              deployed_at=datetime.now(timezone.utc).isoformat())

        if on_status:
            on_status(f"Deploy {deploy_id} rejected")

        try:
            from agent.integrations.slack import send_alert_generic, is_configured
            if is_configured():
                send_alert_generic(
                    title=f"DEPLOY REJECTED — {p.deployment}",
                    message=(
                        f"Deploy `{p.new_image.split(':')[-1]}` to "
                        f"`{p.deployment}/{p.namespace}` was rejected."
                        + (f"\n*Reason:* {reason}" if reason else "")
                    ),
                    severity="info",
                    fields={
                        "Repo":      f"{p.repo}:{p.branch}",
                        "Deploy ID": deploy_id,
                    },
                )
        except Exception:
            pass

        log.info("deployment.reject.done", id=deploy_id, reason=reason)
        return True
