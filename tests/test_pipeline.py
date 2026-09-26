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
