"""Job queue tests: leases, retries with backoff, dead-lettering and requeueing."""

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("sqlalchemy")

from app.jobs import JobRunner, RetryPolicy, Worker  # noqa: E402
from app.models import Classification, Event, EventStatus  # noqa: E402
from app.pipeline import process_event  # noqa: E402
from app.slack import SlackError  # noqa: E402
from tests.conftest import make_tally_payload  # noqa: E402
from tests.test_classifier import VALID, FakeProvider  # noqa: E402
from tests.test_pipeline import FakeNotifier  # noqa: E402

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
POLICY = RetryPolicy(max_attempts=3, base_seconds=10, max_seconds=60, lease_seconds=300)


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kwargs) -> None:
        self.at += timedelta(**kwargs)


def queue_event(session_factory, due: datetime | None = T0, status=EventStatus.RECEIVED) -> int:
    payload = make_tally_payload()
    with session_factory() as s:
        event = Event(
            external_id=payload["eventId"],
            payload=payload,
            message=payload["data"]["fields"][-1]["value"],
            status=status,
            next_attempt_at=due,
        )
        s.add(event)
        s.commit()
        return event.id


def get(session_factory, event_id) -> Event:
    with session_factory() as s:
        return s.get(Event, event_id)


def set_status(session_factory, status):
    def process(event_id):
        with session_factory() as s:
            s.get(Event, event_id).status = status
            s.commit()

    return process


def as_utc(value: datetime) -> datetime:
    # SQLite hands datetimes back without tzinfo; everything is stored in UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def test_backoff_doubles_and_is_capped():
    policy = RetryPolicy(base_seconds=10, max_seconds=60)
    no_jitter = lambda: 0.5  # noqa: E731 - 0.5 maps to a jitter factor of exactly 1
    assert [policy.delay(n, no_jitter).total_seconds() for n in (1, 2, 3, 4, 5)] == [
        10, 20, 40, 60, 60
    ]
    assert policy.delay(1, lambda: 0.0).total_seconds() == 8
    assert policy.delay(1, lambda: 1.0).total_seconds() == 12


def test_successful_run_finishes_the_job(session_factory):
    clock = Clock()
    event_id = queue_event(session_factory)
    runner = JobRunner(
        session_factory,
        lambda eid: process_event(eid, session_factory, FakeProvider(VALID), FakeNotifier()),
        POLICY,
        now=clock,
    )
    assert runner.run(event_id) is True
    event = get(session_factory, event_id)
    assert event.status == EventStatus.NOTIFIED
    assert event.attempts == 1
    assert event.next_attempt_at is None and event.locked_until is None


def test_failure_is_retried_later_with_backoff(session_factory):
    clock = Clock()
    event_id = queue_event(session_factory)
    runner = JobRunner(session_factory, set_status(session_factory, "failed"), POLICY, now=clock)

    runner.run(event_id)
    event = get(session_factory, event_id)
    assert event.status == EventStatus.FAILED
    retry_at = as_utc(event.next_attempt_at)
    assert T0 + timedelta(seconds=8) <= retry_at <= T0 + timedelta(seconds=12)

    assert runner.due() == []  # not yet
    clock.advance(seconds=13)
    assert runner.due() == [event_id]


def test_gives_up_after_max_attempts_and_marks_dead(session_factory):
    clock = Clock()
    event_id = queue_event(session_factory)
    runner = JobRunner(
        session_factory, set_status(session_factory, "notify_failed"), POLICY, now=clock
    )
    for _ in range(POLICY.max_attempts):
        assert runner.run_due() == 1
        clock.advance(minutes=5)

    event = get(session_factory, event_id)
    assert event.status == EventStatus.DEAD
    assert event.attempts == 3
    assert event.next_attempt_at is None
    assert runner.run_due() == 0


def test_a_crashing_processor_releases_the_lease_and_retries(session_factory):
    clock = Clock()
    event_id = queue_event(session_factory)

    def boom(_):
        raise RuntimeError("bug")

    runner = JobRunner(session_factory, boom, POLICY, now=clock)
    assert runner.run(event_id) is True
    event = get(session_factory, event_id)
    assert event.status == EventStatus.RECEIVED
    assert event.locked_until is None and event.next_attempt_at is not None


