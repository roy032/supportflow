"""Slack notifier tests with a mocked HTTP transport (no real network)."""

import json

import httpx
import pytest

from app.slack import SlackError, SlackNotifier, build_message

URL = "https://hooks.slack.test/services/T000/B000/XXXX"


def sample_message():
    return build_message(
        event_id=7,
        category="refund",
        urgency="high",
        sentiment="negative",
        language="bn",
        summary="Customer wants a refund for a damaged item.",
        order_id="ORD-9",
        customer_name="Rahim Uddin",
        customer_email="rahim@example.com",
        message="জিনিসটা ভাঙা এসেছে, টাকা ফেরত চাই",
    )


def notifier_with(responses, sleeps):
    """Build a notifier whose HTTP calls return `responses` in order."""
    queue = list(responses)
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SlackNotifier(URL, client=client, backoff_seconds=1.0, sleep=sleeps.append), sent


def test_message_contains_key_details():
    msg = sample_message()
    blob = json.dumps(msg, ensure_ascii=False)
    assert msg["text"] == "[HIGH] Refund: Customer wants a refund for a damaged item."
    assert "ORD-9" in blob and "rahim@example.com" in blob and "event #7" in blob
    assert "টাকা ফেরত চাই" in blob


def test_long_messages_are_truncated():
    msg = build_message(
        event_id=1, category="other", urgency="low", sentiment="neutral", language="en",
        summary="s", order_id=None, customer_name=None, customer_email=None, message="x" * 2000,
    )
    preview = msg["blocks"][3]["text"]["text"]
    assert len(preview) <= 502 and preview.endswith("...")


def test_success_on_first_try():
    sleeps = []
    notifier, sent = notifier_with([httpx.Response(200, text="ok")], sleeps)
    assert notifier.send(sample_message()) == 1
    assert len(sent) == 1 and sleeps == []


def test_server_errors_are_retried_with_backoff():
    sleeps = []
    notifier, _ = notifier_with(
        [httpx.Response(500), httpx.Response(503), httpx.Response(200, text="ok")], sleeps
    )
    assert notifier.send(sample_message()) == 3
    assert sleeps == [1.0, 2.0]


def test_rate_limit_honours_retry_after():
    sleeps = []
    notifier, _ = notifier_with(
        [httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(200, text="ok")], sleeps
    )
    assert notifier.send(sample_message()) == 2
    assert sleeps == [5.0]


def test_network_errors_are_retried():
    sleeps = []
    request = httpx.Request("POST", URL)
    notifier, _ = notifier_with(
        [httpx.ConnectError("down", request=request), httpx.Response(200, text="ok")], sleeps
    )
    assert notifier.send(sample_message()) == 2


def test_client_errors_fail_immediately():
    sleeps = []
    notifier, sent = notifier_with([httpx.Response(404, text="no_service")], sleeps)
    with pytest.raises(SlackError, match="rejected") as info:
        notifier.send(sample_message())
    assert info.value.attempts == 1 and len(sent) == 1 and sleeps == []


def test_gives_up_after_max_attempts():
    sleeps = []
    notifier, sent = notifier_with([httpx.Response(500)] * 3, sleeps)
    with pytest.raises(SlackError, match="after 3 attempts"):
        notifier.send(sample_message())
    assert len(sent) == 3 and sleeps == [1.0, 2.0]
