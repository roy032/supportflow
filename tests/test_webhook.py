"""Webhook endpoint tests: authenticity, validation, idempotency."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.models import Event, Failure  # noqa: E402
from tests.conftest import encode, make_tally_payload, sign  # noqa: E402


@pytest.fixture
def processed():
    return []


@pytest.fixture
def client(settings, session_factory, processed):
    app = create_app(settings, session_factory, processor=processed.append)
    return TestClient(app)


def post(client, payload, headers=None):
    body = encode(payload)
    return client.post("/webhooks/tally", content=body, headers=headers or sign(body))


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok"}


def test_valid_webhook_is_stored_and_queued(client, session_factory, processed):
    r = post(client, make_tally_payload(event_id="evt-1"))
    assert r.status_code == 202
    event_id = r.json()["event_id"]
    assert processed == [event_id]
    with session_factory() as s:
        event = s.get(Event, event_id)
        assert event.external_id == "evt-1"
        assert event.customer_email == "rahim@example.com"
        assert event.status == "received"


def test_bad_signature_is_rejected_and_nothing_is_stored(client, session_factory, processed):
    body = encode(make_tally_payload())
    r = client.post("/webhooks/tally", content=body, headers=sign(body, secret="wrong"))
    assert r.status_code == 401
    assert processed == []
    with session_factory() as s:
        assert s.query(Event).count() == 0


def test_missing_signature_is_rejected(client):
    body = encode(make_tally_payload())
    r = client.post("/webhooks/tally", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 401


def test_duplicate_delivery_is_ignored(client, session_factory, processed):
    payload = make_tally_payload(event_id="evt-dup")
    first = post(client, payload)
    second = post(client, payload)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["event_id"] == first.json()["event_id"]
    assert len(processed) == 1
    with session_factory() as s:
        assert s.query(Event).count() == 1


def test_invalid_json_is_rejected(client):
    body = b"not json"
    r = client.post("/webhooks/tally", content=body, headers=sign(body))
    assert r.status_code == 400


def test_payload_without_message_is_rejected_and_logged(client, session_factory):
    payload = make_tally_payload()
    payload["data"]["fields"] = []
    r = post(client, payload)
    assert r.status_code == 422
    with session_factory() as s:
        failure = s.query(Failure).one()
        assert failure.stage == "parse"


def test_events_endpoint_lists_recent_events(client):
    post(client, make_tally_payload(event_id="evt-a"))
    post(client, make_tally_payload(event_id="evt-b"))
    r = client.get("/events?limit=10")
    assert r.status_code == 200
    assert [e["external_id"] for e in r.json()] == ["evt-b", "evt-a"]
