"""Buttons on Slack messages: send the suggested reply, or resolve the ticket.

Needs a Slack app with Interactivity turned on and its Request URL set to
https://<your server>/slack/interactions. Every request is verified with the app's
signing secret; see https://api.slack.com/authentication/verifying-requests-from-slack
"""

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from sqlalchemy.orm import Session, sessionmaker

from app.api_tickets import load_ticket
from app.helpdesk import HelpdeskError, send_reply, set_status
from app.mailer import Mailer
from app.models import TicketStatus
from app.slack import escape_mrkdwn

logger = logging.getLogger("supportflow.slack_actions")

# Slack's own guidance: reject requests older than five minutes (replay protection).
MAX_REQUEST_AGE_SECONDS = 300
# Only ever post back to Slack itself.
SLACK_RESPONSE_URL_PREFIX = "https://hooks.slack.com/"


def verify_slack_signature(
    body: bytes, timestamp: str | None, signature: str | None, secret: str, now: float | None = None
) -> bool:
    if not (secret and timestamp and signature):
        return False
    try:
        age = abs((now or time.time()) - int(timestamp))
    except ValueError:
        return False
    if age > MAX_REQUEST_AGE_SECONDS:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode("utf-8", errors="replace"))


def _post_to_response_url(url: str, message: dict[str, Any]) -> None:
    try:
        httpx.post(url, json=message, timeout=10.0)
    except httpx.HTTPError as exc:
        logger.warning("could not post the button result back to Slack: %s", exc)


def build_router(
    session_factory: sessionmaker[Session],
    signing_secret: str,
    mailer: Mailer | None,
    shop_name: str,
    respond: Callable[[str, dict[str, Any]], None] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["slack"])

    def handle(action_id: str, ticket_id: int, actor: str, response_url: str | None) -> None:
        with session_factory() as session:
            try:
                event = load_ticket(session, ticket_id)
            except HTTPException:
                text = f":warning: Ticket #{ticket_id} no longer exists."
            else:
                try:
                    if action_id == "send_draft":
                        draft = event.draft()
                        if draft is None:
                            raise HelpdeskError("there is no unsent suggested reply any more")
                        send_reply(session, event, draft.body, actor, mailer, shop_name)
                        text = f":white_check_mark: Reply to ticket #{ticket_id} sent by {actor}."
                    elif action_id == "resolve":
                        set_status(event, TicketStatus.RESOLVED)
                        session.commit()
                        text = f":white_check_mark: Ticket #{ticket_id} resolved by {actor}."
                    else:
                        return
                except HelpdeskError as exc:
                    text = f":warning: Ticket #{ticket_id}: {escape_mrkdwn(str(exc))}"
        logger.info("slack action %s on ticket %s by %s", action_id, ticket_id, actor)
        if response_url and response_url.startswith(SLACK_RESPONSE_URL_PREFIX):
            # Posted as a reply in the thread of the original message; it stays visible.
            (respond or _post_to_response_url)(
                response_url, {"text": text, "replace_original": False}
            )

    @router.post("/slack/interactions", include_in_schema=False)
    async def interactions(request: Request, background: BackgroundTasks) -> Response:
        body = await request.body()
        if not verify_slack_signature(
            body,
            request.headers.get("x-slack-request-timestamp"),
            request.headers.get("x-slack-signature"),
            signing_secret,
        ):
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            payload = json.loads(parse_qs(body.decode())["payload"][0])
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="bad payload") from exc

        user = payload.get("user") or {}
        actor = f"{user.get('username') or user.get('name') or user.get('id') or 'someone'} (Slack)"
        for action in payload.get("actions") or []:
            action_id = action.get("action_id")
            if action_id not in ("send_draft", "resolve"):
                continue  # e.g. the "Open ticket" link button
            try:
                ticket_id = int(action.get("value"))
            except (TypeError, ValueError):
                continue
            # Slack wants an answer within 3 seconds; sending email can take longer.
            background.add_task(handle, action_id, ticket_id, actor, payload.get("response_url"))
        return Response(status_code=200)

    return router
