"""
Cost Analysis Skill — two-layer cluster cost visibility.

Layer 1 (kubectl only):
  Estimates pod/deployment/namespace costs from resource requests vs actual
  usage. Uses EC2 on-demand pricing. Works with just kubectl — no AWS creds.

Layer 2 (AWS Cost Explorer, needs boto3 + ce:GetCostAndUsage):
  Real AWS bill breakdown by service. Compares EC2 spend vs K8s utilisation
  to surface idle overhead.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from agent.core import context, llm
from agent.core.async_utils import run_sync
from agent.core.models import (
    AWSCostAnalysis, AWSCostData, CostFix, CostReport, DeploymentCost,
    IdleResources, NamespaceCost, PodCost,
)
from agent.integrations.ec2_pricing import (
    HOURS_PER_MONTH, estimate_hourly_from_cpu, get_instance_price, get_monthly_cost,
)
from agent.integrations.kubectl import (
    get_current_context,
    get_nodes_and_pods,
    get_pod_actual_usage,
)
from agent.memory.retrieval import remember
from agent.observability.logging import get_logger
from agent.skills.base import BaseSkill

log = get_logger(__name__)

_COST_SYSTEM = """\
You are a FinOps and Kubernetes cost optimisation expert.
Analyse this cluster cost report and give actionable savings recommendations.

For each over-provisioned deployment provide:
1. Current vs recommended resource requests (with exact numbers)
2. Estimated monthly savings in USD
3. Exact kubectl command to right-size

Also comment on:
- Overall cluster efficiency (are nodes over/under-provisioned?)
- Whether any nodes could be removed after right-sizing
- Total potential monthly savings across all recommendations

Be specific with numbers. Format savings as "$X/month".
Write 8-12 sentences total — dense and actionable."""


_AWS_FINOPS_SYSTEM = """\
You are a senior FinOps engineer specializing in AWS cost optimization.
You have been given a complete AWS cost analysis. For each waste category found:
1. Calculate the exact monthly savings
2. Explain WHY this is costing money
3. Give the EXACT fix command or steps
4. Rate effort: Easy/Medium/Hard
5. Prioritize by ROI (savings vs effort)

