"""Helpdesk: knowledge search, reply drafts, tickets, email replies and SLA escalation."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.drafting import DraftError, write_draft
from app.escalation import escalate_overdue
from app.helpdesk import HelpdeskError, TicketQuery, list_tickets, send_reply, set_status
from app.jobs import JobRunner, Worker
from app.knowledge import KnowledgeBase, Section, split_sections
from app.mailer import MailError
from app.models import Classification, Event, Failure, Reply, ReplyStatus, TicketStatus
from app.pipeline import Drafter, PipelineOptions, process_event
from app.slack import SlackError
from tests.conftest import make_tally_payload
from tests.test_classifier import VALID, FakeProvider
from tests.test_pipeline import FakeNotifier

KB = KnowledgeBase([
    Section("refunds.md", "Damaged or wrong items",
            "Send a photo within 48 hours. We offer a free replacement or a full refund."),
    Section("shipping.md", "Delivery times", "Inside Dhaka: 1 to 2 working days."),
    Section("payments.md", "Payment methods", "We accept bKash, Nagad and cards."),
])
DRAFT = {"reply": "Sorry about that! Please send a photo and we'll refund you.",
         "needs_human": False, "reason": ""}


class FakeMailer:
    def __init__(self, error: Exception | None = None):
        self.sent = []
        self.error = error

    def send(self, to, subject, body, reference=None):
        if self.error:
            raise self.error
        self.sent.append({"to": to, "subject": subject, "body": body, "reference": reference})
        return "<id@test>"


def make_ticket(session_factory, *, category="refund", urgency="high", minutes_ago=0,
                email="rahim@example.com", status=TicketStatus.OPEN, draft=None, now=None):
    payload = make_tally_payload()
    created = (now or datetime.now(UTC)) - timedelta(minutes=minutes_ago)
    with session_factory() as s:
        event = Event(external_id=payload["eventId"], payload=payload, message="It arrived broken",
                      customer_name="Rahim", customer_email=email, status="notified",
                      ticket_status=status, created_at=created)
        event.classification = Classification(
            category=category, urgency=urgency, sentiment="negative", language="en",
            order_id="ORD-9", summary="Item arrived damaged.", model="fake", latency_ms=1)
        if draft:
            event.replies.append(Reply(status=ReplyStatus.DRAFT, body=draft, author="ai"))
        s.add(event)
        s.commit()
        return event.id


def get(session_factory, event_id) -> Event:
    with session_factory() as s:
        event = s.get(Event, event_id)
        _ = event.replies, event.classification
        return event


# knowledge


def test_sections_are_split_on_level_two_headings():
    md = "# Title\nintro is ignored\n## One\nfirst\n## Empty\n\n## Two\nsecond\nmore"
    assert [(s.title, s.text) for s in split_sections("f.md", md)] == [
        ("One", "first"), ("Two", "second\nmore")
    ]


def test_search_ranks_by_shared_words_and_handles_suffixes():
    found = KB.search("my parcel arrived damaged, I want a refund")
    assert found[0].title == "Damaged or wrong items"
    assert KB.search("refunded?")[0].title == "Damaged or wrong items"
    assert KB.search("zzz qqq") == []


def test_bundled_knowledge_loads(tmp_path):
    kb = KnowledgeBase.load("knowledge")
    assert len(kb.sections) >= 8
    assert all(s.source != "README.md" for s in kb.sections)
    assert KnowledgeBase.load(tmp_path / "missing").sections == []


# drafting


def draft_for(provider, kb=KB, message="The blender arrived cracked"):
    return write_draft(provider, kb, message=message, summary="Item arrived damaged; wants refund.",
                       category="refund", language="bn", customer_name="Rahim",
                       order_id="ORD-9", shop_name="Test Shop")


def test_draft_uses_policies_and_customer_language():
    provider = FakeProvider(DRAFT)
    draft = draft_for(provider)
    assert draft.reply.startswith("Sorry")
    assert draft.needs_human is False
    assert "refunds.md / Damaged or wrong items" in draft.sources
    assert "Order ID: ORD-9" in provider.prompts[0]


def test_bangla_message_finds_english_policies_through_the_summary():
    draft = draft_for(FakeProvider(DRAFT), message="জিনিসটা ভাঙা এসেছে")
    assert draft.sources and draft.sources[0].startswith("refunds.md")


def test_draft_without_matching_policy_always_needs_a_person():
    draft = draft_for(FakeProvider(DRAFT), kb=KnowledgeBase([]))
    assert draft.needs_human is True
    assert draft.reason == "no matching policy found"


def test_invalid_draft_is_retried_then_fails():
    assert draft_for(FakeProvider("nope", DRAFT)).reply.startswith("Sorry")
    with pytest.raises(DraftError):
        draft_for(FakeProvider("nope", {"reply": ""}))


# pipeline


def stored_event(session_factory, message="My blender arrived cracked, refund please") -> int:
    payload = make_tally_payload(message=message)
    with session_factory() as s:
        event = Event(external_id=payload["eventId"], payload=payload, message=message,
                      customer_email="rahim@example.com")
        s.add(event)
        s.commit()
        return event.id


def test_pipeline_drafts_a_reply_and_puts_it_in_slack(session_factory):
    event_id = stored_event(session_factory)
    notifier = FakeNotifier()
    drafter = Drafter(FakeProvider(DRAFT), KB, "Test Shop")
    options = PipelineOptions(slack_buttons=True, public_url="https://support.test")
    process_event(event_id, session_factory, FakeProvider(VALID), notifier, drafter, options)

    event = get(session_factory, event_id)
    assert event.status == "notified"
    assert event.draft().body == DRAFT["reply"]
    assert "source: refunds.md" in event.draft().note
    blob = json.dumps(notifier.sent[0])
    assert "Suggested reply" in blob and "send_draft" in blob and "resolve" in blob
    assert f"https://support.test/dashboard/tickets/{event_id}" in blob


def test_spam_is_resolved_and_never_drafted(session_factory):
    event_id = stored_event(session_factory, "You won a prize!")
    spam = {**VALID, "category": "spam"}
    process_event(event_id, session_factory, FakeProvider(spam), FakeNotifier(),
                  Drafter(FakeProvider(), KB, "Shop"))  # drafting would raise
    event = get(session_factory, event_id)
    assert event.ticket_status == TicketStatus.RESOLVED
    assert event.resolved_at is not None
    assert event.replies == []


def test_failed_draft_does_not_block_slack(session_factory):
    event_id = stored_event(session_factory)
    notifier = FakeNotifier()
    process_event(event_id, session_factory, FakeProvider(VALID), notifier,
                  Drafter(FakeProvider("bad", "bad"), KB, "Shop"))
    assert get(session_factory, event_id).status == "notified"
    assert "Suggested reply" not in json.dumps(notifier.sent[0])
    with session_factory() as s:
        assert s.query(Failure).filter_by(stage="draft").count() == 1


def test_draft_is_not_retried_when_slack_is_retried(session_factory):
    event_id = stored_event(session_factory)
    drafter = Drafter(FakeProvider(DRAFT), KB, "Shop")
    process_event(event_id, session_factory, FakeProvider(VALID),
                  FakeNotifier(SlackError("down", 3)), drafter)
    # Second run: an empty FakeProvider would raise if a second draft were requested.
    process_event(event_id, session_factory, FakeProvider(), FakeNotifier(),
                  Drafter(FakeProvider(), KB, "Shop"))
    event = get(session_factory, event_id)
    assert event.status == "notified" and len(event.replies) == 1


# tickets and replies


def test_sending_the_draft_unchanged_marks_it_sent(session_factory):
    ticket = make_ticket(session_factory, draft="Hello from the shop")
    mailer = FakeMailer()
    with session_factory() as s:
        event = s.get(Event, ticket)
        send_reply(s, event, " Hello from the shop ", "Ayesha", mailer, "Test Shop")
    event = get(session_factory, ticket)
    assert [(r.status, r.author) for r in event.replies] == [("sent", "Ayesha")]
    assert event.ticket_status == TicketStatus.PENDING
    assert event.first_response_at is not None
    assert mailer.sent[0]["to"] == "rahim@example.com"
    assert mailer.sent[0]["subject"] == "Re: your order ORD-9"


def test_an_edited_reply_discards_the_draft_and_can_resolve(session_factory):
    ticket = make_ticket(session_factory, draft="AI text")
    with session_factory() as s:
        send_reply(s, s.get(Event, ticket), "My own text", "Ayesha", FakeMailer(), "Shop",
                   resolve=True)
    event = get(session_factory, ticket)
    assert [(r.status, r.body) for r in event.replies] == [
        ("discarded", "AI text"), ("sent", "My own text")
    ]
    assert event.ticket_status == TicketStatus.RESOLVED and event.resolved_at is not None


def test_a_failed_send_is_recorded_and_raised(session_factory):
    ticket = make_ticket(session_factory)
    with session_factory() as s, pytest.raises(HelpdeskError, match="smtp down"):
        send_reply(s, s.get(Event, ticket), "Hi", "A", FakeMailer(MailError("smtp down")), "Shop")
    event = get(session_factory, ticket)
    assert event.replies[0].status == ReplyStatus.FAILED
    assert event.ticket_status == TicketStatus.OPEN and event.first_response_at is None


@pytest.mark.parametrize(
    ("email", "mailer", "match"),
    [(None, FakeMailer(), "no email"), ("a@b.c", None, "not configured")],
)
def test_reply_preconditions(session_factory, email, mailer, match):
    ticket = make_ticket(session_factory, email=email)
    with session_factory() as s, pytest.raises(HelpdeskError, match=match):
        send_reply(s, s.get(Event, ticket), "Hi", "A", mailer, "Shop")


def test_reopening_clears_resolved_at():
    event = Event(ticket_status=TicketStatus.OPEN)
    set_status(event, TicketStatus.RESOLVED)
    assert event.resolved_at is not None
    set_status(event, TicketStatus.OPEN)
    assert event.resolved_at is None
    with pytest.raises(HelpdeskError):
        set_status(event, "closed")


def test_inbox_lists_urgent_first_then_oldest(session_factory):
    low = make_ticket(session_factory, urgency="low", minutes_ago=50)
    high_new = make_ticket(session_factory, urgency="high", minutes_ago=1)
    high_old = make_ticket(session_factory, urgency="high", minutes_ago=30)
    make_ticket(session_factory, status=TicketStatus.RESOLVED)
    with session_factory() as s:
        rows, total = list_tickets(s, TicketQuery())
        assert [e.id for e in rows] == [high_old, high_new, low] and total == 3
        rows, total = list_tickets(s, TicketQuery(status=None, urgency="low"))
        assert [e.id for e in rows] == [low]
        rows, _ = list_tickets(s, TicketQuery(search="ord-9"))
        assert len(rows) == 3


# SLA escalation


def test_overdue_tickets_are_escalated_once(session_factory):
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    overdue = make_ticket(session_factory, urgency="high", minutes_ago=90, now=now)
    make_ticket(session_factory, urgency="high", minutes_ago=10, now=now)  # within SLA
    make_ticket(session_factory, urgency="low", minutes_ago=900, now=now)  # no SLA for low
    sla = {"high": timedelta(minutes=60)}
    notifier = FakeNotifier()

    assert escalate_overdue(session_factory, notifier, sla, "https://s.test", lambda: now) == 1
    assert escalate_overdue(session_factory, notifier, sla, "https://s.test", lambda: now) == 0
    assert f"#{overdue} has had no reply for 90 minutes" in notifier.sent[0]["text"]
    assert get(session_factory, overdue).escalated_at is not None


def test_failed_escalation_is_retried_next_round(session_factory):
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    ticket = make_ticket(session_factory, minutes_ago=90, now=now)
    sla = {"high": timedelta(minutes=60)}
    assert escalate_overdue(session_factory, FakeNotifier(SlackError("x", 1)), sla,
                            now=lambda: now) == 0
    assert get(session_factory, ticket).escalated_at is None
    assert escalate_overdue(session_factory, FakeNotifier(), sla, now=lambda: now) == 1


def test_answered_tickets_are_not_escalated(session_factory):
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    ticket = make_ticket(session_factory, minutes_ago=90, now=now)
    with session_factory() as s:
        s.get(Event, ticket).first_response_at = now
        s.commit()
    assert escalate_overdue(session_factory, FakeNotifier(), {"high": timedelta(minutes=60)},
                            now=lambda: now) == 0


def test_worker_runs_periodic_tasks_on_their_interval(session_factory):
    clock = [0.0]
    calls = []
    worker = Worker(JobRunner(session_factory, lambda eid: None), periodic=[(60, lambda: calls.append(1))],
                    clock=lambda: clock[0])
    worker.run_periodic()
    worker.run_periodic()
    clock[0] = 61
    worker.run_periodic()
    assert calls == [1, 1]
