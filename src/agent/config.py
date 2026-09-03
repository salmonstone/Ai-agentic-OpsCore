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
    )
