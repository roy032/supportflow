"""Request ids, JSON logs and Prometheus metrics."""

import json
import logging
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Event, Failure
from app.observability import DatabaseCollector, JsonFormatter, event_id_var, request_id_var
from tests.conftest import ADMIN_HEADERS, encode, make_tally_payload, sign


def make_client(settings, session_factory):
    return TestClient(create_app(settings, session_factory, processor=lambda eid: None))


def test_every_response_has_a_request_id(settings, session_factory):
    client = make_client(settings, session_factory)
    generated = client.get("/health").headers["x-request-id"]
    assert len(generated) == 32
    assert client.get("/health", headers={"X-Request-ID": "abc-123"}).headers[
        "x-request-id"
    ] == "abc-123"


def test_unsafe_incoming_request_ids_are_replaced(settings, session_factory):
    client = make_client(settings, session_factory)
    r = client.get("/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert r.headers["x-request-id"] != "bad id\nwith newline"
    assert len(r.headers["x-request-id"]) == 32


def test_json_log_lines_carry_request_and_event_ids():
    record = logging.LogRecord("supportflow", logging.INFO, __file__, 1, "hello %s", ("x",), None)
    record.request_id = "req-1"
    record.event_id = 42
    entry = json.loads(JsonFormatter().format(record))
    assert entry["message"] == "hello x"
    assert entry["request_id"] == "req-1" and entry["event_id"] == 42
    assert entry["level"] == "INFO"
    assert request_id_var.get() is None and event_id_var.get() is None


def test_metrics_need_the_admin_key(settings, session_factory):
    client = make_client(settings, session_factory)
    assert client.get("/metrics").status_code == 401


def test_metrics_report_webhooks_http_and_queue(settings, session_factory):
    client = make_client(settings, session_factory)
    body = encode(make_tally_payload(event_id="m-1"))
    client.post("/webhooks/tally", content=body, headers=sign(body))
    client.post("/webhooks/tally", content=body, headers=sign(body))  # duplicate
    client.post("/webhooks/tally", content=body, headers=sign(body, secret="wrong"))

    text = client.get("/metrics", headers=ADMIN_HEADERS).text
    assert 'supportflow_webhooks_total{result="accepted"} 1.0' in text
    assert 'supportflow_webhooks_total{result="duplicate"} 1.0' in text
    assert 'supportflow_webhooks_total{result="unauthorized"} 1.0' in text
    assert (
        'supportflow_http_requests_total{method="POST",route="/webhooks/tally",status="202"} 1.0'
        in text
    )
    # The no-op processor leaves the event scheduled for a retry.
    assert 'supportflow_events{status="received"} 1.0' in text


def test_database_gauges(session_factory):
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    with session_factory() as s:
        for i, (status, due) in enumerate(
            [("received", now - timedelta(seconds=90)), ("dead", None), ("notified", None)]
        ):
            s.add(Event(external_id=f"e{i}", payload={}, message="m", status=status,
                        next_attempt_at=due))
        s.add(Failure(stage="notify", error="x", created_at=now - timedelta(minutes=5)))
        s.add(Failure(stage="notify", error="old", created_at=now - timedelta(hours=3)))
        s.commit()

    metrics = {
        (m.name, tuple(sample.labels.values())): sample.value
        for m in DatabaseCollector(session_factory, now=lambda: now).collect()
        for sample in m.samples
    }
    assert metrics[("supportflow_events", ("dead",))] == 1
    assert metrics[("supportflow_queue_due", ())] == 1
    assert metrics[("supportflow_queue_lag_seconds", ())] == 90
    assert metrics[("supportflow_failures_last_hour", ("notify",))] == 1
