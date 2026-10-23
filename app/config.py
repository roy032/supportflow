"""Application settings, loaded from environment variables and `.env`."""

import logging
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The value shipped in .env.example. Treated as "not set" so a copied example file
# never becomes a working secret.
PLACEHOLDER_SECRET = "change-me"  # noqa: S105 - a known non-secret, rejected below

MIN_ADMIN_KEY_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./supportflow.db"

    tally_signing_secret: str = ""
    # Largest webhook body accepted. Real Tally submissions are a few kilobytes.
    max_webhook_body_bytes: int = Field(default=256 * 1024, gt=0)

    # Bearer token for the monitoring endpoints (/events, /failures), which return
    # customer data. Empty means those endpoints reject every request.
    admin_api_key: str = ""

    llm_provider: str = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    llm_timeout_seconds: float = Field(default=120.0, gt=0)

    slack_webhook_url: str = ""

    log_level: str = "INFO"

    @field_validator("tally_signing_secret", "admin_api_key", "slack_webhook_url", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("tally_signing_secret", "admin_api_key")
    @classmethod
    def _placeholder_is_unset(cls, value: str) -> str:
        return "" if value == PLACEHOLDER_SECRET else value

    @field_validator("admin_api_key")
    @classmethod
    def _admin_key_is_long_enough(cls, value: str) -> str:
        if value and len(value) < MIN_ADMIN_KEY_LENGTH:
            raise ValueError(
                f"ADMIN_API_KEY must be at least {MIN_ADMIN_KEY_LENGTH} characters; "
                "generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _known_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if not isinstance(logging.getLevelName(level), int):
            raise ValueError(f"unknown log level {value!r}; use DEBUG, INFO, WARNING or ERROR")
        return level


@lru_cache
def get_settings() -> Settings:
    return Settings()
