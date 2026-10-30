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
    database_url: str = "postgresql+asyncpg://personaos:personaos@localhost:5432/personaos"
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # None = internal/trusted mode; when set, requires Bearer token
    operator_token: str | None = None

    # Worker
    worker_poll_interval_seconds: float = 2.0
    task_lease_ttl_seconds: int = 300
    policies_dir: str | None = None

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    # Runtime adapter (v0.1 ships OpenClaw, simulation, litellm)
    runtime_adapter: str = "openclaw"

    # LiteLLM / LLM Adapter Settings
    litellm_api_base: str = "http://localhost:4000/v1"
    litellm_api_key: str = "sk-master-dev-key"
    litellm_model: str = "qwen2.5-coder-14b"

    # Node Supervision
    enable_node_supervision: bool = True
    node_supervision_interval_seconds: float = 10.0
    node_heartbeat_timeout_seconds: float = 60.0

    # Memory Subsystem (Phase 2)
    memory_adapter: str = "in_memory"
    mem0_api_key: str | None = None
    mem0_base_url: str = "https://api.mem0.ai/v1"
    memory_min_confidence: float = 0.0


settings = Settings()
