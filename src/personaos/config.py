"""Application configuration (pydantic-settings).

Reads from environment variables and .env. Single Settings instance,
imported across the runtime.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """PersonaOS v0.1 settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Database
    database_url: str = (
        "postgresql+asyncpg://personaos:personaos@localhost:5432/personaos"
    )
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Worker
    worker_poll_interval_seconds: float = 2.0
    task_lease_ttl_seconds: int = 300

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    # Runtime adapter (v0.1 ships OpenClaw only)
    runtime_adapter: str = "openclaw"


settings = Settings()
