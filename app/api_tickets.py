"""JSON API for the helpdesk: list, read and update tickets, and reply to customers."""

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.helpdesk import HelpdeskError, TicketQuery, assign, list_tickets, send_reply, set_status
from app.mailer import Mailer
from app.models import Event


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def ticket_summary(event: Event) -> dict[str, Any]:
    c = event.classification
    draft = event.draft()
    return {
        "id": event.id,
        "ticket_status": event.ticket_status,
        "assignee": event.assignee,
        "customer_name": event.customer_name,
        "customer_email": event.customer_email,
        "category": c.category if c else None,
        "urgency": c.urgency if c else None,
        "summary": c.summary if c else None,
        "order_id": c.order_id if c else None,
        "has_draft": draft is not None,
        "created_at": _iso(event.created_at),
        "first_response_at": _iso(event.first_response_at),
        "resolved_at": _iso(event.resolved_at),
    }


def ticket_detail(event: Event) -> dict[str, Any]:
    c = event.classification
    return {
        **ticket_summary(event),
        "message": event.message,
        "pipeline_status": event.status,
        "classification": (
            {
                "category": c.category, "urgency": c.urgency, "sentiment": c.sentiment,
                "language": c.language, "order_id": c.order_id, "summary": c.summary,
                "model": c.model,
            }
            if c else None
        ),
        "replies": [
            {
                "id": r.id, "status": r.status, "body": r.body, "author": r.author,
                "model": r.model, "needs_human": r.needs_human, "note": r.note,
                "error": r.error, "created_at": _iso(r.created_at), "sent_at": _iso(r.sent_at),
            }
            for r in event.replies
        ],
    }


class TicketUpdate(BaseModel):
    ticket_status: Literal["open", "pending", "resolved"] | None = None
    # An empty string unassigns.
    assignee: str | None = Field(default=None, max_length=100)


class ReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)
    author: str = Field(min_length=1, max_length=100)
    resolve: bool = False


def load_ticket(session: Session, ticket_id: int) -> Event:
    event = session.get(
        Event,
        ticket_id,
        options=[selectinload(Event.classification), selectinload(Event.replies)],
        # Two agents replying at once would otherwise both email the customer.
        with_for_update=True,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return event


def build_router(
    session_factory: sessionmaker[Session],
    require_admin: Callable[..., None],
    mailer: Mailer | None,
    shop_name: str,
) -> APIRouter:
    router = APIRouter(prefix="/tickets", tags=["tickets"], dependencies=[Depends(require_admin)])

    @router.get("")
    def list_(
        status: Literal["open", "pending", "resolved", "all"] = "open",
        category: str | None = None,
        urgency: Literal["low", "medium", "high"] | None = None,
        assignee: str | None = None,
        q: str | None = Query(None, max_length=200),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        query = TicketQuery(
            status=None if status == "all" else status, category=category, urgency=urgency,
            assignee=assignee, search=q, limit=limit, offset=offset,
        )
        with session_factory() as session:
            rows, total = list_tickets(session, query)
            return {"total": total, "items": [ticket_summary(e) for e in rows]}

    @router.get("/{ticket_id}")
    def get(ticket_id: int) -> dict[str, Any]:
        with session_factory() as session:
            return ticket_detail(load_ticket(session, ticket_id))

    @router.patch("/{ticket_id}")
    def update(ticket_id: int, change: TicketUpdate) -> dict[str, Any]:
        with session_factory() as session:
            event = load_ticket(session, ticket_id)
            if change.ticket_status is not None:
                set_status(event, change.ticket_status)
            if change.assignee is not None:
                assign(event, change.assignee)
            session.commit()
            return ticket_detail(event)

    @router.post("/{ticket_id}/reply")
    def reply(ticket_id: int, request: ReplyRequest) -> dict[str, Any]:
        with session_factory() as session:
            event = load_ticket(session, ticket_id)
            try:
                send_reply(
                    session, event, request.body, request.author, mailer, shop_name,
                    resolve=request.resolve,
                )
            except HelpdeskError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return ticket_detail(event)

    return router

