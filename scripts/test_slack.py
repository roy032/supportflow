"""Send one test message to Slack using SLACK_WEBHOOK_URL from your .env.

Usage:
    python scripts/test_slack.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.slack import SlackError, SlackNotifier, build_message  # noqa: E402


def main() -> int:
    url = get_settings().slack_webhook_url
    if not url:
        print("Set SLACK_WEBHOOK_URL in your .env first.")
        return 1

    message = build_message(
        event_id=0,
        category="order_status",
        urgency="medium",
        sentiment="neutral",
        language="en",
        summary="Test message from SupportFlow. If you can read this, Slack is connected.",
        order_id="ORD-TEST",
        customer_name="SupportFlow",
        customer_email=None,
        message="This is a test notification sent by scripts/test_slack.py.",
    )
    try:
        attempts = SlackNotifier(url).send(message)
    except SlackError as exc:
        print(f"Slack failed: {exc}")
        return 1
    print(f"Sent to Slack ({attempts} attempt(s)). Check your channel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
