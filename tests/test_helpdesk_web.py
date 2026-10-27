"""Tickets API, Slack buttons and the dashboard, through the real FastAPI app."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.main import create_app
from app.models import Event, ReplyStatus, TicketStatus
from app.slack_actions import verify_slack_signature
from tests.conftest import ADMIN_HEADERS
from tests.test_helpdesk import FakeMailer, get, make_ticket

SLACK_SECRET = "slack-signing-secret"
PASSWORD = "correct horse battery"


@pytest.fixture
def mailer(monkeypatch):
    fake = FakeMailer()
    monkeypatch.setattr("app.mailer.build_mailer", lambda settings: fake)
    return fake


@pytest.fixture
def app(settings, session_factory, mailer):
    configured = settings.model_copy(update={
        "slack_signing_secret": SLACK_SECRET,
        "dashboard_password": PASSWORD,
        "shop_name": "Test Shop",
    })
    return create_app(configured, session_factory, processor=lambda eid: None)


@pytest.fixture
def client(app):
    return TestClient(app)


# tickets API


def test_tickets_api_requires_the_admin_key(client):
    assert client.get("/tickets").status_code == 401


def test_list_get_update_and_reply(client, session_factory, mailer):
    ticket = make_ticket(session_factory, draft="Draft text")

    listing = client.get("/tickets", headers=ADMIN_HEADERS).json()
    assert listing["total"] == 1
    assert listing["items"][0]["has_draft"] is True

    detail = client.get(f"/tickets/{ticket}", headers=ADMIN_HEADERS).json()
    assert detail["replies"][0]["status"] == "draft"

    patched = client.patch(f"/tickets/{ticket}", headers=ADMIN_HEADERS,
                           json={"assignee": "Ayesha"}).json()
    assert patched["assignee"] == "Ayesha"

    r = client.post(f"/tickets/{ticket}/reply", headers=ADMIN_HEADERS,
                    json={"body": "Draft text", "author": "Ayesha", "resolve": True})
    assert r.status_code == 200
    assert r.json()["ticket_status"] == "resolved"
    assert mailer.sent[0]["body"] == "Draft text"
    assert client.get("/tickets", headers=ADMIN_HEADERS).json()["total"] == 0
    assert client.get("/tickets?status=all", headers=ADMIN_HEADERS).json()["total"] == 1


def test_reply_without_customer_email_is_a_conflict(client, session_factory):
    ticket = make_ticket(session_factory, email=None)
    r = client.post(f"/tickets/{ticket}/reply", headers=ADMIN_HEADERS,
                    json={"body": "Hi", "author": "A"})
    assert r.status_code == 409


def test_unknown_ticket_is_404(client):
    assert client.get("/tickets/999", headers=ADMIN_HEADERS).status_code == 404


# Slack buttons


def slack_post(client, payload, secret=SLACK_SECRET, timestamp=None):
    body = urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(timestamp or time.time()))
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body,
                           hashlib.sha256).hexdigest()
    return client.post("/slack/interactions", content=body, headers={
        "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
        "Content-Type": "application/x-www-form-urlencoded",
    })


def action(action_id, ticket):
    return {"user": {"username": "ayesha"}, "response_url": "https://hooks.slack.com/actions/x",
            "actions": [{"action_id": action_id, "value": str(ticket)}]}


def test_slack_signature_rules():
    body, ts = b"payload=x", str(int(time.time()))
    good = "v0=" + hmac.new(b"s", b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    assert verify_slack_signature(body, ts, good, "s")
    assert not verify_slack_signature(body, ts, good, "other")
    assert not verify_slack_signature(body, str(int(ts) - 600), good, "s")  # replayed
    assert not verify_slack_signature(body, None, good, "s")
    assert not verify_slack_signature(body, ts, good, "")


def test_slack_rejects_bad_signatures(client):
    assert slack_post(client, action("resolve", 1), secret="wrong").status_code == 401
    assert slack_post(client, action("resolve", 1), timestamp=time.time() - 3600).status_code == 401


@pytest.fixture(autouse=True)
def slack_responses(monkeypatch):
    """Capture what the button handler posts back to Slack instead of calling Slack."""
    posted = []
    monkeypatch.setattr("app.slack_actions._post_to_response_url",
                        lambda url, message: posted.append((url, message)))
    return posted


def test_slack_send_draft_emails_the_customer(client, session_factory, mailer, slack_responses):
    ticket = make_ticket(session_factory, draft="Here is your answer")
    r = slack_post(client, action("send_draft", ticket))
    assert r.status_code == 200
    event = get(session_factory, ticket)
    assert event.replies[0].status == ReplyStatus.SENT
    assert event.replies[0].author == "ayesha (Slack)"
    assert mailer.sent[0]["body"] == "Here is your answer"
    assert len(slack_responses) == 1
    assert "sent by ayesha (Slack)" in slack_responses[0][1]["text"]


def test_slack_resolve_and_ignored_actions(client, session_factory):
    ticket = make_ticket(session_factory)
    payload = action("resolve", ticket)
    payload["actions"].insert(0, {"action_id": "open_ticket", "url": "https://x"})
    assert slack_post(client, payload).status_code == 200
    assert get(session_factory, ticket).ticket_status == TicketStatus.RESOLVED


def test_slack_endpoint_is_absent_without_a_signing_secret(settings, session_factory):
    client = TestClient(create_app(settings, session_factory, processor=lambda eid: None))
    assert client.post("/slack/interactions", content=b"payload={}").status_code == 404


# dashboard


def login(client, agent="Ayesha", password=PASSWORD):
    return client.post("/dashboard/login", data={"agent": agent, "password": password},
                       follow_redirects=False)


def csrf_of(client, path):
    html = client.get(path).text
    marker = 'name="csrf" value="'
    start = html.index(marker) + len(marker)
    return html[start:html.index('"', start)]


def test_dashboard_requires_sign_in(client):
    r = client.get("/dashboard/tickets", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/login"


def test_wrong_password_and_throttling(client):
    for _ in range(5):
        assert "Wrong name or password" in login(client, password="nope").text
    assert "Too many failed attempts" in login(client).text


def test_sign_in_inbox_ticket_and_reply(client, session_factory, mailer):
    ticket = make_ticket(session_factory, draft="Draft for <b>you</b>")
    r = login(client)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie

    inbox = client.get("/dashboard/tickets").text
    assert "Item arrived damaged." in inbox and "reply drafted" in inbox

    page = client.get(f"/dashboard/tickets/{ticket}").text
    assert "Draft for &lt;b&gt;you&lt;/b&gt;" in page  # escaped, not rendered as HTML
    token = csrf_of(client, f"/dashboard/tickets/{ticket}")

    r = client.post(f"/dashboard/tickets/{ticket}/reply",
                    data={"csrf": "forged", "body": "x"}, follow_redirects=False)
    assert r.status_code == 403

    r = client.post(f"/dashboard/tickets/{ticket}/reply",
                    data={"csrf": token, "body": "Thanks, refund on its way", "resolve": "true"},
                    follow_redirects=False)
    assert r.status_code == 303 and "sent=1" in r.headers["location"]
    event = get(session_factory, ticket)
    assert event.ticket_status == TicketStatus.RESOLVED
    assert [r.status for r in event.replies] == ["discarded", "sent"]
    assert event.replies[-1].author == "Ayesha"


def test_dashboard_status_and_assign(client, session_factory):
    ticket = make_ticket(session_factory)
    login(client)
    token = csrf_of(client, f"/dashboard/tickets/{ticket}")
    client.post(f"/dashboard/tickets/{ticket}/assign", data={"csrf": token, "assignee": "@me"})
    client.post(f"/dashboard/tickets/{ticket}/status", data={"csrf": token, "status": "pending"})
    event = get(session_factory, ticket)
    assert event.assignee == "Ayesha" and event.ticket_status == "pending"
    mine = client.get("/dashboard/tickets?status=pending&mine=true").text
    assert "Item arrived damaged." in mine


def test_dashboard_is_off_without_a_password(settings, session_factory):
    client = TestClient(create_app(settings, session_factory, processor=lambda eid: None))
    assert "dashboard is turned off" in client.get("/dashboard/login").text
    assert login(client, password="").status_code in (303, 422)


def test_tampered_cookie_is_rejected(client):
    login(client)
    value = client.cookies.get("sf_session")
    client.cookies.clear()  # set() may add a second cookie instead of replacing the first
    client.cookies.set("sf_session", value[:-1] + ("0" if value[-1] != "0" else "1"),
                       path="/dashboard")
    assert client.get("/dashboard/tickets", follow_redirects=False).status_code == 303


def test_module_exposes_app_lazily():
    assert hasattr(main_module, "create_app")
    assert Event.__tablename__ == "events"
