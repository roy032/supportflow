"""Find events that did not finish and run them through the pipeline again.

Replaying is safe because each pipeline stage is idempotent: a classified event is not
sent to the LLM again, and a notified event is skipped. See scripts/replay_events.py.
"""

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Event, EventStatus

# Every status except `notified`. `classified` is included because an event stays
# there when Slack was not configured at the time it was processed.
UNFINISHED_STATUSES: tuple[str, ...] = (
    EventStatus.RECEIVED,
    EventStatus.CLASSIFIED,
    EventStatus.FAILED,
    EventStatus.NOTIFY_FAILED,
)

# Events touched more recently than this may still be in progress in the web server.
DEFAULT_MIN_AGE = timedelta(minutes=5)


def find_unfinished(
    session: Session,
    statuses: Iterable[str] = UNFINISHED_STATUSES,
    min_age: timedelta = DEFAULT_MIN_AGE,
    limit: int = 100,
    now: datetime | None = None,
) -> list[int]:
    """Return ids of events in `statuses` that were last updated at least `min_age` ago."""
    statuses = [s for s in statuses if s != EventStatus.NOTIFIED]
    cutoff = (now or datetime.now(timezone.utc)) - min_age
    stmt = (
        select(Event.id)
        .where(Event.status.in_(statuses), Event.updated_at <= cutoff)
        .order_by(Event.id)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def replay(
    event_ids: Iterable[int],
    process: Callable[[int], None],
    session_factory: sessionmaker[Session],
) -> dict[int, tuple[str | None, str | None]]:
    """Run `process` on each event; return {event_id: (status_before, status_after)}.

    A status of None means the event does not exist.
    """

    def status_of(event_id: int) -> str | None:
        with session_factory() as session:
            event = session.get(Event, event_id)
            return event.status if event else None

    results: dict[int, tuple[str | None, str | None]] = {}
    for event_id in event_ids:
        before = status_of(event_id)
        if before is not None:
            process(event_id)
        results[event_id] = (before, status_of(event_id))
    return results