def test_a_leased_event_is_not_run_twice(session_factory):
    clock = Clock()
    event_id = queue_event(session_factory)
    inner_runs = []

    def process(eid):
        # While this run holds the lease, a second worker must not get the event.
        inner_runs.append(other.run(eid))

    runner = JobRunner(session_factory, process, POLICY, now=clock)
    other = JobRunner(session_factory, lambda eid: None, POLICY, now=clock)
    assert runner.run(event_id) is True
    assert inner_runs == [False]


def test_an_expired_lease_can_be_taken_over(session_factory):
    clock = Clock()
    event_id = queue_event(session_factory)
    with session_factory() as s:  # a worker died holding the lease
        s.get(Event, event_id).locked_until = T0 + timedelta(seconds=60)
        s.commit()
    runner = JobRunner(session_factory, set_status(session_factory, "notified"), POLICY, now=clock)

    assert runner.run(event_id) is False
    clock.advance(seconds=61)
    assert runner.run(event_id) is True
    assert get(session_factory, event_id).status == EventStatus.NOTIFIED


def test_unscheduled_and_finished_events_are_never_picked(session_factory):
    clock = Clock()
    queue_event(session_factory, due=None)
    queue_event(session_factory, status=EventStatus.NOTIFIED)
    queue_event(session_factory, status=EventStatus.DEAD)
    runner = JobRunner(session_factory, lambda eid: None, POLICY, now=clock)
    assert runner.due() == []


def test_classified_without_slack_is_finished_not_retried(session_factory):
    clock = Clock()
    event_id = queue_event(session_factory)
    runner = JobRunner(
        session_factory,
        lambda eid: process_event(eid, session_factory, FakeProvider(VALID), None),
        POLICY,
        now=clock,
    )
    runner.run(event_id)
    event = get(session_factory, event_id)
    assert event.status == EventStatus.CLASSIFIED and event.next_attempt_at is None


def test_slack_outage_recovers_on_retry_without_reclassifying(session_factory):
    clock = Clock()
    event_id = queue_event(session_factory)
    notifiers = [FakeNotifier(SlackError("down", 3)), FakeNotifier()]
    providers = [FakeProvider(VALID), FakeProvider()]  # the second raises if called

    def process(eid):
        process_event(eid, session_factory, providers.pop(0), notifiers[0])
        notifiers.pop(0)

    runner = JobRunner(session_factory, process, POLICY, now=clock)
    runner.run_due()
    assert get(session_factory, event_id).status == EventStatus.NOTIFY_FAILED
    clock.advance(minutes=1)
    runner.run_due()
    assert get(session_factory, event_id).status == EventStatus.NOTIFIED
    with session_factory() as s:
        assert s.query(Classification).count() == 1


def test_requeue_revives_a_dead_event_at_the_stage_it_reached(session_factory):
    clock = Clock()
    dead = queue_event(session_factory, due=None, status=EventStatus.DEAD)
    notified = queue_event(session_factory, due=None, status=EventStatus.NOTIFIED)
    with session_factory() as s:
        s.get(Event, dead).attempts = 3
        s.commit()

    runner = JobRunner(session_factory, lambda eid: None, POLICY, now=clock)
    assert runner.requeue([dead, notified, 999]) == [dead]
    event = get(session_factory, dead)
    assert event.status == EventStatus.RECEIVED
    assert event.attempts == 0
    assert runner.due() == [dead]


def test_worker_loop_runs_due_events_until_stopped(session_factory):
    event_id = queue_event(session_factory, due=datetime.now(UTC))
    ran = []

    def process(eid):
        ran.append(eid)
        set_status(session_factory, "notified")(eid)
        worker.stop()

    worker = Worker(JobRunner(session_factory, process, POLICY), poll_seconds=0.01)
    thread = worker.start_thread()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert ran == [event_id]
