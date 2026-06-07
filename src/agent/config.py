from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    anthropic_api_key: str
    voyage_api_key: str = ""          # optional — enables real semantic embeddings
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_expensive_model: str = "claude-opus-4-8"
    llm_temperature: float = 0.1
    db_path: str = "data/memory.db"
    vault_path: str = "data/vault"
    log_format: str = "pretty"  # "pretty" | "json"


settings = Settings()
