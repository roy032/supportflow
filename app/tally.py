"""Turn a Tally webhook payload into a clean support message.

Tally sends every form field as {key, label, type, value}. We match fields by label
so the form can be edited in Tally without code changes, as long as the labels still
contain the expected words.
"""

from typing import Any

from pydantic import BaseModel


class SupportMessage(BaseModel):
    name: str | None = None
    email: str | None = None
    order_id: str | None = None
    message: str


class TallyPayloadError(ValueError):
    """The payload is not a usable Tally form response."""


# label keyword -> SupportMessage field. First match wins, checked in this order.
_LABEL_KEYWORDS: list[tuple[str, str]] = [
    ("email", "email"),
    ("order", "order_id"),
    ("name", "name"),
    ("message", "message"),
    ("question", "message"),
    ("issue", "message"),
    ("describe", "message"),
]


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value if v is not None)
    text = str(value).strip()
    return text or None


def parse_tally_payload(payload: dict[str, Any]) -> SupportMessage:
    if payload.get("eventType") != "FORM_RESPONSE":
        raise TallyPayloadError(f"unsupported eventType: {payload.get('eventType')!r}")

    fields = (payload.get("data") or {}).get("fields")
    if not isinstance(fields, list):
        raise TallyPayloadError("payload has no data.fields list")

    found: dict[str, str] = {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        label = str(field.get("label") or "").lower()
        text = _as_text(field.get("value"))
        if text is None:
            continue
        for keyword, target in _LABEL_KEYWORDS:
            if keyword in label and target not in found:
                found[target] = text
                break

    if "message" not in found:
        raise TallyPayloadError("no message field found (label should contain 'message')")

    return SupportMessage(**found)
