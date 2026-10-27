"""Tell Slack about open tickets nobody has answered within their urgency's SLA."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.helpdesk import overdue_for_escalation
from app.models import Event
from app.pipeline import ticket_url
from app.slack import SlackError, SlackNotifier, build_escalation

logger = logging.getLogger("supportflow.escalation")


def escalate_overdue(
    session_factory: sessionmaker[Session],
    notifier: SlackNotifier,
    sla: dict[str, timedelta],
    public_url: str = "",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Post one escalation per overdue ticket; returns how many were posted."""
    posted = 0
    current = now()
    with session_factory() as session:
        for event in overdue_for_escalation(session, sla, current):
            created = event.created_at
            if created.tzinfo is None:  # SQLite returns naive UTC
                created = created.replace(tzinfo=UTC)
            message = build_escalation(
                event_id=event.id,
                urgency=event.classification.urgency,
                summary=event.classification.summary,
                waited_minutes=int((current - created).total_seconds() // 60),
                ticket_url=ticket_url(public_url, event.id),
            )
            # Claim the escalation first, so two workers never both post it.
            claimed = session.execute(
                update(Event)
                .where(Event.id == event.id, Event.escalated_at.is_(None))
                .values(escalated_at=current)
                .execution_options(synchronize_session=False)
            ).rowcount
            session.commit()
            if not claimed:
                continue
            try:
                notifier.send(message)
            except SlackError as exc:
                logger.error("could not escalate ticket %s: %s", event.id, exc)
                session.execute(  # release it; tried again on the next round
                    update(Event).where(Event.id == event.id).values(escalated_at=None)
                )
                session.commit()
                continue
            posted += 1
            logger.warning("ticket %s escalated: no reply within SLA", event.id)
    return posted


def periodic_tasks(
    settings: Settings, session_factory: sessionmaker[Session]
) -> list[tuple[float, Callable[[], object]]]:
    """The worker's periodic jobs for these settings."""
    sla = settings.sla()
    if not (settings.slack_webhook_url and sla):
        return []
    notifier = SlackNotifier(settings.slack_webhook_url)

    def escalate() -> int:
        return escalate_overdue(session_factory, notifier, sla, settings.public_url)

    return [(60.0, escalate)]
