from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Optional so the app starts before .env is created (wizard sets it)
    anthropic_api_key: str = ""
    voyage_api_key: str = ""

    llm_model: str = "claude-haiku-4-5-20251001"
    llm_expensive_model: str = "claude-opus-4-8"
    llm_temperature: float = 0.1

    db_path: str = "data/memory.db"
    vault_path: str = "data/vault"

    k8s_default_namespace: str = "all"
    log_format: str = "pretty"  # "pretty" | "json"

    # Resource monitor thresholds (percentages)
    resource_cpu_warning:       int = 70
    resource_cpu_critical:      int = 85
    resource_mem_warning:       int = 75
    resource_mem_critical:      int = 90
    resource_watch_interval:    int = 30
    resource_snapshot_interval: int = 300

    # Slack alerts (set SLACK_WEBHOOK_URL to enable)
    slack_webhook_url:        str  = ""
    slack_default_channel:    str  = "#alerts"
    slack_critical_channel:   str  = "#incidents"
    slack_enabled:            bool = False
    slack_alert_on_warning:   bool = False   # True = also send warning-level alerts
    slack_alert_on_recovery:  bool = True    # True = notify when critical clears
    alert_cooldown_minutes:   int  = 30
    alert_send_resolved:      bool = True

    # AWS authentication — pick ONE method via aws_auth_method
    #   "iam_role"    (default, recommended) — EC2/EKS instance role or local
    #                 IMDS; boto3/aws CLI resolve credentials automatically,
    #                 nothing else needs to be set.
    #   "access_key"  — static long-lived credentials (local dev fallback).
    #   "sso_profile" — a named ~/.aws profile (SSO, role-assumption, or
    #                   static keys — whatever that profile is backed by).
    aws_auth_method:       str = "iam_role"
    aws_region:            str = "us-east-1"
    eks_cluster_name:      str = ""
    aws_access_key_id:     str = ""
    aws_secret_access_key: str = ""
    aws_profile:           str = ""

    # Cost analysis
    ec2_region:           str = "us-east-1"
    hours_per_month:      int = 730
    cost_waste_threshold: int = 50    # flag if waste exceeds this %
    aws_cost_days:        int = 30

    # GitHub webhook + deployment automation
    github_webhook_secret:       str  = ""
    webhook_port:                int  = 8080
    webhook_host:                str  = "0.0.0.0"
    webhook_mappings_file:       str  = "data/webhook_mappings.yaml"
    deploy_watch_seconds:        int  = 120
    deploy_cooldown_minutes:     int  = 10
    deploy_pending_expiry_hours: int  = 1
    auto_approve_low_risk:       bool = False
    slack_signing_secret:        str  = ""

    # PagerDuty / OpsGenie on-call paging
    pagerduty_routing_key:  str = ""   # Events API v2 routing key
    opsgenie_api_key:       str = ""   # OpsGenie Alerts API key
    opsgenie_region:        str = "us" # "us" or "eu"

    # Jenkins CI/CD monitoring + self-healing
    jenkins_url:            str  = ""   # e.g. https://jenkins.company.com
    jenkins_user:           str  = ""
    jenkins_api_token:      str  = ""   # API token, never the account password
    jenkins_verify_ssl:     bool = True
    jenkins_timeout:        int  = 30
    jenkins_auto_heal:      bool = False   # true = auto-fix safe, known patterns
    jenkins_scan_interval:  int  = 5       # minutes between scans in `agent jenkins watch`

    def get_aws_session(self):
        """
        Build a boto3.Session for the configured aws_auth_method.

        - access_key: only used when both key fields are actually set —
          falls through to the iam_role behavior otherwise, so a half
          -configured access_key method doesn't hard-fail every AWS call.
        - sso_profile: only used when aws_profile is actually set, same
          fallback reasoning.
        - iam_role (default): Session() with no explicit credentials — boto3
          resolves them itself (env vars, ~/.aws/credentials default profile,
          or EC2/EKS instance metadata), which is exactly what "use whatever
          role this is running as" means.
        """
        import boto3

        if self.aws_auth_method == "access_key" and self.aws_access_key_id and self.aws_secret_access_key:
            return boto3.Session(
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.aws_secret_access_key,
                region_name=self.aws_region,
            )
        if self.aws_auth_method == "sso_profile" and self.aws_profile:
            return boto3.Session(profile_name=self.aws_profile, region_name=self.aws_region)
        return boto3.Session(region_name=self.aws_region)


# Lazy singleton — never raises on import, even without .env
try:
    settings = Settings()
except Exception:
    settings = Settings.model_construct(
        anthropic_api_key="",
        voyage_api_key="",
        llm_model="claude-haiku-4-5-20251001",
        llm_expensive_model="claude-opus-4-8",
        llm_temperature=0.1,
        db_path="data/memory.db",
        vault_path="data/vault",
        k8s_default_namespace="all",
        log_format="pretty",
        resource_cpu_warning=70,
        resource_cpu_critical=85,
        resource_mem_warning=75,
        resource_mem_critical=90,
        resource_watch_interval=30,
        resource_snapshot_interval=300,
        slack_webhook_url="",
        slack_default_channel="#alerts",
        slack_critical_channel="#incidents",
        slack_enabled=False,
        slack_alert_on_warning=False,
        slack_alert_on_recovery=True,
        alert_cooldown_minutes=30,
        alert_send_resolved=True,
        aws_auth_method="iam_role",
        aws_region="us-east-1",
        eks_cluster_name="",
        aws_access_key_id="",
        aws_secret_access_key="",
        aws_profile="",
        ec2_region="us-east-1",
        hours_per_month=730,
        cost_waste_threshold=50,
        aws_cost_days=30,
        github_webhook_secret="",
        webhook_port=8080,
        webhook_host="0.0.0.0",
        webhook_mappings_file="data/webhook_mappings.yaml",
        deploy_watch_seconds=120,
        deploy_cooldown_minutes=10,
        deploy_pending_expiry_hours=1,
        auto_approve_low_risk=False,
        slack_signing_secret="",
        pagerduty_routing_key="",
        opsgenie_api_key="",
        opsgenie_region="us",
        jenkins_url="",
        jenkins_user="",
        jenkins_api_token="",
        jenkins_verify_ssl=True,
        jenkins_timeout=30,
        jenkins_auto_heal=False,
        jenkins_scan_interval=5,
    )
