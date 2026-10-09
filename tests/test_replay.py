"""Replay tests: unfinished events are found and finished; finished ones are left alone."""

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("sqlalchemy")

from app.models import Classification, Event  # noqa: E402
from app.pipeline import process_event  # noqa: E402
from app.replay import find_unfinished, replay  # noqa: E402
from app.slack import SlackError  # noqa: E402
from tests.conftest import make_tally_payload  # noqa: E402
from tests.test_classifier import VALID, FakeProvider  # noqa: E402
from tests.test_pipeline import FakeNotifier  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def add_event(session_factory, status: str, minutes_ago: float) -> int:
    payload = make_tally_payload()
    stamp = NOW - timedelta(minutes=minutes_ago)
    with session_factory() as s:
        event = Event(
            external_id=payload["eventId"],
            payload=payload,
            message=payload["data"]["fields"][-1]["value"],
            status=status,
            created_at=stamp,
            updated_at=stamp,
        )
        s.add(event)
        s.commit()
        return event.id


def test_finds_unfinished_events_older_than_min_age(session_factory):
    old_received = add_event(session_factory, "received", 30)
    old_failed = add_event(session_factory, "failed", 30)
    old_notify_failed = add_event(session_factory, "notify_failed", 30)
    add_event(session_factory, "notified", 30)  # finished
    add_event(session_factory, "received", 1)  # may still be in progress

    with session_factory() as s:
        found = find_unfinished(s, min_age=timedelta(minutes=5), now=NOW)
    assert found == [old_received, old_failed, old_notify_failed]


def test_status_filter_and_notified_is_never_selected(session_factory):
    add_event(session_factory, "received", 30)
    notify_failed = add_event(session_factory, "notify_failed", 30)
    add_event(session_factory, "notified", 30)

    with session_factory() as s:
        assert find_unfinished(s, statuses=["notify_failed"], now=NOW) == [notify_failed]
        assert find_unfinished(s, statuses=["notified"], now=NOW) == []


def test_replay_finishes_a_stuck_event(session_factory):
    event_id = add_event(session_factory, "received", 30)
    notifier = FakeNotifier()

    results = replay(
        [event_id],
        lambda eid: process_event(eid, session_factory, FakeProvider(VALID), notifier),
        session_factory,
    )
    assert results == {event_id: ("received", "notified")}
    assert len(notifier.sent) == 1


def test_replay_after_slack_outage_does_not_reclassify(session_factory):
    event_id = add_event(session_factory, "received", 30)
    process_event(
        event_id, session_factory, FakeProvider(VALID), FakeNotifier(SlackError("down", 3))
    )

    notifier = FakeNotifier()
    results = replay(
        [event_id],
        # An empty FakeProvider raises if the LLM is called again.
        lambda eid: process_event(eid, session_factory, FakeProvider(), notifier),
        session_factory,
    )
    assert results == {event_id: ("notify_failed", "notified")}
    with session_factory() as s:
        assert s.query(Classification).count() == 1


def test_replay_reports_missing_events_without_processing(session_factory):
    calls = []
    assert replay([999], calls.append, session_factory) == {999: (None, None)}
    assert calls == []
