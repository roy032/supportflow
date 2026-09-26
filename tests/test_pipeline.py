"""Pipeline tests: stored event -> classification row, and failures recorded, not raised."""

import pytest

pytest.importorskip("sqlalchemy")

from app.models import Classification, Event, Failure  # noqa: E402
from app.pipeline import process_event  # noqa: E402
from tests.conftest import make_tally_payload  # noqa: E402
from tests.test_classifier import VALID, FakeProvider  # noqa: E402


def store_event(session_factory, **kwargs) -> int:
    payload = make_tally_payload(**kwargs)
    with session_factory() as s:
        event = Event(
            external_id=payload["eventId"],
            payload=payload,
            message=payload["data"]["fields"][-1]["value"],
        )
        s.add(event)
        s.commit()
        return event.id


def test_successful_classification_is_saved(session_factory):
    event_id = store_event(session_factory)
    process_event(event_id, session_factory, FakeProvider(VALID))
    with session_factory() as s:
        event = s.get(Event, event_id)
        assert event.status == "classified"
        assert event.classification.category == "order_status"
        assert event.classification.model == "fake:model"


def test_failed_classification_is_recorded_not_raised(session_factory):
    event_id = store_event(session_factory)
    process_event(event_id, session_factory, FakeProvider("bad", "bad"))
    with session_factory() as s:
        event = s.get(Event, event_id)
        assert event.status == "failed"
        failure = s.query(Failure).one()
        assert failure.stage == "classify"
        assert failure.attempts == 2


def test_already_classified_event_is_not_reprocessed(session_factory):
    event_id = store_event(session_factory)
    process_event(event_id, session_factory, FakeProvider(VALID))
    process_event(event_id, session_factory, FakeProvider())  # would raise if called
    with session_factory() as s:
        assert s.query(Classification).count() == 1


class FakeNotifier:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def send(self, message):
        if self.error:
            raise self.error
        self.sent.append(message)
        return 1


def test_classified_event_is_posted_to_slack(session_factory):
    event_id = store_event(session_factory)
    notifier = FakeNotifier()
    process_event(event_id, session_factory, FakeProvider(VALID), notifier)
    assert len(notifier.sent) == 1
    assert "Order status" in notifier.sent[0]["text"]
    with session_factory() as s:
        assert s.get(Event, event_id).status == "notified"


def test_slack_failure_is_recorded_and_retry_only_resends(session_factory):
    from app.slack import SlackError

    event_id = store_event(session_factory)
    process_event(
        event_id, session_factory, FakeProvider(VALID), FakeNotifier(SlackError("down", 3))
    )
    with session_factory() as s:
        assert s.get(Event, event_id).status == "notify_failed"
        assert s.query(Failure).one().stage == "notify"

    # Re-running sends to Slack again without calling the LLM a second time.
    notifier = FakeNotifier()
    process_event(event_id, session_factory, FakeProvider(), notifier)
    assert len(notifier.sent) == 1
    with session_factory() as s:
        assert s.get(Event, event_id).status == "notified"
        assert s.query(Classification).count() == 1


def test_full_flow_from_webhook_to_slack(settings, session_factory):
    """Signed webhook in, classification stored, Slack message out, all in one request."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import encode, sign

    notifier = FakeNotifier()
    app = create_app(
        settings,
        session_factory,
        processor=lambda eid: process_event(eid, session_factory, FakeProvider(VALID), notifier),
    )
    body = encode(make_tally_payload(event_id="evt-e2e"))
    r = TestClient(app).post("/webhooks/tally", content=body, headers=sign(body))
    assert r.status_code == 202
    assert len(notifier.sent) == 1

    events = TestClient(app).get("/events").json()
    assert events[0]["status"] == "notified"
    assert events[0]["classification"]["category"] == "order_status"
