"""Database tables.

events           one row per accepted webhook (raw payload kept for replay/debugging); also
                 the job queue: next_attempt_at says when a worker should (re)process it
classifications  the LLM's structured output for an event
failures         anything that went wrong, with the pipeline stage it happened in
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class EventStatus:
    RECEIVED = "received"
    CLASSIFIED = "classified"
    NOTIFIED = "notified"
    NOTIFY_FAILED = "notify_failed"
    FAILED = "failed"
    # Gave up after JOB_MAX_ATTEMPTS automatic runs; only a manual replay retries it.
    DEAD = "dead"


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Tally's eventId. Unique, so a redelivered webhook can't create a second row.
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(32), default="tally")
    status: Mapped[str] = mapped_column(String(32), default=EventStatus.RECEIVED, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    customer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    customer_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    message: Mapped[str] = mapped_column(Text)

    # Job queue. A worker may run the event when next_attempt_at has passed and no other
    # worker holds an unexpired lease (locked_until). NULL next_attempt_at: nothing to do.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    classification: Mapped["Classification | None"] = relationship(
        back_populates="event", uselist=False, cascade="all, delete-orphan"
    )
    failures: Mapped[list["Failure"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class Classification(Base):
    __tablename__ = "classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), unique=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    urgency: Mapped[str] = mapped_column(String(16))
    sentiment: Mapped[str] = mapped_column(String(16))
    language: Mapped[str] = mapped_column(String(8))
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(64))
    latency_ms: Mapped[int] = mapped_column(Integer)
    # Tokens used across all attempts, when the provider reports them.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    event: Mapped[Event] = relationship(back_populates="classification")


class Failure(Base):
    __tablename__ = "failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)  # parse / classify / notify
    error: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    event: Mapped[Event | None] = relationship(back_populates="failures")
