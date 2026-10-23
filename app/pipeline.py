"""What happens after a webhook is accepted: classify the message, store the result, notify Slack.

Each stage is safe to re-run: an event that is already classified is not sent to the LLM
again, and an event that is already notified is left alone.
"""

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from app.classifier import ClassificationError, classify
from app.config import Settings
from app.llm import LLMProvider, get_provider
from app.models import Classification, Event, EventStatus, Failure
from app.slack import SlackError, SlackNotifier, build_message
from app.tally import SupportMessage, TallyPayloadError, parse_tally_payload

logger = logging.getLogger("supportflow.pipeline")


def _message_from_event(event: Event) -> SupportMessage:
    try:
        return parse_tally_payload(event.payload)
    except TallyPayloadError:
        # The stored columns are enough if the raw payload can't be re-read.
        return SupportMessage(
            name=event.customer_name, email=event.customer_email, message=event.message
        )


def _classify(session: Session, event: Event, provider: LLMProvider) -> bool:
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
    )
    event.status = EventStatus.CLASSIFIED
    session.commit()
    logger.info(
        "event %s classified as %s/%s in %sms (%s attempt(s))",
        event.id, r.category, r.urgency, outcome.latency_ms, outcome.attempts,
    )
    return True


def _notify(session: Session, event: Event, notifier: SlackNotifier) -> None:
    c = event.classification
    if c is None:
        raise RuntimeError(f"event {event.id} has no classification to notify about")
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
) -> None:
    with session_factory() as session:
        event = session.get(Event, event_id)
        if event is None:
            logger.error("event %s not found", event_id)
            return
        if event.status == EventStatus.NOTIFIED:
            logger.info("event %s already done, skipping", event_id)
            return

        if event.classification is None and not _classify(session, event, provider):
            return

        if notifier is None:
            logger.info("event %s: Slack not configured, skipping notification", event_id)
            return
        _notify(session, event, notifier)


def build_processor(
    settings: Settings, session_factory: sessionmaker[Session]
) -> Callable[[int], None]:
    provider = get_provider(settings)
    notifier = SlackNotifier(settings.slack_webhook_url) if settings.slack_webhook_url else None

    def process(event_id: int) -> None:
        try:
            process_event(event_id, session_factory, provider, notifier)
        except Exception:  # noqa: BLE001 - a background task must never crash the server
            logger.exception("unexpected error while processing event %s", event_id)

    return process
