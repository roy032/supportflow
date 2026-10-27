"""A small job queue on top of the events table, so no event is lost or stuck.

Every accepted event is due immediately (`next_attempt_at = now`). A runner claims an
event by taking a lease (`locked_until`) with one conditional UPDATE, which only one
worker can win, then runs the pipeline. Afterwards the event is either finished
(`next_attempt_at = NULL`), scheduled again with exponential backoff, or, after
`max_attempts` runs, marked `dead` for a person to look at.

If a worker dies mid-run its lease simply expires and another worker picks the event
up. This works the same on SQLite and Postgres, and needs no extra infrastructure.
"""

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Event, EventStatus
from app.observability import event_id_var

logger = logging.getLogger("supportflow.jobs")

# Statuses a worker may pick up. `notified` and `dead` are left alone until someone
# requeues them.
RUNNABLE_STATUSES: tuple[str, ...] = (
    EventStatus.RECEIVED,
    EventStatus.CLASSIFIED,
    EventStatus.FAILED,
    EventStatus.NOTIFY_FAILED,
)

# After a run, these mean the pipeline could not finish and should try again later.
RETRYABLE_STATUSES: tuple[str, ...] = (
    EventStatus.RECEIVED,  # the processor crashed before recording anything
    EventStatus.FAILED,
    EventStatus.NOTIFY_FAILED,
)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_seconds: float = 30.0
    max_seconds: float = 3600.0
    lease_seconds: float = 600.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "RetryPolicy":
        return cls(
            max_attempts=settings.job_max_attempts,
            base_seconds=settings.job_retry_base_seconds,
            max_seconds=settings.job_retry_max_seconds,
            lease_seconds=settings.job_lease_seconds,
        )

    def delay(self, attempts: int, rand: Callable[[], float] = random.random) -> timedelta:
        """Backoff before run number `attempts + 1`: base * 2^(attempts-1), capped, with
        +/-20% jitter so a batch that failed together doesn't retry together."""
        raw = min(self.base_seconds * 2 ** max(attempts - 1, 0), self.max_seconds)
        return timedelta(seconds=raw * (0.8 + 0.4 * rand()))


class JobRunner:
    """Claims due events and runs `process(event_id)` on them under a lease."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        process: Callable[[int], None],
        policy: RetryPolicy | None = None,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self.session_factory = session_factory
        self.process = process
        self.policy = policy or RetryPolicy()
        self._now = now

    def due(self, limit: int = 10) -> list[int]:
        """Ids of events that are due and not leased by another worker, oldest first."""
        now = self._now()
        stmt = (
            select(Event.id)
            .where(
                Event.status.in_(RUNNABLE_STATUSES),
                Event.next_attempt_at.is_not(None),
                Event.next_attempt_at <= now,
                or_(Event.locked_until.is_(None), Event.locked_until < now),
            )
            .order_by(Event.next_attempt_at, Event.id)
            .limit(limit)
        )
        with self.session_factory() as session:
            return list(session.scalars(stmt))

    def _claim(self, event_id: int) -> bool:
        now = self._now()
        stmt = (
            update(Event)
            .where(
                Event.id == event_id,
                Event.status.in_(RUNNABLE_STATUSES),
                Event.next_attempt_at.is_not(None),
                Event.next_attempt_at <= now,
                or_(Event.locked_until.is_(None), Event.locked_until < now),
            )
            .values(
                locked_until=now + timedelta(seconds=self.policy.lease_seconds),
                attempts=Event.attempts + 1,
            )
            .execution_options(synchronize_session=False)
        )
        with self.session_factory() as session:
            claimed = session.execute(stmt).rowcount == 1
            session.commit()
        return claimed

    def _finish(self, event_id: int) -> str | None:
        with self.session_factory() as session:
            event = session.get(Event, event_id)
            if event is None:
                return None
            event.locked_until = None
            if event.status not in RETRYABLE_STATUSES:
                event.next_attempt_at = None
            elif event.attempts >= self.policy.max_attempts:
                logger.error(
                    "event %s gave up after %s attempts (last status %s); marked dead",
                    event_id, event.attempts, event.status,
                )
                event.status = EventStatus.DEAD
                event.next_attempt_at = None
            else:
                delay = self.policy.delay(event.attempts)
                event.next_attempt_at = self._now() + delay
                logger.warning(
                    "event %s attempt %s ended %s; retrying in %.0fs",
                    event_id, event.attempts, event.status, delay.total_seconds(),
                )
            status = event.status
            session.commit()
            return status

    def run(self, event_id: int) -> bool:
        """Run one event if it is due and nobody else holds it. Returns whether it ran."""
        if not self._claim(event_id):
            return False
        token = event_id_var.set(event_id)  # attached to every log line of this run
        try:
            self.process(event_id)
        except Exception:  # noqa: BLE001 - the lease must be released whatever happens
            logger.exception("unexpected error while processing event %s", event_id)
        finally:
            self._finish(event_id)
            event_id_var.reset(token)
        return True

    def run_due(self, limit: int = 10) -> int:
        """Run every due event (up to `limit`); return how many ran."""
        return sum(self.run(event_id) for event_id in self.due(limit))

    def requeue(self, event_ids: list[int]) -> list[int]:
        """Make events due now with a fresh attempt budget; returns the ids that exist.

        Used for manual replay. A dead event goes back to the stage it reached.
        """
        found = []
        with self.session_factory() as session:
            for event_id in event_ids:
                event = session.get(Event, event_id)
                if event is None or event.status == EventStatus.NOTIFIED:
                    continue
                if event.status == EventStatus.DEAD:
                    event.status = (
                        EventStatus.RECEIVED if event.classification is None
                        else EventStatus.CLASSIFIED
                    )
                event.attempts = 0
                event.next_attempt_at = self._now()
                event.locked_until = None
                found.append(event_id)
            session.commit()
        return found


class Worker:
    """Polls for due events in a loop until `stop()` is called. Also runs `periodic` tasks,
    given as (interval_seconds, function) pairs, between polls."""

    def __init__(
        self,
        runner: JobRunner,
        poll_seconds: float = 2.0,
        batch_size: int = 10,
        periodic: list[tuple[float, Callable[[], object]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runner = runner
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self.periodic = periodic or []
        self._clock = clock
        self._last_run = [float("-inf")] * len(self.periodic)
        self._stop = threading.Event()

    def run_periodic(self) -> None:
        for i, (interval, task) in enumerate(self.periodic):
            if self._clock() - self._last_run[i] >= interval:
                self._last_run[i] = self._clock()
                try:
                    task()
                except Exception:  # noqa: BLE001 - one failing task must not stop the worker
                    logger.exception("periodic task %s failed", getattr(task, "__name__", task))

    def run_forever(self) -> None:
        logger.info("worker started (polling every %.1fs)", self.poll_seconds)
        while not self._stop.is_set():
            try:
                ran = self.runner.run_due(self.batch_size)
            except Exception:  # noqa: BLE001 - e.g. the database is briefly unreachable
                logger.exception("worker poll failed")
                ran = 0
            self.run_periodic()
            if ran < self.batch_size:
                self._stop.wait(self.poll_seconds)
        logger.info("worker stopped")

    def start_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, name="supportflow-worker", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
