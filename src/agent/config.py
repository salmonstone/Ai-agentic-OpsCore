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
    )
