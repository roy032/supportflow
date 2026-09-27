"""Classify a support message with an LLM into validated, structured fields."""

import json
import time
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.llm.base import LLMError, LLMProvider
from app.tally import SupportMessage

Category = Literal["refund", "order_status", "complaint", "spam", "other"]
Urgency = Literal["low", "medium", "high"]
Sentiment = Literal["positive", "neutral", "negative"]
Language = Literal["bn", "en", "mixed"]


class ClassificationResult(BaseModel):
    category: Category
    urgency: Urgency
    sentiment: Sentiment
    language: Language
    order_id: str | None = None
    summary: str = Field(min_length=1, max_length=300)

    @field_validator("category", "urgency", "sentiment", "language", mode="before")
    @classmethod
    def _lowercase(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("order_id", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in {"", "null", "none", "n/a"}:
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("summary", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


SCHEMA = ClassificationResult.model_json_schema()

SYSTEM_PROMPT = """You triage customer support messages for an online shop.
Return ONLY a JSON object with these fields:

- category: one of
    refund        the customer wants money back or to return an item
    order_status  the customer asks where an order is or when it will arrive
    complaint     the customer is unhappy with a product, delivery or service
    spam          advertising, prizes, links to click, or anything that is not a real
                  customer writing about their own order or account
    other         a genuine customer request that fits none of the above
- urgency: "high" only if the customer is angry, mentions a deadline, or asks for money
  back urgently; "medium" for normal problems such as a late order; "low" for general
  questions. Spam is always "low".
- sentiment: "positive", "neutral" or "negative"
- language: "bn" for Bangla, "en" for English, "mixed" for Bangla and English together
  (including Bangla written in English letters)
- order_id: the order number if one is mentioned, otherwise null
- summary: one short English sentence describing what the customer needs

Messages may be written in Bangla, English, or both. Always write the summary in English.

Examples:

Message: You have been selected for a $500 gift card! Claim it here: bit.ly/xyz
{"category": "spam", "urgency": "low", "sentiment": "neutral", "language": "en", "order_id": null, "summary": "Promotional spam, not a customer request."}

Message: My order ORD-55 was supposed to come on Monday, it's Thursday now. Any update?
{"category": "order_status", "urgency": "medium", "sentiment": "neutral", "language": "en", "order_id": "ORD-55", "summary": "Customer asks for an update on a late order."}

Message: Apnader product ta ekdom baje, ami khub hotash. Keu reply o dey na!
{"category": "complaint", "urgency": "high", "sentiment": "negative", "language": "mixed", "order_id": null, "summary": "Angry customer says the product is poor and nobody replies."}"""


def build_user_prompt(message: SupportMessage) -> str:
    parts = [f"Message:\n{message.message}"]
    if message.order_id:
        parts.append(f"Order ID from the form: {message.order_id}")
    return "\n\n".join(parts)


class ClassificationError(RuntimeError):
    def __init__(self, message: str, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


@dataclass
class ClassificationOutcome:
    result: ClassificationResult
    attempts: int
    latency_ms: int
    model: str


def classify(
    provider: LLMProvider, message: SupportMessage, max_attempts: int = 2
) -> ClassificationOutcome:
    """Ask the model to classify `message`.

    Invalid JSON, schema violations and provider errors are retried up to `max_attempts`
    times in total. On a retry after bad output, the model is told what was wrong.
    """
    user_prompt = build_user_prompt(message)
    last_error = "no attempts made"
    started = time.perf_counter()

    for attempt in range(1, max_attempts + 1):
        prompt = user_prompt
        if attempt > 1:
            prompt += (
                f"\n\nYour previous answer was invalid ({last_error}). "
                "Reply with a single JSON object that follows the rules exactly."
            )
        try:
            raw = provider.complete_json(SYSTEM_PROMPT, prompt, SCHEMA)
            result = ClassificationResult.model_validate(json.loads(raw))
        except LLMError as exc:
            last_error = f"provider error: {exc}"
            continue
        except json.JSONDecodeError as exc:
            last_error = f"not valid JSON: {exc.msg}"
            continue
        except ValidationError as exc:
            first = exc.errors()[0]
            field = ".".join(str(p) for p in first["loc"]) or "response"
            last_error = f"field '{field}': {first['msg']}"
            continue

        # The form's order ID is more reliable than the model's reading of free text.
        if result.order_id is None and message.order_id:
            result.order_id = message.order_id
        # Business rule, enforced in code rather than trusted to the model:
        # spam never goes to the top of the queue.
        if result.category == "spam":
            result.urgency = "low"

        return ClassificationOutcome(
            result=result,
            attempts=attempt,
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=provider.name,
        )

    raise ClassificationError(
        f"classification failed after {max_attempts} attempts: {last_error}", max_attempts
    )
