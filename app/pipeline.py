"""What happens after a webhook is accepted: classify the message, store the result, notify."""

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from app.classifier import ClassificationError, classify
from app.config import Settings
from app.llm import LLMProvider, get_provider
from app.models import Classification, Event, EventStatus, Failure
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


def process_event(
    event_id: int,
    session_factory: sessionmaker[Session],
    provider: LLMProvider,
) -> None:
    with session_factory() as session:
        event = session.get(Event, event_id)
        if event is None:
            logger.error("event %s not found", event_id)
            return
        if event.classification is not None:
            logger.info("event %s already classified, skipping", event_id)
            return

        message = _message_from_event(event)

        try:
            outcome = classify(provider, message)
        except ClassificationError as exc:
            logger.error("event %s: %s", event_id, exc)
            session.add(
                Failure(event_id=event.id, stage="classify", error=str(exc), attempts=exc.attempts)
            )
            event.status = EventStatus.FAILED
            session.commit()
            return

        r = outcome.result
        session.add(
            Classification(
                event_id=event.id,
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
        )
        event.status = EventStatus.CLASSIFIED
        session.commit()
        logger.info(
            "event %s classified as %s/%s in %sms (%s attempt(s))",
            event_id, r.category, r.urgency, outcome.latency_ms, outcome.attempts,
        )


def build_processor(
    settings: Settings, session_factory: sessionmaker[Session]
) -> Callable[[int], None]:
    provider = get_provider(settings)

    def process(event_id: int) -> None:
        try:
            process_event(event_id, session_factory, provider)
        except Exception:  # noqa: BLE001 - a background task must never crash the server
            logger.exception("unexpected error while processing event %s", event_id)

    return process
