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


def escape_mrkdwn(text: str) -> str:
    """Escape the three characters Slack treats as control characters in mrkdwn.

    Customer text (and the LLM's summary of it) must not be able to write `<!channel>`,
    `<@U123>` or `<https://evil.example|your order>` into the support channel.
    See https://api.slack.com/reference/surfaces/formatting#escaping
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quote(text: str) -> str:
    """Render `text` as a mrkdwn block quote; `>` only quotes the line it starts."""
    return "\n".join(f">{line}" for line in escape_mrkdwn(text).splitlines() or [""])


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
    draft: str | None = None,
    needs_human: bool = False,
    interactive: bool = False,
    ticket_url: str | None = None,
) -> dict[str, Any]:
    category_label = CATEGORY_LABEL.get(category, category)
    urgency_label = URGENCY_LABEL.get(urgency, urgency)
    preview = message if len(message) <= 500 else message[:497] + "..."
    customer = escape_mrkdwn(customer_name or "Unknown customer")
    if customer_email:
        customer += f" ({escape_mrkdwn(customer_email)})"
    summary_text = escape_mrkdwn(summary)
    order_text = escape_mrkdwn(order_id) if order_id else "none"

    return {
        # Plain-text fallback for notifications and clients that can't render blocks.
        "text": f"[{urgency_label}] {category_label}: {summary_text}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{category_label} | urgency: {urgency_label}"},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{summary_text}*"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Customer*\n{customer}"},
                    {"type": "mrkdwn", "text": f"*Order ID*\n{order_text}"},
                    {"type": "mrkdwn", "text": f"*Sentiment*\n{sentiment}"},
                    {"type": "mrkdwn", "text": f"*Language*\n{language}"},
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": _quote(preview)}},
            *_draft_blocks(draft, needs_human),
            *_action_blocks(event_id, draft, customer_email, interactive, ticket_url),
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"SupportFlow event #{event_id}"}],
            },
        ],
    }


def _draft_blocks(draft: str | None, needs_human: bool) -> list[dict[str, Any]]:
    if not draft:
        return []
    label = "*Suggested reply* (check it first)" if needs_human else "*Suggested reply*"
    text = draft if len(draft) <= 2500 else draft[:2497] + "..."
    return [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{label}\n{escape_mrkdwn(text)}"}},
    ]


def _action_blocks(
    event_id: int,
    draft: str | None,
    customer_email: str | None,
    interactive: bool,
    ticket_url: str | None,
) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    if interactive and draft and customer_email:
        elements.append({
            "type": "button",
            "action_id": "send_draft",
            "value": str(event_id),
            "style": "primary",
            "text": {"type": "plain_text", "text": "Send suggested reply"},
            "confirm": {
                "title": {"type": "plain_text", "text": "Send this reply?"},
                "text": {"type": "plain_text", "text": f"It will be emailed to {customer_email}."},
                "confirm": {"type": "plain_text", "text": "Send"},
                "deny": {"type": "plain_text", "text": "Cancel"},
            },
        })
    if interactive:
        elements.append({
            "type": "button",
            "action_id": "resolve",
            "value": str(event_id),
            "text": {"type": "plain_text", "text": "Resolve"},
        })
    if ticket_url:
        elements.append({
            "type": "button",
            "action_id": "open_ticket",
            "url": ticket_url,
            "text": {"type": "plain_text", "text": "Open ticket"},
        })
    return [{"type": "actions", "elements": elements}] if elements else []


def build_escalation(
    *, event_id: int, urgency: str, summary: str, waited_minutes: int, ticket_url: str | None
) -> dict[str, Any]:
    text = (
        f":rotating_light: *{URGENCY_LABEL.get(urgency, urgency)} ticket #{event_id} has had no "
        f"reply for {waited_minutes} minutes.*\n{escape_mrkdwn(summary)}"
    )
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if ticket_url:
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "action_id": "open_ticket", "url": ticket_url,
            "text": {"type": "plain_text", "text": "Open ticket"},
        }]})
    return {"text": f"Ticket #{event_id} has had no reply for {waited_minutes} minutes", "blocks": blocks}


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
