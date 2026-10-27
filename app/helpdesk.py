"""Ticket operations shared by the API, the dashboard and Slack buttons."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.mailer import Mailer, MailError
from app.models import Classification, Event, Reply, ReplyStatus, TicketStatus

logger = logging.getLogger("supportflow.helpdesk")

URGENCY_RANK = {"high": 0, "medium": 1, "low": 2}


class HelpdeskError(RuntimeError):
    """The operation can't be done (bad state or missing configuration)."""


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class TicketQuery:
    status: str | None = TicketStatus.OPEN
    category: str | None = None
    urgency: str | None = None
    assignee: str | None = None
    search: str | None = None
    limit: int = 50
    offset: int = 0


def list_tickets(session: Session, query: TicketQuery) -> tuple[list[Event], int]:
    """Tickets matching `query`, most urgent first and oldest first within an urgency."""
    stmt = select(Event).outerjoin(Classification)
    if query.status:
        stmt = stmt.where(Event.ticket_status == query.status)
    if query.category:
        stmt = stmt.where(Classification.category == query.category)
    if query.urgency:
        stmt = stmt.where(Classification.urgency == query.urgency)
    if query.assignee:
        stmt = stmt.where(Event.assignee == query.assignee)
    if query.search:
        like = f"%{query.search}%"
        stmt = stmt.where(
            or_(
                Event.message.ilike(like),
                Event.customer_name.ilike(like),
                Event.customer_email.ilike(like),
                Classification.order_id.ilike(like),
            )
        )
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    # Unclassified tickets sort with the urgent ones: nobody knows yet how urgent they are.
    rank = case(
        {u: r for u, r in URGENCY_RANK.items()}, value=Classification.urgency, else_=0
    )
    rows = session.scalars(
        stmt.options(selectinload(Event.classification), selectinload(Event.replies))
        .order_by(rank, Event.created_at, Event.id)
        .limit(query.limit)
        .offset(query.offset)
    ).all()
    return list(rows), total


def set_status(event: Event, status: str) -> None:
    if status not in TicketStatus.ALL:
        raise HelpdeskError(f"unknown ticket status {status!r}")
    event.ticket_status = status
    event.resolved_at = _now() if status == TicketStatus.RESOLVED else None


def assign(event: Event, assignee: str | None) -> None:
    event.assignee = (assignee or "").strip()[:100] or None


def send_reply(
    session: Session,
    event: Event,
    body: str,
    author: str,
    mailer: Mailer | None,
    shop_name: str,
    resolve: bool = False,
) -> Reply:
    """Email `body` to the customer and record it. The ticket moves to `pending` (waiting
    on the customer), or `resolved` with `resolve=True`. Any open AI draft is marked sent if
    it went out unchanged, and discarded otherwise. A failed send is recorded and raised."""
    body = body.strip()
    if not body:
        raise HelpdeskError("the reply is empty")
    if not event.customer_email:
        raise HelpdeskError("this customer left no email address")
    if mailer is None:
        raise HelpdeskError("email is not configured (set SMTP_HOST and SMTP_FROM)")

    draft = event.draft()
    if draft is not None and draft.body.strip() == body:
        reply = draft
        reply.author = author
    else:
        if draft is not None:
            draft.status = ReplyStatus.DISCARDED
        reply = Reply(event=event, body=body, author=author)
        session.add(reply)

    subject = f"Re: your message to {shop_name}"
    if event.classification and event.classification.order_id:
        subject = f"Re: your order {event.classification.order_id}"
    try:
        mailer.send(event.customer_email, subject, body, reference=str(event.id))
    except MailError as exc:
        reply.status = ReplyStatus.FAILED
        reply.error = str(exc)[:2000]
        session.commit()
        logger.error("ticket %s: %s", event.id, exc)
        raise HelpdeskError(str(exc)) from exc

    now = _now()
    reply.status = ReplyStatus.SENT
    reply.error = None
    reply.sent_at = now
    event.first_response_at = event.first_response_at or now
    set_status(event, TicketStatus.RESOLVED if resolve else TicketStatus.PENDING)
    session.commit()
    logger.info("ticket %s: reply sent by %s", event.id, author)
    return reply


def overdue_for_escalation(
    session: Session, sla: dict[str, timedelta], now: datetime | None = None
) -> list[Event]:
    """Open tickets with no reply yet that have waited longer than their urgency's SLA and
    have not been escalated before."""
    now = now or _now()
    conditions = [
        (Classification.urgency == urgency) & (Event.created_at <= now - limit)
        for urgency, limit in sla.items()
    ]
    if not conditions:
        return []
    stmt = (
        select(Event)
        .join(Classification)
        .where(
            Event.ticket_status == TicketStatus.OPEN,
            Event.first_response_at.is_(None),
            Event.escalated_at.is_(None),
            or_(*conditions),
        )
        .order_by(Event.created_at)
    )
    return list(session.scalars(stmt))
