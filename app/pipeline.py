"""Pipeline that runs after a webhook is accepted. Filled in by the classification step."""

import logging

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings

logger = logging.getLogger("supportflow.pipeline")


def build_processor(settings: Settings, session_factory: sessionmaker[Session]):
    def process(event_id: int) -> None:
        logger.info("event %s stored (classification not wired up yet)", event_id)

    return process
