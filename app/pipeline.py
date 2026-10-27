"""What happens after a webhook is accepted: classify the message, draft a reply, notify Slack.

Each stage is safe to re-run: an event that is already classified is not sent to the LLM
again, a reply is drafted at most once, and an event that is already notified is left alone.
A failed draft never blocks the notification; the ticket just arrives without a suggestion.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.classifier import ClassificationError, classify
from app.config import Settings
from app.drafting import DraftError, write_draft
from app.helpdesk import set_status
from app.knowledge import KnowledgeBase
from app.llm import LLMProvider, get_provider
from app.models import (
    Classification,
    Event,
    EventStatus,
    Failure,
    Reply,
    ReplyStatus,
    TicketStatus,
)
from app.slack import SlackError, SlackNotifier, build_message
from app.tally import SupportMessage, TallyPayloadError, parse_tally_payload

logger = logging.getLogger("supportflow.pipeline")


@dataclass
class Drafter:
    provider: LLMProvider
    knowledge: KnowledgeBase
    shop_name: str


@dataclass
class PipelineOptions:
    auto_resolve_spam: bool = True
    # Add Send/Resolve buttons to Slack messages (needs SLACK_SIGNING_SECRET).
    slack_buttons: bool = False
    public_url: str = ""


def _message_from_event(event: Event) -> SupportMessage:
    try:
        return parse_tally_payload(event.payload)
    except TallyPayloadError:
        # The stored columns are enough if the raw payload can't be re-read.
        return SupportMessage(
            name=event.customer_name, email=event.customer_email, message=event.message
        )


def _classify(
    session: Session, event: Event, provider: LLMProvider, auto_resolve_spam: bool = True
) -> bool:
    message = _message_from_event(event)
    try:
        outcome = classify(provider, message)
    except ClassificationError as exc:
        logger.error("event %s: %s", event.id, exc)
        session.add(
            Failure(event_id=event.id, stage="classify", error=str(exc), attempts=exc.attempts)
        )
        event.status = EventStatus.FAILED
        session.commit()
        return False

    r = outcome.result
    event.classification = Classification(
        category=r.category,
        urgency=r.urgency,
        sentiment=r.sentiment,
        language=r.language,
        order_id=r.order_id,
        summary=r.summary,
        model=outcome.model,
        latency_ms=outcome.latency_ms,
        attempts=outcome.attempts,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
    )
    event.status = EventStatus.CLASSIFIED
    if auto_resolve_spam and r.category == "spam":
        set_status(event, TicketStatus.RESOLVED)
    session.commit()
    logger.info(
        "event %s classified as %s/%s in %sms (%s attempt(s))",
        event.id, r.category, r.urgency, outcome.latency_ms, outcome.attempts,
    )
    return True


def _should_draft(event: Event) -> bool:
    c = event.classification
    return (
        c is not None
        and c.category != "spam"
        and event.ticket_status == TicketStatus.OPEN
        and not event.replies
        and not any(f.stage == "draft" for f in event.failures)
    )


def _draft(session: Session, event: Event, drafter: Drafter) -> None:
    assert_classified(event)
    c = event.classification
    try:
        draft = write_draft(
            drafter.provider,
            drafter.knowledge,
            message=event.message,
            summary=c.summary,
            category=c.category,
            language=c.language,
            customer_name=event.customer_name,
            order_id=c.order_id,
            shop_name=drafter.shop_name,
        )
    except DraftError as exc:
        logger.warning("event %s: %s", event.id, exc)
        session.add(Failure(event_id=event.id, stage="draft", error=str(exc), attempts=2))
        session.commit()
        return
    session.add(Reply(
        event=event,
        status=ReplyStatus.DRAFT,
        body=draft.reply,
        author="ai",
        model=draft.model[:64],
        needs_human=draft.needs_human,
        note="; ".join(filter(None, [draft.reason, *(f"source: {s}" for s in draft.sources)])),
    ))
    session.commit()
    logger.info("event %s: reply drafted (needs human: %s)", event.id, draft.needs_human)


def assert_classified(event: Event) -> None:
    if event.classification is None:
        raise RuntimeError(f"event {event.id} has no classification yet")


def ticket_url(public_url: str, event_id: int) -> str | None:
    return f"{public_url}/dashboard/tickets/{event_id}" if public_url else None


def _notify(
    session: Session, event: Event, notifier: SlackNotifier, options: PipelineOptions
) -> None:
    assert_classified(event)
    c = event.classification
    draft = event.draft()
    slack_message = build_message(
        event_id=event.id,
        category=c.category,
        urgency=c.urgency,
        sentiment=c.sentiment,
        language=c.language,
        summary=c.summary,
        order_id=c.order_id,
        customer_name=event.customer_name,
        customer_email=event.customer_email,
        message=event.message,
        draft=draft.body if draft else None,
        needs_human=draft.needs_human if draft else False,
        interactive=options.slack_buttons and event.ticket_status == TicketStatus.OPEN,
        ticket_url=ticket_url(options.public_url, event.id),
    )
    try:
        attempts = notifier.send(slack_message)
    except SlackError as exc:
        logger.error("event %s: %s", event.id, exc)
        session.add(
            Failure(event_id=event.id, stage="notify", error=str(exc), attempts=exc.attempts)
        )
        event.status = EventStatus.NOTIFY_FAILED
        session.commit()
        return

    event.status = EventStatus.NOTIFIED
    session.commit()
    logger.info("event %s posted to Slack (%s attempt(s))", event.id, attempts)


def process_event(
    event_id: int,
    session_factory: sessionmaker[Session],
    provider: LLMProvider,
    notifier: SlackNotifier | None = None,
    drafter: Drafter | None = None,
    options: PipelineOptions | None = None,
) -> None:
    options = options or PipelineOptions()
    with session_factory() as session:
        event = session.get(Event, event_id)
        if event is None:
            logger.error("event %s not found", event_id)
            return
        if event.status == EventStatus.NOTIFIED:
            logger.info("event %s already done, skipping", event_id)
            return

        if event.classification is None and not _classify(
            session, event, provider, options.auto_resolve_spam
        ):
            return

        if drafter is not None and _should_draft(event):
            _draft(session, event, drafter)

        if notifier is None:
            logger.info("event %s: Slack not configured, skipping notification", event_id)
            return
        _notify(session, event, notifier, options)


def build_processor(
    settings: Settings, session_factory: sessionmaker[Session]
) -> Callable[[int], None]:
    provider = get_provider(settings)
    notifier = SlackNotifier(settings.slack_webhook_url) if settings.slack_webhook_url else None
    drafter = (
        Drafter(provider, KnowledgeBase.load(settings.knowledge_dir), settings.shop_name)
        if settings.draft_replies
        else None
    )
    options = PipelineOptions(
        auto_resolve_spam=settings.auto_resolve_spam,
        slack_buttons=bool(settings.slack_signing_secret),
        public_url=settings.public_url,
    )

    def process(event_id: int) -> None:
        try:
            process_event(event_id, session_factory, provider, notifier, drafter, options)
        except Exception:  # noqa: BLE001 - a background task must never crash the server
            logger.exception("unexpected error while processing event %s", event_id)

    return process
