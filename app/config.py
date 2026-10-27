"""Application settings, loaded from environment variables and `.env`."""

import logging
from datetime import timedelta
from functools import lru_cache
from typing import Literal

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

    llm_provider: Literal["ollama", "anthropic"] = "ollama"
    # Used when LLM_PROVIDER fails to answer (down, timed out, rate limited). Empty: none.
    llm_fallback_provider: Literal["", "ollama", "anthropic"] = ""
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    llm_timeout_seconds: float = Field(default=120.0, gt=0)

    # Claude. Without ANTHROPIC_API_KEY the SDK falls back to its own credential lookup.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"

    slack_webhook_url: str = ""
    # Signing secret of the Slack app, for the buttons on Slack messages
    # (POST /slack/interactions). Empty: messages are sent without buttons.
    slack_signing_secret: str = ""

    # Helpdesk.
    shop_name: str = "Our shop"
    # Where the dashboard is reachable, for links in Slack (e.g. https://support.example.com).
    public_url: str = ""
    # Policy documents the AI reply drafts are written from.
    knowledge_dir: str = "knowledge"
    draft_replies: bool = True
    # Spam is resolved as soon as it is classified, so it never sits in the inbox.
    auto_resolve_spam: bool = True
    # Minutes an open, unanswered ticket may wait before Slack is told, per urgency.
    # 0 turns escalation off for that urgency.
    sla_high_minutes: int = Field(default=60, ge=0)
    sla_medium_minutes: int = Field(default=240, ge=0)
    sla_low_minutes: int = Field(default=0, ge=0)

    # Outgoing email for replies to customers.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True

    # Dashboard sign-in. Sessions are signed with SESSION_SECRET (falls back to ADMIN_API_KEY).
    dashboard_password: str = ""
    session_secret: str = ""

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
    # "text" for people reading a terminal, "json" for a log pipeline.
    log_format: Literal["text", "json"] = "text"

    @field_validator(
        "tally_signing_secret", "admin_api_key", "slack_webhook_url", "anthropic_api_key",
        "slack_signing_secret", "smtp_password", "dashboard_password", "session_secret",
        "public_url", mode="before",
    )
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "tally_signing_secret", "admin_api_key", "slack_signing_secret", "dashboard_password",
        "session_secret",
    )
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

    @field_validator("public_url")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("dashboard_password")
    @classmethod
    def _dashboard_password_is_long_enough(cls, value: str) -> str:
        if value and len(value) < 12:
            raise ValueError("DASHBOARD_PASSWORD must be at least 12 characters")
        return value

    def sla(self) -> dict[str, timedelta]:
        minutes = {
            "high": self.sla_high_minutes,
            "medium": self.sla_medium_minutes,
            "low": self.sla_low_minutes,
        }
        return {u: timedelta(minutes=m) for u, m in minutes.items() if m > 0}

    @model_validator(mode="after")
    def _lease_outlasts_a_run(self) -> "Settings":
        # A run is two classification attempts (each possibly trying a fallback model,
        # whose SDK may retry) plus Slack retries. If the lease ran out first, a second
        # worker could take the event over and post it to Slack twice.
        calls_per_attempt = 2 if self.llm_fallback_provider else 1
        minimum = 2 * calls_per_attempt * self.llm_timeout_seconds + 120
        if self.job_lease_seconds < minimum:
            raise ValueError(
                f"JOB_LEASE_SECONDS must be at least {minimum:.0f} (2 x LLM_TIMEOUT_SECONDS"
                f"{' x 2 with a fallback provider' if self.llm_fallback_provider else ''}"
                " + 120) so a slow run isn't picked up twice"
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