Focus on quick wins first. Be specific with dollar amounts.
Format as a prioritized numbered list. Keep it concise and actionable."""


# ---------------------------------------------------------------------------
# ROI scoring — rank fixes by savings weighted by how easy/safe they are.
# ---------------------------------------------------------------------------
_EFFORT_MULT = {"easy": 3.0, "medium": 1.5, "hard": 0.5}
_RISK_MULT   = {"safe": 1.0, "low": 0.9, "medium": 0.6, "high": 0.3}


def _roi_score(fix: CostFix) -> float:
    return (
        fix.monthly_savings
        * _EFFORT_MULT.get(fix.effort, 1.0)
        * _RISK_MULT.get(fix.risk, 1.0)
    )


def _has_prod_tag(resource: dict) -> bool:
    """True if a resource carries an Environment=prod/production tag."""
    for tag in resource.get("Tags", []) or []:
        if tag.get("Key") == "Environment" and str(tag.get("Value", "")).lower() in (
            "prod", "production"
        ):
            return True
    return False


class CostAnalysisSkill(BaseSkill):
    @property
    def name(self) -> str:
        return "cost-analysis"

    @property
    def description(self) -> str:
        return (
            "Estimate K8s cluster costs from resource requests, "
            "detect waste, and surface right-sizing opportunities."
        )

    def execute(self, input_data: dict) -> dict:
        report = self.analyze_cluster_cost(input_data.get("namespace", "all"))
        return report.model_dump()

    # ------------------------------------------------------------------
    # Layer 1 — K8s cost estimation
    # ------------------------------------------------------------------

    def analyze_cluster_cost(
        self,
        namespace: str = "all",
        include_system: bool = False,
        with_ai: bool = True,
        with_usage: bool = False,
    ) -> CostReport:
        """
        Fetch nodes + pod requests (fast). Optionally fetch kubectl top (slow).

        with_usage: run kubectl top pods for actual usage / waste % (adds ~10-20s)
        include_system: include kube-system / AWS DaemonSet pods
        with_ai: run Claude analysis (adds ~5s)
        """
        # Single kubectl call fetches nodes + pods together — one auth round-trip
        nodes_list, pod_reqs = get_nodes_and_pods(namespace)

        # kubectl top is opt-in — it polls metrics-server and can take 15-30s
        if with_usage:
            pod_usage = get_pod_actual_usage(namespace)
        else:
            pod_usage = []

        # Filter system namespaces unless requested
        _SYSTEM_NS = frozenset({
            "kube-system", "kube-public", "kube-node-lease",
            "ingress-nginx", "cert-manager", "amazon-cloudwatch",
            "aws-load-balancer-controller", "external-dns", "cluster-autoscaler",
        })
        if not include_system:
            pod_reqs = [p for p in pod_reqs if p["namespace"] not in _SYSTEM_NS]

        # Build lookup dicts
        nodes: dict[str, dict] = {n["name"]: n for n in nodes_list}
        usage_map: dict[tuple[str, str], dict] = {
            (u["namespace"], u["pod"]): u for u in pod_usage
        }

        # Total node cost
        total_node_cost = 0.0
        for node in nodes_list:
            pricing = get_instance_price(node["instance_type"])
            if pricing:
                hourly = pricing["hourly"]
            else:
                hourly = estimate_hourly_from_cpu(max(int(node["cpu"]), 1))
            total_node_cost += get_monthly_cost(hourly, node.get("region", "us-east-1"))

        # Per-pod cost + waste
        pod_costs: list[PodCost] = []
        for p in pod_reqs:
            usage   = usage_map.get((p["namespace"], p["pod"]))
            node    = nodes.get(p["node_name"], {})
            inst    = node.get("instance_type", "unknown")
            monthly = self.calculate_pod_cost(p, node)
            waste_pct, waste_lbl = self.detect_waste(p, usage)
            pod_costs.append(PodCost(
                pod                = p["pod"],
                namespace          = p["namespace"],
                deployment         = p["deployment"],
                cpu_request        = p["cpu_request"],
                memory_request     = p["memory_request"],
                cpu_actual         = usage["cpu_actual"]    if usage else 0.0,
                memory_actual      = usage["memory_actual"] if usage else 0.0,
                node_instance_type = inst,
                est_monthly_cost   = monthly,
                waste_percent      = waste_pct,
                waste_label        = waste_lbl,
            ))

        total_requested = sum(p.est_monthly_cost for p in pod_costs)
        total_waste     = sum(
            p.est_monthly_cost * p.waste_percent / 100
            for p in pod_costs if p.waste_label == "over-provisioned"
        )
        overall_waste_pct = (
            int(total_waste / total_requested * 100) if total_requested > 0 else 0
        )

        # Aggregate by deployment
        dep_buckets: dict[tuple[str, str], list[PodCost]] = defaultdict(list)
        for p in pod_costs:
            dep_buckets[(p.namespace, p.deployment)].append(p)

        deployments: list[DeploymentCost] = []
        for (ns, dep), pods in dep_buckets.items():
            total_cpu_req = sum(p.cpu_request  for p in pods)
            total_cpu_act = sum(p.cpu_actual   for p in pods)
            dep_cost      = sum(p.est_monthly_cost for p in pods)
            dep_waste     = (
                int((total_cpu_req - total_cpu_act) / total_cpu_req * 100)
                if total_cpu_req > 0 else 0
            )
            dep_waste = max(0, dep_waste)
            _, dep_lbl = self.detect_waste(
                {"cpu_request": total_cpu_req},
                {"cpu_actual": total_cpu_act} if total_cpu_act else None,
            )
            deployments.append(DeploymentCost(
                deployment        = dep,
                namespace         = ns,
                pod_count         = len(pods),
                total_cpu_request = round(total_cpu_req, 3),
                total_cpu_actual  = round(total_cpu_act, 3),
                est_monthly_cost  = round(dep_cost, 2),
                waste_percent     = dep_waste,
                waste_label       = dep_lbl,
            ))
        deployments.sort(key=lambda d: d.est_monthly_cost, reverse=True)

        # Aggregate by namespace
        ns_buckets: dict[str, list[PodCost]] = defaultdict(list)
        for p in pod_costs:
            ns_buckets[p.namespace].append(p)

        namespaces: list[NamespaceCost] = sorted(
            [
                NamespaceCost(
                    namespace        = ns,
                    pod_count        = len(pods),
                    est_monthly_cost = round(sum(p.est_monthly_cost for p in pods), 2),
                )
                for ns, pods in ns_buckets.items()
            ],
            key=lambda n: n.est_monthly_cost,
            reverse=True,
        )

        # Top wasteful pods (over-provisioned, sorted by waste%)
        wasteful = sorted(
            [p for p in pod_costs if p.waste_label == "over-provisioned"],
            key=lambda p: p.waste_percent,
            reverse=True,
        )[:10]

        cluster  = get_current_context() or "unknown"
        analysis = (
            self.analyze_with_claude(deployments, total_requested, total_waste)
            if with_ai else ""
        )
        now = datetime.now(timezone.utc).isoformat()

        report = CostReport(
            cluster_name             = cluster,
            total_nodes              = len(nodes_list),
            total_monthly_node_cost  = round(total_node_cost, 2),
            total_requested_cost     = round(total_requested, 2),
            total_waste_cost         = round(total_waste, 2),
            waste_percent            = overall_waste_pct,
            namespaces               = namespaces,
            deployments              = deployments,
            top_wasteful_pods        = wasteful,
            claude_analysis          = analysis,
            generated_at             = now,
        )

        remember(
            content=(
                f"Cost analysis on {cluster}: "
                f"node_cost=${total_node_cost:.0f}/mo "
                f"requested=${total_requested:.0f}/mo "
                f"waste=${total_waste:.0f}/mo ({overall_waste_pct}%). "
                f"{analysis[:250]}"
            ),
            source="cost-analysis",
            metadata={
                "cluster":       cluster,
                "node_cost":     round(total_node_cost, 2),
                "waste_cost":    round(total_waste, 2),
                "waste_percent": overall_waste_pct,
            },
        )
        log.info("cost.done", cluster=cluster, nodes=len(nodes_list),
                 node_cost=total_node_cost, waste=total_waste)
        return report

    def calculate_pod_cost(self, pod: dict, node: dict) -> float:
        """
        pod_cost = max(cpu_fraction, mem_fraction) * node_hourly * 730

        Uses the binding constraint (CPU or memory, whichever is larger)
        so a memory-heavy pod isn't undercharged.
        """
        pricing  = get_instance_price(node.get("instance_type", ""))
        if pricing:
            cpu_cap  = pricing["cpu"]       or 1
            mem_cap  = pricing["memory_gb"] or 1
            hourly   = pricing["hourly"]
        else:
            cpu_cap  = node.get("cpu", 1)    or 1
            mem_cap  = node.get("memory_gb", 4) or 4
            hourly   = estimate_hourly_from_cpu(int(cpu_cap))

        region   = node.get("region", "us-east-1")
        mult     = 1.0
        try:
            from agent.integrations.ec2_pricing import REGION_MULTIPLIER
            mult = REGION_MULTIPLIER.get(region, 1.0)
        except ImportError:
            pass

        cpu_frac = pod.get("cpu_request", 0)    / cpu_cap
        mem_frac = pod.get("memory_request", 0) / mem_cap
        fraction = max(cpu_frac, mem_frac, 0.0)
        return round(fraction * hourly * mult * HOURS_PER_MONTH, 2)

    def detect_waste(
        self,
        pod_req: dict,
        usage: dict | None,
    ) -> tuple[int, str]:
        """
        Compare request vs actual CPU.
        Returns (waste_percent, label) where label is one of:
          'OK' | 'over-provisioned' | 'under-provisioned' | 'unknown'
        """
        cpu_req = pod_req.get("cpu_request", 0)
        if not usage or cpu_req == 0:
            return 0, "unknown"
        cpu_act   = usage.get("cpu_actual", 0)
        waste_pct = max(0, int((cpu_req - cpu_act) / cpu_req * 100))
        if cpu_act > cpu_req * 0.90:
            return waste_pct, "under-provisioned"
        if waste_pct >= 50:
            return waste_pct, "over-provisioned"
        return waste_pct, "OK"

    def analyze_with_claude(
        self,
        deployments: list[DeploymentCost],
        total_cost: float,
        total_waste: float,
    ) -> str:
        top = [d for d in deployments if d.waste_label == "over-provisioned"][:8]
        if not top and deployments:
            top = deployments[:5]
        if not top:
            return "No pods with resource requests found — deploy some workloads first."

        lines = [
            f"=== CLUSTER COST REPORT ===",
            f"Total estimated: ${total_cost:.2f}/month",
            f"Estimated waste: ${total_waste:.2f}/month",
            "",
            "=== OVER-PROVISIONED DEPLOYMENTS ===",
        ]
        for d in top:
            lines.append(
                f"{d.namespace}/{d.deployment}  "
                f"req={d.total_cpu_request:.2f}CPU act={d.total_cpu_actual:.2f}CPU  "
                f"cost=${d.est_monthly_cost:.2f}/mo  waste={d.waste_percent}%"
            )

        try:
            resp = run_sync(llm.chat(
                messages=[context.user_message("\n".join(lines))],
                system=_COST_SYSTEM,
                max_tokens=700,
            ))
            return resp.content.strip()
        except Exception as exc:
            log.warning("cost.claude_failed", error=str(exc))
            return f"AI analysis unavailable: {exc}"

    # ------------------------------------------------------------------
    # Layer 2 — AWS Cost Explorer
    # ------------------------------------------------------------------

    def get_aws_costs(self, days: int = 30) -> AWSCostData:
        """
        Call AWS Cost Explorer API for real billing data.
        Requires: boto3 installed + ce:GetCostAndUsage IAM permission.
        Raises RuntimeError if boto3 missing, PermissionError if IAM denied.
        """
        try:
            import boto3
            from botocore.exceptions import ClientError, NoCredentialsError
        except ImportError:
            raise RuntimeError(
                "boto3 not installed.\n"
                "Run:  uv add boto3\n"
                "Then ensure your IAM role has: ce:GetCostAndUsage"
            )

        from datetime import timedelta
        end   = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        try:
            client   = boto3.client("ce")
            response = client.get_cost_and_usage(
                TimePeriod  = {"Start": start, "End": end},
                Granularity = "MONTHLY",
                Metrics     = ["UnblendedCost"],
                GroupBy     = [{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
        except Exception as exc:
            msg = str(exc)
            if "AccessDenied" in msg or "AuthFailure" in msg:
                raise PermissionError(
                    "IAM permission denied.\n"
                    "Add this to your role:\n"
                    '  {"Effect":"Allow","Action":"ce:GetCostAndUsage","Resource":"*"}'
                )
            raise RuntimeError(f"AWS Cost Explorer error: {msg}")

        by_service: dict[str, float] = {}
        total = 0.0
        for result_time in response.get("ResultsByTime", []):
            for group in result_time.get("Groups", []):
                svc    = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                by_service[svc] = round(by_service.get(svc, 0.0) + amount, 2)
                total          += amount

        # Friendly service name normalization
        friendly: dict[str, float] = {}
        for svc, amt in by_service.items():
            if amt < 0.01:
                continue
            if "Elastic Compute" in svc or "EC2" in svc:
                key = "EC2"
            elif "Elastic Block" in svc or "EBS" in svc:
                key = "EBS"
            elif "Data Transfer" in svc:
                key = "Data Transfer"
            elif "NAT" in svc:
                key = "NAT Gateway"
            elif "Elastic Load" in svc or "ELB" in svc:
                key = "Load Balancer"
            elif "Elastic Kubernetes" in svc or "EKS" in svc:
                key = "EKS"
            elif "Simple Storage" in svc or "S3" in svc:
                key = "S3"
            else:
                key = svc[:40]
            friendly[key] = round(friendly.get(key, 0.0) + amt, 2)

        ec2_spend  = friendly.get("EC2", 0.0)
        daily_avg  = round(total / days, 2)
        return AWSCostData(
            period          = f"Last {days} days",
            total_spend     = round(total, 2),
            by_service      = dict(sorted(friendly.items(), key=lambda x: x[1], reverse=True)),
            daily_average   = daily_avg,
            today_estimate  = daily_avg,
            ec2_spend       = ec2_spend,
            k8s_utilization_percent = 0,
            idle_overhead   = 0.0,
        )

    def compare_k8s_vs_aws(
        self, k8s: CostReport, aws: AWSCostData,
    ) -> dict:
        """Overlay estimated K8s node cost against real EC2 bill."""
        ec2     = aws.ec2_spend
        k8s_req = k8s.total_requested_cost
        idle    = max(0.0, ec2 - k8s_req)
        util_pct = int(k8s_req / ec2 * 100) if ec2 > 0 else 0
        return {
            "ec2_bill":           round(ec2, 2),
            "k8s_requested":      round(k8s_req, 2),
            "idle_overhead":      round(idle, 2),
            "utilization_percent": util_pct,
            "insight": (
                f"K8s workloads use {util_pct}% of EC2 spend. "
                f"${idle:.0f}/mo is idle overhead (node headroom, system pods, DaemonSets)."
            ),
        }

    # ------------------------------------------------------------------
    # Layer 3 — Full AWS cost optimization
    # ------------------------------------------------------------------

    def full_aws_analysis(self, days: int = 30) -> AWSCostAnalysis:
        """
        Run every AWS waste/optimisation check concurrently, build a prioritised
        savings plan of CostFix items, and attach a Claude FinOps summary.

        Degrades gracefully — any check that fails (missing perms, no boto3)
        returns empty data and is simply omitted from the plan.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from agent.integrations import aws_cost

        checks = {
            "cost":        lambda: aws_cost.get_cost_and_usage(days),
            "idle":        aws_cost.get_idle_resources,
            "ebs":         aws_cost.get_ebs_optimization,
            "logs":        aws_cost.get_cloudwatch_logs_cost,
            "ecr":         aws_cost.get_ecr_waste,
            "rightsizing": aws_cost.get_rightsizing_recommendations,
            "reserved":    aws_cost.get_reserved_vs_ondemand,
            "anomalies":   lambda: aws_cost.get_spend_anomalies(days),
            "savings_plans": aws_cost.get_savings_plan_recommendations,
            "cost_by_tag": aws_cost.get_cost_by_tag,
        }

        results: dict[str, object] = {}
        with ThreadPoolExecutor(max_workers=len(checks)) as pool:
            futures = {pool.submit(fn): key for key, fn in checks.items()}
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    results[key] = fut.result()
                except Exception as exc:
                    log.warning("aws.check_failed", check=key, error=str(exc))
                    results[key] = None

        cost = results.get("cost") or {}
        idle = results.get("idle") or {}
        ebs = results.get("ebs") or []
        logs = results.get("logs") or {}
        ecr = results.get("ecr") or {}
        rightsizing = results.get("rightsizing") or []
        reserved = results.get("reserved") or {}
        anomalies = results.get("anomalies") or {}
        savings_plans = results.get("savings_plans") or {}
        cost_by_tag = results.get("cost_by_tag") or {}

        idle_model = IdleResources(
            unattached_volumes  = idle.get("unattached_volumes", []),
            unused_eips         = idle.get("unused_eips", []),
            old_snapshots       = idle.get("old_snapshots", []),
            idle_load_balancers = idle.get("idle_load_balancers", []),
            stopped_instances   = idle.get("stopped_instances", []),
            total_monthly_waste = idle.get("total_monthly_waste", 0.0),
        )

        savings_plan = self._build_savings_plan(
            idle, ebs, logs, ecr, rightsizing, reserved, savings_plans,
        )

        total_waste = round(
            idle_model.total_monthly_waste
            + sum(o.get("monthly_savings", 0.0) for o in ebs)
            + logs.get("potential_savings", 0.0)
            + ecr.get("estimated_cost", 0.0),
            2,
        )

        analysis = AWSCostAnalysis(
            period_days          = days,
            total_spend          = cost.get("total", 0.0),
            last_month_spend     = cost.get("last_month", 0.0),
            month_change_percent = cost.get("month_change_pct", 0.0),
            forecast_this_month  = cost.get("forecast", 0.0),
            by_service           = cost.get("by_service", {}),
            idle_resources       = idle_model,
            ebs_opportunities    = ebs,
            cloudwatch_logs      = logs,
            ecr_waste            = ecr,
            rightsizing          = rightsizing,
            reserved_vs_ondemand = reserved,
            total_monthly_waste  = total_waste,
            savings_plan         = savings_plan,
            anomalies            = anomalies,
            savings_plans        = savings_plans,
            cost_by_tag          = cost_by_tag,
            generated_at         = datetime.now(timezone.utc).isoformat(),
        )

        analysis.claude_analysis = self.analyze_with_claude(analysis)

        total_savings = sum(f.monthly_savings for f in savings_plan)
        remember(
            content=(
                f"AWS cost analysis: spend=${analysis.total_spend:.0f} "
                f"(last {days}d), waste=${total_waste:.0f}/mo, "
                f"{len(savings_plan)} optimisation(s), "
                f"potential savings=${total_savings:.0f}/mo. "
                f"{analysis.claude_analysis[:200]}"
            ),
            source="cost-aws",
            metadata={
                "total_spend":  analysis.total_spend,
                "waste":        total_waste,
                "fixes":        len(savings_plan),
                "savings":      round(total_savings, 2),
            },
        )
        log.info(
            "cost.aws_done",
            spend=analysis.total_spend,
            waste=total_waste,
            fixes=len(savings_plan),
        )
        return analysis

    def _build_savings_plan(
        self,
        idle: dict,
        ebs: list[dict],
        logs: dict,
        ecr: dict,
        rightsizing: list[dict],
        reserved: dict,
        savings_plans: dict | None = None,
    ) -> list[CostFix]:
        """Turn raw findings into a sorted, prioritised list of CostFix items."""
        fixes: list[CostFix] = []
        n = 0

        # Unattached EBS volumes — auto-fixable, safe (prod tagging downgrades risk)
        for vol in idle.get("unattached_volumes", []):
            n += 1
            vid = vol.get("VolumeId", "")
            effort, risk, auto = "easy", "safe", True
            if _has_prod_tag(vol):
                risk, auto = "medium", False
            fixes.append(CostFix(
                id=f"ebs-idle-{n}",
                category="ebs_idle",
                title=f"Delete unattached volume {vid}",
                description=(
                    f"{vol.get('Size', 0)}GB {vol.get('VolumeType', 'gp2')} volume "
                    f"is unattached and billed monthly."
                ),
                monthly_savings=vol.get("cost_per_month", 0.0),
                effort=effort, risk=risk, fix_type="aws_cli",
                fix_command=f"aws ec2 delete-volume --volume-id {vid}",
                auto_fixable=auto, aws_resource_id=vid,
            ))

        # Unused Elastic IPs — auto-fixable, safe (prod tagging downgrades risk)
        for eip in idle.get("unused_eips", []):
            n += 1
            alloc = eip.get("AllocationId", "")
            eip_risk, eip_auto = "safe", True
            if _has_prod_tag(eip):
                eip_risk, eip_auto = "medium", False
            fixes.append(CostFix(
                id=f"eip-{n}",
                category="eip",
                title=f"Release unused Elastic IP {eip.get('PublicIp', '')}",
                description="Unassociated Elastic IPs are billed hourly.",
                monthly_savings=eip.get("cost_per_month", 3.60),
                effort="easy", risk=eip_risk, fix_type="aws_cli",
                fix_command=f"aws ec2 release-address --allocation-id {alloc}",
                auto_fixable=eip_auto, aws_resource_id=alloc,
            ))

        # Old snapshots — backup/DR snapshots are higher-risk, harder to fix
        for snap in idle.get("old_snapshots", []):
            cost = snap.get("cost_per_month", 0.0)
            if cost < 0.01:
                continue
            n += 1
            sid = snap.get("SnapshotId", "")
            tags_blob = " ".join(
                f"{t.get('Key', '')}={t.get('Value', '')}"
                for t in (snap.get("Tags", []) or [])
            ).lower()
            desc_blob = str(snap.get("Description", "")).lower()
            is_backup = any(k in tags_blob or k in desc_blob for k in ("backup", "dr"))
            snap_effort = "hard" if is_backup else "easy"
            snap_risk = "medium" if is_backup else "safe"
            fixes.append(CostFix(
                id=f"snapshot-{n}",
                category="snapshot",
                title=f"Delete old snapshot {sid}",
                description=(
                    f"Snapshot is {snap.get('age_days', 0)} days old "
                    f"({snap.get('Size', 0)}GB) and billed monthly."
                ),
                monthly_savings=cost,
                effort=snap_effort, risk=snap_risk, fix_type="aws_cli",
                fix_command=f"aws ec2 delete-snapshot --snapshot-id {sid}",
                auto_fixable=False, aws_resource_id=sid,
            ))

        # Stopped instances — recently stopped may be an intentional restart
        for si in idle.get("stopped_instances", []):
            cost = si.get("cost_per_month", 0.0)
            if cost < 0.01:
                continue
            n += 1
            iid = si.get("InstanceId", "")
            stopped_days = si.get("stopped_days")
            si_risk = "safe"
            if stopped_days is not None and stopped_days < 3:
                si_risk = "medium"
            elif _has_prod_tag(si):
                si_risk = "medium"
            fixes.append(CostFix(
                id=f"stopped-{n}",
                category="stopped_instance",
                title=f"Terminate stopped instance {iid}",
                description=(
                    f"Stopped {si.get('InstanceType', 'instance')} still bills for "
                    f"attached EBS volumes."
                ),
                monthly_savings=cost,
                effort="medium", risk=si_risk, fix_type="console",
                fix_command=f"aws ec2 terminate-instances --instance-ids {iid}",
                auto_fixable=False, aws_resource_id=iid,
            ))

        # gp2 -> gp3 upgrades — auto-fixable, safe
        for opp in ebs:
            n += 1
            vid = opp.get("VolumeId", "")
            fixes.append(CostFix(
                id=f"ebs-upgrade-{n}",
                category="ebs_upgrade",
                title=f"Upgrade {vid} gp2 -> gp3",
                description=(
                    f"{opp.get('Size', 0)}GB gp2 volume can move to gp3 "
                    f"(20% cheaper, same/better performance)."
                ),
                monthly_savings=opp.get("monthly_savings", 0.0),
                effort="easy", risk="safe", fix_type="aws_cli",
                fix_command=f"aws ec2 modify-volume --volume-id {vid} --volume-type gp3",
                auto_fixable=True, aws_resource_id=vid,
            ))

        # CloudWatch log retention — auto-fixable, safe (one fix per group)
        for grp in logs.get("groups_no_retention", []):
            cost = grp.get("cost_per_month", 0.0)
            if cost < 0.01:
                continue
            n += 1
            name = grp.get("logGroupName", "")
            fixes.append(CostFix(
                id=f"cw-logs-{n}",
                category="cloudwatch",
                title=f"Set 30-day retention on {name}",
                description=(
                    f"Log group has no retention policy ({grp.get('stored_gb', 0)}GB "
                    f"stored, growing indefinitely)."
                ),
                monthly_savings=round(cost * 0.7, 2),
                effort="easy", risk="safe", fix_type="aws_cli",
                fix_command=(
                    f"aws logs put-retention-policy --log-group-name {name} "
                    f"--retention-in-days 30"
                ),
                auto_fixable=True, aws_resource_id=name,
            ))

        # Old ECR images — auto-fixable, low risk (one fix per image)
        for img in ecr.get("old_untagged_images", []):
            n += 1
            repo = img.get("repository", "")
            digest = img.get("digest", "")
            per_img_cost = round(img.get("size_gb", 0.0) * 0.10, 2)
            fixes.append(CostFix(
                id=f"ecr-{n}",
                category="ecr",
                title=f"Delete old untagged image in {repo}",
                description=(
                    f"Untagged image ({img.get('size_gb', 0)}GB, "
                    f"{img.get('age_days', 0)} days old) consuming storage."
                ),
                monthly_savings=per_img_cost,
                effort="easy", risk="low", fix_type="aws_cli",
                fix_command=(
                    f"aws ecr batch-delete-image --repository-name {repo} "
                    f"--image-ids imageDigest={digest}"
                ),
                auto_fixable=True, aws_resource_id=f"{repo}/{digest}",
            ))

        # Rightsizing — manual, medium risk
        for rec in rightsizing:
            savings = rec.get("estimated_monthly_savings", 0.0)
            if savings <= 0:
                continue
            n += 1
            iid = rec.get("instance_id", "")
            fixes.append(CostFix(
                id=f"rightsize-{n}",
                category="rightsizing",
                title=f"Rightsize {iid}: {rec.get('current_type', '?')} -> {rec.get('recommended_type', '?')}",
                description=(
                    "Cost Explorer flagged this instance as over-provisioned "
                    "based on utilisation."
                ),
                monthly_savings=savings,
                effort="medium", risk="medium", fix_type="console",
                fix_command=None,
                auto_fixable=False, aws_resource_id=iid,
            ))

        # Idle load balancers — manual, low risk
        for lb in idle.get("idle_load_balancers", []):
            n += 1
            fixes.append(CostFix(
                id=f"idle-lb-{n}",
                category="load_balancer",
                title=f"Remove idle load balancer {lb.get('LoadBalancerName', '')}",
                description="Load balancer has no healthy targets but is still billed.",
                monthly_savings=lb.get("cost_per_month", 18.0),
                effort="medium", risk="low", fix_type="console",
                fix_command=(
                    f"aws elbv2 delete-load-balancer --load-balancer-arn "
                    f"{lb.get('LoadBalancerArn', '')}"
                ),
                auto_fixable=False, aws_resource_id=lb.get("LoadBalancerArn", ""),
            ))

        # Reserved instances / savings plans — manual, low risk
        ri_savings = reserved.get("potential_reserved_savings", 0.0)
        if ri_savings > 0:
            n += 1
            fixes.append(CostFix(
                id=f"reserved-{n}",
                category="reserved",
                title="Purchase Savings Plan / Reserved Instances",
                description=(
                    f"On-demand EC2 spend is "
                    f"${reserved.get('ondemand_monthly', 0.0):.0f}/mo with "
                    f"{reserved.get('coverage_pct', 0.0)}% savings-plan coverage. "
                    f"Committing to a 1-year plan typically saves ~35%."
                ),
                monthly_savings=ri_savings,
                effort="medium", risk="low", fix_type="console",
                fix_command=None,
                auto_fixable=False,
            ))

        # Compute Savings Plan recommendation — usually the highest-ROI item
        sp = savings_plans or {}
        sp_savings = sp.get("max_monthly_savings", 0.0)
        if sp_savings > 0:
            n += 1
            best = None
            for r in sp.get("recommendations", []):
                if best is None or r.get("estimated_monthly_savings", 0) > best.get(
                    "estimated_monthly_savings", 0
                ):
                    best = r
            hourly = best.get("hourly_commitment", 0.0) if best else 0.0
            term = best.get("term", "1-year") if best else "1-year"
            fixes.append(CostFix(
                id=f"savings-plan-{n}",
                category="savings_plan",
                title=f"Purchase {term} Compute Savings Plan",
                description=(
                    sp.get("recommendation")
                    or (
                        f"Committing to a {term} Compute Savings Plan covers "
                        f"on-demand EC2 spend at a discount."
                    )
                ),
                monthly_savings=sp_savings,
                effort="medium", risk="low", fix_type="console",
                fix_command=(
                    f"aws ce create-savings-plan-purchase "
                    f"--commitment {hourly} --term {term}"
                ),
                auto_fixable=False,
            ))

        # Rank by ROI (savings weighted by effort + risk), not raw savings.
        for fix in fixes:
            fix.roi_score = round(_roi_score(fix), 2)
        fixes.sort(key=_roi_score, reverse=True)
        for i, fix in enumerate(fixes, start=1):
            fix.priority = i
        return fixes

    def analyze_with_claude(self, analysis: AWSCostAnalysis) -> str:
        """Run a FinOps-focused Claude summary over the gathered analysis."""
        idle = analysis.idle_resources
        lines = [
            "=== AWS COST ANALYSIS ===",
            f"Total spend (last {analysis.period_days}d): ${analysis.total_spend:,.2f}",
            f"Last month: ${analysis.last_month_spend:,.2f} "
            f"({analysis.month_change_percent:+.1f}% MTD vs last month)",
            f"Forecast this month: ${analysis.forecast_this_month:,.2f}",
            f"Total identified waste: ${analysis.total_monthly_waste:,.2f}/month",
            "",
            "=== WASTE BREAKDOWN ===",
            f"Unattached EBS volumes: {len(idle.unattached_volumes)} "
            f"(${sum(v.get('cost_per_month', 0) for v in idle.unattached_volumes):,.2f}/mo)",
            f"Unused Elastic IPs: {len(idle.unused_eips)} "
            f"(${sum(e.get('cost_per_month', 0) for e in idle.unused_eips):,.2f}/mo)",
            f"Old snapshots (>90d): {len(idle.old_snapshots)} "
            f"(${sum(s.get('cost_per_month', 0) for s in idle.old_snapshots):,.2f}/mo)",
            f"Idle load balancers: {len(idle.idle_load_balancers)} "
            f"(${sum(l.get('cost_per_month', 0) for l in idle.idle_load_balancers):,.2f}/mo)",
            f"Stopped instances (EBS): {len(idle.stopped_instances)}",
            f"gp2->gp3 upgrades: {len(analysis.ebs_opportunities)} "
            f"(${sum(o.get('monthly_savings', 0) for o in analysis.ebs_opportunities):,.2f}/mo)",
            f"CloudWatch logs w/o retention: "
            f"{len(analysis.cloudwatch_logs.get('groups_no_retention', []))} "
            f"(potential ${analysis.cloudwatch_logs.get('potential_savings', 0):,.2f}/mo)",
            f"Old ECR images: {len(analysis.ecr_waste.get('old_untagged_images', []))} "
            f"(${analysis.ecr_waste.get('estimated_cost', 0):,.2f}/mo)",
            f"Rightsizing recommendations: {len(analysis.rightsizing)}",
        ]
        rvo = analysis.reserved_vs_ondemand
        if rvo.get("ondemand_monthly"):
            lines.append(
                f"On-demand EC2: ${rvo.get('ondemand_monthly', 0):,.2f}/mo, "
                f"coverage {rvo.get('coverage_pct', 0)}%, "
                f"potential RI savings ${rvo.get('potential_reserved_savings', 0):,.2f}/mo"
            )

        lines.append("")
        lines.append("=== TOP FIXES (by savings) ===")
        for fix in analysis.savings_plan[:10]:
            lines.append(
                f"{fix.priority}. [{fix.category}] {fix.title} — "
                f"${fix.monthly_savings:,.2f}/mo "
                f"(effort={fix.effort}, risk={fix.risk}, "
                f"auto={'yes' if fix.auto_fixable else 'no'})"
            )

        if analysis.total_monthly_waste < 1 and not analysis.savings_plan:
            return (
                "No significant AWS waste detected — account looks well-optimised. "
                "Keep an eye on Savings Plan coverage and log retention as usage grows."
            )

        try:
            resp = run_sync(llm.chat(
                messages=[context.user_message("\n".join(lines))],
                system=_AWS_FINOPS_SYSTEM,
                max_tokens=900,
            ))
            return resp.content.strip()
        except Exception as exc:
            log.warning("cost.aws_claude_failed", error=str(exc))
            return f"AI analysis unavailable: {exc}"

    def apply_cost_fix(self, fix: CostFix) -> bool:
        """
        Execute an auto-fixable CostFix via boto3. Returns True on success.

        Only acts when fix.auto_fixable is True. Logs the action to memory.
        """
        if not fix.auto_fixable:
            log.warning("cost.fix_not_auto", fix_id=fix.id, category=fix.category)
            return False

        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError:
            log.warning("cost.fix_no_boto3", fix_id=fix.id)
            return False

        rid = fix.aws_resource_id or ""

        try:
            if fix.category == "cloudwatch":
                logs = boto3.client("logs")
                logs.put_retention_policy(logGroupName=rid, retentionInDays=30)

            elif fix.category == "eip":
                ec2 = boto3.client("ec2")
                ec2.release_address(AllocationId=rid)

            elif fix.category == "ebs_idle":
                ec2 = boto3.client("ec2")
                # Re-verify the volume is still unattached before deleting.
                resp = ec2.describe_volumes(VolumeIds=[rid])
                vols = resp.get("Volumes", [])
                if not vols or vols[0].get("State") != "available":
                    log.warning("cost.fix_skip_attached", fix_id=fix.id, volume=rid)
                    return False
                ec2.delete_volume(VolumeId=rid)

            elif fix.category == "ebs_upgrade":
                ec2 = boto3.client("ec2")
                ec2.modify_volume(VolumeId=rid, VolumeType="gp3")

            elif fix.category == "ecr":
                ecr = boto3.client("ecr")
                # aws_resource_id is "repo/sha256:digest"
                repo, _, digest = rid.partition("/")
                if not repo or not digest:
                    log.warning("cost.fix_bad_ecr_id", fix_id=fix.id, rid=rid)
                    return False
                result = ecr.batch_delete_image(
                    repositoryName=repo,
                    imageIds=[{"imageDigest": digest}],
                )
                if result.get("failures"):
                    log.warning("cost.fix_ecr_failure", fix_id=fix.id,
                                failures=result["failures"])
                    return False

            else:
                log.warning("cost.fix_unknown_category", fix_id=fix.id,
                            category=fix.category)
                return False

        except ClientError as exc:
            log.warning("cost.fix_failed", fix_id=fix.id, error=str(exc))
            return False
        except Exception as exc:
            log.warning("cost.fix_failed", fix_id=fix.id, error=str(exc))
            return False

        remember(
            content=(
                f"Applied AWS cost fix: {fix.title} "
                f"(category={fix.category}, resource={rid}, "
                f"saved ${fix.monthly_savings:.2f}/mo)"
            ),
            source="cost-aws-fix",
            metadata={
                "fix_id":   fix.id,
                "category": fix.category,
                "resource": rid,
                "savings":  fix.monthly_savings,
            },
        )
        log.info("cost.fix_applied", fix_id=fix.id, category=fix.category,
                 resource=rid, savings=fix.monthly_savings)
        return True
