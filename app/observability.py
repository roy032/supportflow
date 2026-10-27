"""Logging, request ids and Prometheus metrics.

Logs: LOG_FORMAT=json writes one JSON object per line (for Loki, CloudWatch, Datadog...),
with the request id and event id attached to every line logged while handling them.

Metrics: GET /metrics in Prometheus format. Queue and status numbers are read from the
database at scrape time, so they are correct no matter how many web servers and workers
are running; per-process counters cover HTTP traffic and webhook outcomes.
"""

import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.models import Classification, Event, EventStatus, Failure

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
event_id_var: ContextVar[int | None] = ContextVar("event_id", default=None)

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Incoming X-Request-ID values are echoed into logs and headers, so only accept tame ones.
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.event_id = event_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "event_id"):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(level: str, fmt: str = "text") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(_ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(TEXT_FORMAT))
    handler._supportflow = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    # Replace our own handler if configured before (tests build many apps); leave
    # handlers that others installed, such as pytest's log capture, alone.
    for old in [h for h in root.handlers if getattr(h, "_supportflow", False)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    # Alembic announces every plugin it loads at INFO on each startup.
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)


class Metrics:
    """Per-app metrics, in their own registry so test apps don't collide."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.registry = CollectorRegistry()
        self.http_requests = Counter(
            "supportflow_http_requests_total", "HTTP requests handled.",
            ["method", "route", "status"], registry=self.registry,
        )
        self.http_latency = Histogram(
            "supportflow_http_request_duration_seconds", "HTTP request latency.",
            ["method", "route"], registry=self.registry,
        )
        self.webhooks = Counter(
            "supportflow_webhooks_total",
            "Webhook deliveries by outcome (accepted, duplicate, unauthorized, invalid, too_large).",
            ["result"], registry=self.registry,
        )
        self.registry.register(DatabaseCollector(session_factory))


class DatabaseCollector(Collector):
    """Gauges computed from the database on every scrape."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self._now = now or (lambda: datetime.now(UTC))

    def collect(self) -> Iterator[GaugeMetricFamily]:
        now = self._now()
        with self.session_factory() as session:
            by_status = dict(
                session.execute(select(Event.status, func.count()).group_by(Event.status)).all()
            )
            due = select(Event).where(
                Event.next_attempt_at.is_not(None), Event.next_attempt_at <= now
            )
            due_count = session.scalar(select(func.count()).select_from(due.subquery()))
            oldest_due = session.scalar(
                select(func.min(Event.next_attempt_at)).where(
                    Event.next_attempt_at.is_not(None), Event.next_attempt_at <= now
                )
            )
            failures = dict(
                session.execute(
                    select(Failure.stage, func.count())
                    .where(Failure.created_at >= now - timedelta(hours=1))
                    .group_by(Failure.stage)
                ).all()
            )
            recent_latency = select(Classification.latency_ms).order_by(
                Classification.id.desc()
            ).limit(100).subquery()
            avg_latency = session.scalar(select(func.avg(recent_latency.c.latency_ms)))

        events = GaugeMetricFamily(
            "supportflow_events", "Events by current status.", labels=["status"]
        )
        for status in (
            EventStatus.RECEIVED, EventStatus.CLASSIFIED, EventStatus.NOTIFIED,
            EventStatus.FAILED, EventStatus.NOTIFY_FAILED, EventStatus.DEAD,
        ):
            events.add_metric([status], by_status.get(status, 0))
        yield events

        yield GaugeMetricFamily(
            "supportflow_queue_due", "Events waiting for a worker right now.", value=due_count or 0
        )
        lag = 0.0
        if oldest_due is not None:
            if oldest_due.tzinfo is None:  # SQLite returns naive UTC
                oldest_due = oldest_due.replace(tzinfo=UTC)
            lag = max((now - oldest_due).total_seconds(), 0.0)
        yield GaugeMetricFamily(
            "supportflow_queue_lag_seconds",
            "How long the oldest due event has been waiting. Alert if this keeps growing.",
            value=lag,
        )

        recent = GaugeMetricFamily(
            "supportflow_failures_last_hour", "Failures recorded in the last hour.",
            labels=["stage"],
        )
        for stage in ("parse", "classify", "notify"):
            recent.add_metric([stage], failures.get(stage, 0))
        yield recent

        yield GaugeMetricFamily(
            "supportflow_classification_latency_ms_avg",
            "Average LLM classification latency over the last 100 classifications.",
            value=float(avg_latency or 0),
        )


def _route_template(scope: Scope) -> str:
    # The matched route's path ("/events/{id}") keeps metric labels low-cardinality.
    route = scope.get("route")
    return getattr(route, "path", "unmatched")


class RequestContextMiddleware:
    """Gives every request an id (from X-Request-ID or a new one), echoes it in the
    response, attaches it to log lines, and records HTTP metrics."""

    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID_PATTERN.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", []), (b"x-request-id", request_id.encode("ascii"))
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            route = _route_template(scope)
            method = scope.get("method", "")
            self.metrics.http_requests.labels(method, route, str(status)).inc()
            self.metrics.http_latency.labels(method, route).observe(time.perf_counter() - started)
            request_id_var.reset(token)
