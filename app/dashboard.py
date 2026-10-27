"""Server-rendered agent dashboard: sign in, see the inbox, read a ticket, send a reply.

Plain HTML forms, no JavaScript build step. Sign-in uses DASHBOARD_PASSWORD; sessions are
signed cookies, and every form carries a CSRF token.
"""

import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from app.api_tickets import load_ticket
from app.helpdesk import (
    URGENCY_RANK,
    HelpdeskError,
    TicketQuery,
    assign,
    list_tickets,
    send_reply,
    set_status,
)
from app.mailer import Mailer
from app.models import TicketStatus
from app.web_sessions import (
    COOKIE_NAME,
    SESSION_SECONDS,
    WebSession,
    check_csrf,
    csrf_token,
    decode_session,
    encode_session,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _ago(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:  # SQLite returns naive UTC
        value = value.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - value).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


def _with_query(params: Any, **changes: Any) -> str:
    merged = {**dict(params), **{k: str(v) for k, v in changes.items()}}
    return urlencode(merged)


TEMPLATES.env.filters["ago"] = _ago
TEMPLATES.env.filters["qs"] = _with_query

# Failed sign-ins allowed per client address per window, to slow down password guessing.
MAX_FAILED_LOGINS = 5
LOGIN_WINDOW_SECONDS = 300

CATEGORIES = ["refund", "order_status", "complaint", "other", "spam"]


class _LoginThrottle:
    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, key: str, now: float) -> bool:
        failures = self._failures[key]
        while failures and failures[0] < now - LOGIN_WINDOW_SECONDS:
            failures.popleft()
        return len(failures) >= MAX_FAILED_LOGINS

    def record_failure(self, key: str, now: float) -> None:
        self._failures[key].append(now)


