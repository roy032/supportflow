import pytest

from app.tally import TallyPayloadError, parse_tally_payload
from tests.conftest import make_tally_payload


def test_parses_fields_by_label():
    msg = parse_tally_payload(make_tally_payload(message="Refund please", order_id="ORD-7"))
    assert msg.name == "Rahim Uddin"
    assert msg.email == "rahim@example.com"
    assert msg.order_id == "ORD-7"
    assert msg.message == "Refund please"


def test_empty_optional_fields_become_none():
    msg = parse_tally_payload(make_tally_payload(order_id=None))
    assert msg.order_id is None


def test_bangla_message_is_kept_intact():
    text = "আমার অর্ডার এখনো আসেনি"
    assert parse_tally_payload(make_tally_payload(message=text)).message == text


def test_rejects_other_event_types():
    with pytest.raises(TallyPayloadError):
        parse_tally_payload(make_tally_payload(event_type="FORM_UPDATED"))


def test_rejects_payload_without_message():
    payload = make_tally_payload()
    payload["data"]["fields"] = [f for f in payload["data"]["fields"] if f["label"] != "Message"]
    with pytest.raises(TallyPayloadError):
        parse_tally_payload(payload)
