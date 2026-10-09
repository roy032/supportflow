"""Application settings, loaded from environment variables and `.env`."""

import logging
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The value shipped in .env.example. Treated as "not set" so a copied example file
# never becomes a working secret.
PLACEHOLDER_SECRET = "change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./supportflow.db"

    tally_signing_secret: str = ""

    llm_provider: str = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    llm_timeout_seconds: float = Field(default=120.0, gt=0)

    slack_webhook_url: str = ""

    log_level: str = "INFO"

    @field_validator("tally_signing_secret", "slack_webhook_url", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("tally_signing_secret")
    @classmethod
    def _placeholder_is_unset(cls, value: str) -> str:
        return "" if value == PLACEHOLDER_SECRET else value

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