def build_router(
    session_factory: sessionmaker[Session],
    password: str,
    secret: str,
    mailer: Mailer | None,
    shop_name: str,
    secure_cookies: bool,
) -> APIRouter:
    router = APIRouter(prefix="/dashboard", include_in_schema=False)
    throttle = _LoginThrottle()

    def current(request: Request) -> WebSession | None:
        return decode_session(request.cookies.get(COOKIE_NAME), secret)

    def require(request: Request) -> WebSession:
        session = current(request)
        if session is None:
            raise HTTPException(status_code=303, headers={"Location": "/dashboard/login"})
        return session

    def require_post(request: Request, token: str | None) -> WebSession:
        session = require(request)
        if not check_csrf(session, secret, token):
            raise HTTPException(status_code=403, detail="invalid form token; reload the page")
        return session

    def render(request: Request, name: str, session: WebSession | None, **context: Any) -> Response:
        return TEMPLATES.TemplateResponse(
            request,
            name,
            {
                "shop_name": shop_name,
                "agent": session.agent if session else None,
                "csrf": csrf_token(session, secret) if session else "",
                **context,
            },
        )

    def back_to_ticket(ticket_id: int, **flash: str) -> RedirectResponse:
        query = f"?{urlencode(flash)}" if flash else ""
        return RedirectResponse(f"/dashboard/tickets/{ticket_id}{query}", status_code=303)

    @router.get("", response_class=HTMLResponse)
    def index() -> RedirectResponse:
        return RedirectResponse("/dashboard/tickets", status_code=303)

    @router.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, error: str | None = None) -> Response:
        return render(request, "login.html", None, error=error, enabled=bool(password and secret))

    @router.post("/login")
    def login(
        request: Request,
        agent: Annotated[str, Form(max_length=100)],
        password_given: Annotated[str, Form(alias="password")],
    ) -> Response:
        now = time.time()
        client = request.client.host if request.client else "unknown"
        if not (password and secret):
            return RedirectResponse("/dashboard/login", status_code=303)
        if throttle.blocked(client, now):
            return render(request, "login.html", None, enabled=True,
                          error="Too many failed attempts. Wait a few minutes.")
        agent = agent.strip()
        if not agent or not hmac.compare_digest(password_given.encode(), password.encode()):
            throttle.record_failure(client, now)
            return render(request, "login.html", None, enabled=True,
                          error="Wrong name or password.")
        session = WebSession(agent=agent, expires=int(now) + SESSION_SECONDS,
                             nonce=secrets.token_hex(16))
        response = RedirectResponse("/dashboard/tickets", status_code=303)
        response.set_cookie(
            COOKIE_NAME, encode_session(session, secret), max_age=SESSION_SECONDS,
            httponly=True, samesite="lax", secure=secure_cookies, path="/dashboard",
        )
        return response

    @router.post("/logout")
    def logout(request: Request, csrf: Annotated[str, Form()]) -> Response:
        require_post(request, csrf)
        response = RedirectResponse("/dashboard/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/dashboard")
        return response

    @router.get("/tickets", response_class=HTMLResponse)
    def inbox(
        request: Request,
        status: str = "open",
        urgency: str | None = None,
        category: str | None = None,
        mine: bool = False,
        q: str | None = Query(None, max_length=200),
        page: int = Query(1, ge=1),
    ) -> Response:
        session = require(request)
        status = status if status in (*TicketStatus.ALL, "all") else "open"
        per_page = 50
        query = TicketQuery(
            status=None if status == "all" else status,
            urgency=urgency if urgency in URGENCY_RANK else None,
            category=category if category in CATEGORIES else None,
            assignee=session.agent if mine else None,
            search=q or None,
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        with session_factory() as db:
            tickets, total = list_tickets(db, query)
            counts = {
                s: list_tickets(db, TicketQuery(status=s, limit=1))[1] for s in TicketStatus.ALL
            }
            return render(
                request, "inbox.html", session, tickets=tickets, total=total, counts=counts,
                status=status, urgency=urgency, category=category, mine=mine, q=q or "",
                page=page, pages=max(1, -(-total // per_page)), categories=CATEGORIES,
            )

    @router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
    def ticket(
        request: Request, ticket_id: int, sent: str | None = None, error: str | None = None
    ) -> Response:
        session = require(request)
        with session_factory() as db:
            event = load_ticket(db, ticket_id)
            return render(
                request, "ticket.html", session, t=event, draft=event.draft(),
                can_email=mailer is not None and bool(event.customer_email),
                sent=sent, error=error,
            )

    @router.post("/tickets/{ticket_id}/reply")
    def reply(
        request: Request,
        ticket_id: int,
        csrf: Annotated[str, Form()],
        body: Annotated[str, Form(max_length=10_000)],
        resolve: Annotated[bool, Form()] = False,
    ) -> Response:
        session = require_post(request, csrf)
        with session_factory() as db:
            event = load_ticket(db, ticket_id)
            try:
                send_reply(db, event, body, session.agent, mailer, shop_name, resolve=resolve)
            except HelpdeskError as exc:
                return back_to_ticket(ticket_id, error=str(exc))
        return back_to_ticket(ticket_id, sent="1")

    @router.post("/tickets/{ticket_id}/status")
    def change_status(
        request: Request,
        ticket_id: int,
        csrf: Annotated[str, Form()],
        status: Annotated[str, Form()],
    ) -> Response:
        require_post(request, csrf)
        with session_factory() as db:
            event = load_ticket(db, ticket_id)
            try:
                set_status(event, status)
            except HelpdeskError as exc:
                return back_to_ticket(ticket_id, error=str(exc))
            db.commit()
        return back_to_ticket(ticket_id)

    @router.post("/tickets/{ticket_id}/assign")
    def change_assignee(
        request: Request,
        ticket_id: int,
        csrf: Annotated[str, Form()],
        assignee: Annotated[str, Form(max_length=100)] = "",
    ) -> Response:
        session = require_post(request, csrf)
        with session_factory() as db:
            event = load_ticket(db, ticket_id)
            assign(event, session.agent if assignee == "@me" else assignee)
            db.commit()
        return back_to_ticket(ticket_id)

    return router
