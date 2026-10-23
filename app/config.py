"""Application settings, loaded from environment variables and `.env`."""

import logging
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
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

    # Job queue (see app/jobs.py). An event is run at most JOB_MAX_ATTEMPTS times
    # automatically, waiting JOB_RETRY_BASE_SECONDS * 2^n (capped) between runs.
    job_max_attempts: int = Field(default=5, ge=1)
    job_retry_base_seconds: float = Field(default=30.0, gt=0)
    job_retry_max_seconds: float = Field(default=3600.0, gt=0)
    # How long a worker may hold an event before another worker may take it over.
    job_lease_seconds: float = Field(default=600.0, gt=0)
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    # Run the worker as a thread inside the web server. Convenient locally; in production
    # run `python -m app.worker` separately and set this to false.
    run_worker_in_process: bool = True

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

    @model_validator(mode="after")
    def _lease_outlasts_a_run(self) -> "Settings":
        # A run is two LLM calls plus Slack retries. If the lease ran out first, a second
        # worker could take the event over and post it to Slack twice.
        minimum = 2 * self.llm_timeout_seconds + 120
        if self.job_lease_seconds < minimum:
            raise ValueError(
                f"JOB_LEASE_SECONDS must be at least {minimum:.0f} "
                "(2 x LLM_TIMEOUT_SECONDS + 120) so a slow run isn't picked up twice"
            )
        return self

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
