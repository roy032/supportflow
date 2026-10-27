"""FastAPI application: webhook intake plus small read-only endpoints for monitoring."""

import hmac
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_session_factory
from app.jobs import JobRunner, RetryPolicy, Worker
from app.models import Event, Failure
from app.observability import Metrics, RequestContextMiddleware, configure_logging
from app.security import verify_tally_signature
from app.tally import TallyPayloadError, parse_tally_payload

logger = logging.getLogger("supportflow")

# Length of events.external_id.
EXTERNAL_ID_MAX_LENGTH = 128

# A processor takes the id of a stored event and runs the rest of the pipeline.
Processor = Callable[[int], None]


def _noop_processor(event_id: int) -> None:  # pragma: no cover - placeholder
    logger.info("event %s stored; no processor configured", event_id)


async def _read_body(request: Request, limit: int) -> bytes:
    """Read the request body, refusing anything over `limit` bytes without buffering it all."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            too_big = int(declared) > limit
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from None
        if too_big:
            raise HTTPException(status_code=413, detail="body is too large")
    # Content-Length can be absent (chunked) or wrong, so count what actually arrives.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _clip(value: str | None, length: int) -> str | None:
    """Fit a form value into its column; Postgres rejects over-long strings."""
    return value[:length] if value is not None else None


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    processor: Processor | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    if not settings.tally_signing_secret:
        logger.warning("TALLY_SIGNING_SECRET is not set; every webhook will be rejected with 401")
    if not settings.admin_api_key:
        logger.warning("ADMIN_API_KEY is not set; /events and /failures will reject every request")

    if session_factory is None:
        engine = make_engine(settings.database_url)
        init_db(engine)
        session_factory = make_session_factory(engine)

    if processor is None:
        from app.pipeline import build_processor

        processor = build_processor(settings, session_factory)

    runner = JobRunner(session_factory, processor, RetryPolicy.from_settings(settings))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        worker = None
        if settings.run_worker_in_process:
            from app.escalation import periodic_tasks

            worker = Worker(
                runner,
                poll_seconds=settings.worker_poll_seconds,
                periodic=periodic_tasks(settings, session_factory),
            )
            worker.start_thread()
        yield
        if worker is not None:
            worker.stop()

    app = FastAPI(
        title="SupportFlow",
        description="AI support-automation pipeline: Tally webhook -> LLM classification -> Slack.",
        version="0.1.0",
        lifespan=lifespan,
    )
    metrics = Metrics(session_factory)
    app.add_middleware(RequestContextMiddleware, metrics=metrics)

    bearer = HTTPBearer(auto_error=False, description="The ADMIN_API_KEY from .env")

    def require_admin(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        """Guard endpoints that return customer data. Fails closed when no key is configured."""
        key = settings.admin_api_key
        if not (
            key
            and credentials is not None
            and hmac.compare_digest(credentials.credentials.encode(), key.encode())
        ):
            raise HTTPException(
                status_code=401,
                detail="invalid or missing API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def record_failure(stage: str, error: str, event_id: int | None = None) -> None:
        with session_factory() as session:
            session.add(Failure(event_id=event_id, stage=stage, error=error[:2000]))
            session.commit()

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            with session_factory() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            logger.error("health check failed: %s", exc)
            return JSONResponse({"status": "degraded", "database": "error"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "ok"})

    @app.post("/webhooks/tally", status_code=202)
    async def tally_webhook(request: Request, background: BackgroundTasks) -> Any:
        try:
            body = await _read_body(request, settings.max_webhook_body_bytes)
        except HTTPException as exc:
            metrics.webhooks.labels("too_large" if exc.status_code == 413 else "invalid").inc()
            raise

        # 1. Authenticity first: nothing is parsed or stored before the signature checks out.
        signature = request.headers.get("tally-signature")
        if not verify_tally_signature(body, signature, settings.tally_signing_secret):
            logger.warning("rejected webhook with invalid signature")
            metrics.webhooks.labels("unauthorized").inc()
            raise HTTPException(status_code=401, detail="invalid signature")

        # 2. Shape checks.
        try:
            payload = json.loads(body)
        except ValueError:
            logger.warning("rejected webhook: body is not valid JSON")
            metrics.webhooks.labels("invalid").inc()
            raise HTTPException(status_code=400, detail="body is not valid JSON") from None
        if not isinstance(payload, dict) or not payload.get("eventId"):
            logger.warning("rejected webhook: missing eventId")
            metrics.webhooks.labels("invalid").inc()
            raise HTTPException(status_code=400, detail="missing eventId")
        external_id = str(payload["eventId"])
        if len(external_id) > EXTERNAL_ID_MAX_LENGTH:
            logger.warning("rejected webhook: eventId longer than %s", EXTERNAL_ID_MAX_LENGTH)
            metrics.webhooks.labels("invalid").inc()
            raise HTTPException(status_code=400, detail="eventId is too long")

        try:
            message = parse_tally_payload(payload)
        except TallyPayloadError as exc:
            logger.warning("rejected webhook %s: %s", external_id, exc)
            record_failure("parse", f"{external_id}: {exc}")
            metrics.webhooks.labels("invalid").inc()
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # 3. Idempotency: Tally retries deliveries, so the same eventId can arrive twice.
        with session_factory() as session:
            existing = session.scalar(select(Event).where(Event.external_id == external_id))
            if existing is not None:
                logger.info("duplicate webhook %s ignored", external_id)
                metrics.webhooks.labels("duplicate").inc()
                return JSONResponse(
                    {"status": "duplicate", "event_id": existing.id}, status_code=200
                )

            event = Event(
                external_id=external_id,
                payload=payload,
                customer_name=_clip(message.name, 200),
                customer_email=_clip(message.email, 320),
                message=message.message,
                next_attempt_at=datetime.now(UTC),
            )
            session.add(event)
            try:
                session.commit()
            except IntegrityError:
                # Two deliveries raced past the check above; the unique index caught it.
                session.rollback()
                existing = session.scalar(select(Event).where(Event.external_id == external_id))
                metrics.webhooks.labels("duplicate").inc()
                return JSONResponse(
                    {"status": "duplicate", "event_id": existing.id if existing else None},
                    status_code=200,
                )
            event_id = event.id

        # 4. Respond fast; classification and Slack run after the response is sent.
        # The event is already queued, so if this first try fails (or the server stops
        # before it runs) a worker retries it with backoff.
        background.add_task(runner.run, event_id)
        logger.info("accepted webhook %s as event %s", external_id, event_id)
        metrics.webhooks.labels("accepted").inc()
        return {"status": "accepted", "event_id": event_id}

    @app.get("/metrics", dependencies=[Depends(require_admin)], include_in_schema=False)
    def prometheus_metrics() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/events", dependencies=[Depends(require_admin)])
    def list_events(limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
        with session_factory() as session:
            rows = session.scalars(select(Event).order_by(Event.id.desc()).limit(limit)).all()
            return [
                {
                    "id": e.id,
                    "external_id": e.external_id,
                    "status": e.status,
                    "attempts": e.attempts,
                    "next_attempt_at": (
                        e.next_attempt_at.isoformat() if e.next_attempt_at else None
                    ),
                    "created_at": e.created_at.isoformat(),
                    "message": e.message[:200],
                    "classification": (
                        {
                            "category": e.classification.category,
                            "urgency": e.classification.urgency,
                            "sentiment": e.classification.sentiment,
                            "language": e.classification.language,
                            "order_id": e.classification.order_id,
                            "summary": e.classification.summary,
                            "latency_ms": e.classification.latency_ms,
                            "model": e.classification.model,
                            "input_tokens": e.classification.input_tokens,
                            "output_tokens": e.classification.output_tokens,
                        }
                        if e.classification
                        else None
                    ),
                }
                for e in rows
            ]

    @app.get("/failures", dependencies=[Depends(require_admin)])
    def list_failures(limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
        with session_factory() as session:
            rows = session.scalars(select(Failure).order_by(Failure.id.desc()).limit(limit)).all()
            return [
                {
                    "id": f.id,
                    "event_id": f.event_id,
                    "stage": f.stage,
                    "error": f.error,
                    "attempts": f.attempts,
                    "created_at": f.created_at.isoformat(),
                }
                for f in rows
            ]

    # Helpdesk: tickets API, Slack buttons and the agent dashboard.
    from app import api_tickets, dashboard, slack_actions
    from app.mailer import build_mailer

    mailer = build_mailer(settings)
    app.include_router(
        api_tickets.build_router(session_factory, require_admin, mailer, settings.shop_name)
    )
    if settings.slack_signing_secret:
        app.include_router(
            slack_actions.build_router(
                session_factory, settings.slack_signing_secret, mailer, settings.shop_name
            )
        )
    app.include_router(
        dashboard.build_router(
            session_factory,
            password=settings.dashboard_password,
            secret=settings.session_secret or settings.admin_api_key,
            mailer=mailer,
            shop_name=settings.shop_name,
            secure_cookies=settings.public_url.startswith("https://"),
        )
    )

    return app


def get_app() -> FastAPI:
    """Entry point for uvicorn: `uvicorn app.main:app`."""
    return create_app()


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    # `app.main:app` is built lazily, so importing this module in tests
    # doesn't read the real .env or create the real database.
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
