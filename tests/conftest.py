"""Shared test fixtures. No test touches the network or the real database."""

import json
import uuid
from typing import Any

import pytest

from app.config import Settings
from app.security import compute_tally_signature

SECRET = "test-signing-secret"


def make_tally_payload(
    message: str = "My order hasn't arrived yet. Where is it?",
    order_id: str | None = "ORD-1042",
    event_id: str | None = None,
    event_type: str = "FORM_RESPONSE",
) -> dict[str, Any]:
    fields = [
        {"key": "question_name", "label": "Full name", "type": "INPUT_TEXT", "value": "Rahim Uddin"},
        {"key": "question_email", "label": "Email", "type": "INPUT_EMAIL", "value": "rahim@example.com"},
        {"key": "question_order", "label": "Order ID", "type": "INPUT_TEXT", "value": order_id},
        {"key": "question_message", "label": "Message", "type": "TEXTAREA", "value": message},
    ]
    return {
        "eventId": event_id or str(uuid.uuid4()),
        "eventType": event_type,
        "createdAt": "2026-09-26T12:00:00.000Z",
        "data": {
            "responseId": "resp_1",
            "formId": "form_1",
            "formName": "Contact support",
            "fields": fields,
        },
    }


def sign(body: bytes, secret: str = SECRET) -> dict[str, str]:
    return {"Tally-Signature": compute_tally_signature(body, secret), "Content-Type": "application/json"}


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        tally_signing_secret=SECRET,
        slack_webhook_url="https://hooks.slack.test/services/T000/B000/XXXX",
        ollama_url="http://ollama.test",
        ollama_model="test-model",
        _env_file=None,
    )


@pytest.fixture
def session_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app.db import init_db, make_session_factory

    # One shared in-memory database per test.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    init_db(engine)
    return make_session_factory(engine)
