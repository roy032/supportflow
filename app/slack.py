"""Slack notifications through an incoming webhook, with retries for temporary failures."""

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger("supportflow.slack")

URGENCY_LABEL = {"high": "HIGH", "medium": "Medium", "low": "Low"}
CATEGORY_LABEL = {
    "refund": "Refund",
    "order_status": "Order status",
    "complaint": "Complaint",
    "spam": "Spam",
    "other": "Other",
}


# Upper bound on a single wait, so a large Retry-After can't block a worker for minutes.
MAX_WAIT_SECONDS = 30.0


class SlackError(RuntimeError):
    def __init__(self, message: str, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


def _retry_after_seconds(value: str | None, default: float) -> float:
    """Parse a Retry-After header given in seconds; fall back to `default` otherwise."""
    if value is None:
        return default
    try:
        seconds = float(value)
    except ValueError:
        return default  # HTTP-date form or garbage
    return seconds if seconds >= 0 else default


def build_message(
    *,
    event_id: int,
    category: str,
    urgency: str,
    sentiment: str,
    language: str,
    summary: str,
    order_id: str | None,
    customer_name: str | None,
    customer_email: str | None,
    message: str,
) -> dict[str, Any]:
    category_label = CATEGORY_LABEL.get(category, category)
    urgency_label = URGENCY_LABEL.get(urgency, urgency)
    preview = message if len(message) <= 500 else message[:497] + "..."
    customer = customer_name or "Unknown customer"
    if customer_email:
        customer += f" ({customer_email})"

    return {
        # Plain-text fallback for notifications and clients that can't render blocks.
        "text": f"[{urgency_label}] {category_label}: {summary}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{category_label} | urgency: {urgency_label}"},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{summary}*"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Customer*\n{customer}"},
                    {"type": "mrkdwn", "text": f"*Order ID*\n{order_id or 'none'}"},
                    {"type": "mrkdwn", "text": f"*Sentiment*\n{sentiment}"},
                    {"type": "mrkdwn", "text": f"*Language*\n{language}"},
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f">{preview}"}},
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"SupportFlow event #{event_id}"}],
            },
        ],
    }


class SlackNotifier:
    def __init__(
        self,
        webhook_url: str,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.webhook_url = webhook_url
        self._client = client or httpx.Client(timeout=10.0)
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep

    def send(self, message: dict[str, Any]) -> int:
        """Post `message`; return the number of attempts it took.

        Retries network errors, HTTP 429 (honouring Retry-After) and 5xx with exponential
        backoff; each wait is capped at MAX_WAIT_SECONDS. Other 4xx responses and malformed
        URLs mean the request itself is wrong (for example a revoked webhook URL), so they
        fail immediately.
        """
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._client.post(self.webhook_url, json=message)
            except (httpx.UnsupportedProtocol, httpx.InvalidURL) as exc:
                # A malformed URL is a configuration error; retrying cannot fix it.
                raise SlackError(
                    f"Slack rejected the message (invalid SLACK_WEBHOOK_URL: {exc})", attempt
                ) from exc
            except httpx.HTTPError as exc:
                last_error = f"network error: {exc}"
                wait = self.backoff_seconds * 2 ** (attempt - 1)
            else:
                if response.status_code == 200:
                    return attempt
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code == 429:
                    wait = _retry_after_seconds(
                        response.headers.get("Retry-After"), self.backoff_seconds
                    )
                elif response.status_code >= 500:
                    wait = self.backoff_seconds * 2 ** (attempt - 1)
                else:
                    raise SlackError(f"Slack rejected the message ({last_error})", attempt)

            if attempt < self.max_attempts:
                wait = min(wait, MAX_WAIT_SECONDS)
                logger.warning("Slack attempt %s failed (%s); retrying in %.1fs", attempt, last_error, wait)
                self._sleep(wait)

        raise SlackError(
            f"Slack failed after {self.max_attempts} attempts ({last_error})", self.max_attempts
        )
