"""Centralized configuration management.

All settings are environment-variable driven with sensible defaults
and production-safety guards (e.g. refusing to start without an API
key when require_api_key is true).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="INSIGHTFORGE_",
    )

    # ── LLM ────────────────────────────────────────────────────────────────
    model: str = Field(default="gpt-4o")
    api_key: str = Field(default="")
    api_base: str = Field(default="")
    fallback_models: str = Field(
        default="",
        description="Comma-separated fallback model names for retry on error.",
    )

    # ── Generation ─────────────────────────────────────────────────────────
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=8192, ge=1)
    max_rounds: int = Field(default=25, ge=1, le=100)

    # ── Execution ──────────────────────────────────────────────────────────
    executor_backend: str = Field(default="docker")
    code_timeout: int = Field(default=300, ge=1)
    docker_image: str = Field(default="insightforge/sandbox:latest")
    docker_memory_limit: str = Field(default="2g")
    docker_cpu_limit: float = Field(default=1.0)
    docker_network: str = Field(default="none")

    # ── Storage ────────────────────────────────────────────────────────────
    workspace: str = Field(default="./workspace")
    session_backend: str = Field(default="sqlite")
    session_db_url: str = Field(default="sqlite+aiosqlite:///./sessions.db")

    # ── Server ─────────────────────────────────────────────────────────────
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8787, ge=1, le=65535)
    api_key: Optional[str] = Field(default=None)
    require_api_key: bool = Field(default=False)
    cors_origins: str = Field(
        default="http://localhost:8787,http://127.0.0.1:8787",
        description="Comma-separated allowed CORS origins. Use '*' to allow all (not recommended).",
    )

    # ── Rate limiting ───────────────────────────────────────────────────────
    rate_limit_per_minute: int = Field(default=60, ge=1)
    max_tokens_per_run: int = Field(default=200000, ge=1000)

    # ── Auth ────────────────────────────────────────────────────────────────
    jwt_secret: str = Field(default="")
    require_auth: bool = Field(default=False)
    min_password_length: int = Field(default=8, ge=6)

    # ── Security ────────────────────────────────────────────────────────────
    max_task_length: int = Field(default=4000, ge=100, description="Max chars for analysis task input")

    # ── Context management ──────────────────────────────────────────────────
    context_window_tokens: int = Field(
        default=128000,
        ge=1000,
        description="Maximum context window size in tokens for the model.",
    )
    context_keep_recent_rounds: int = Field(
        default=6,
        ge=1,
        description="Number of recent rounds to keep intact when pruning context.",
    )

    # ── Observability ──────────────────────────────────────────────────────
    observability_enabled: bool = Field(default=False)
    observability_provider: str = Field(default="langfuse")
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")
    langfuse_host: str = Field(default="https://cloud.langfuse.com")

    @field_validator("fallback_models", mode="before")
    @classmethod
    def _ensure_str(cls, v):
        if v is None:
            return ""
        return v

    @model_validator(mode="after")
    def _validate_auth_config(self) -> "Settings":
        if self.require_auth and not self.jwt_secret:
            raise ValueError(
                "INSIGHTFORGE_REQUIRE_AUTH=true requires INSIGHTFORGE_JWT_SECRET to be set. "
                "Generate one with: python -c 'import secrets; print(secrets.token_hex(32))'"
            )
        return self

    @property
    def fallback_model_list(self) -> List[str]:
        if not self.fallback_models:
            return []
        return [m.strip() for m in self.fallback_models.split(",") if m.strip()]

    @property
    def cors_origin_list(self) -> List[str]:
        if self.cors_origins == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()