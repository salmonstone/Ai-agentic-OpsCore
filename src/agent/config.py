from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # unknown .env vars (set by agent setup) don't crash the app
    )

    anthropic_api_key: str
    voyage_api_key: str = ""
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_expensive_model: str = "claude-opus-4-8"
    llm_temperature: float = 0.1
    db_path: str = "data/memory.db"
    vault_path: str = "data/vault"
    k8s_default_namespace: str = "all"
    log_format: str = "pretty"  # "pretty" | "json"


settings = Settings()
