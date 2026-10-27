"""Classifier and Ollama provider tests. The LLM is always faked."""

import json

import httpx
import pytest

from app.classifier import SCHEMA, ClassificationError, classify
from app.llm.base import LLMError, LLMResult
from app.llm.ollama import OllamaProvider
from app.tally import SupportMessage

VALID = {
    "category": "order_status",
    "urgency": "medium",
    "sentiment": "negative",
    "language": "en",
    "order_id": "ORD-1042",
    "summary": "Customer asks where their order is.",
}


class FakeProvider:
    """Returns queued responses in order; an Exception in the queue is raised instead."""

    name = "fake:model"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def complete_json(self, system, user, schema):
        self.prompts.append(user)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, LLMResult):
            return item
        return LLMResult(item if isinstance(item, str) else json.dumps(item))


MSG = SupportMessage(message="Where is my order ORD-1042?", order_id="ORD-1042")


def test_valid_response_on_first_try():
    outcome = classify(FakeProvider(VALID), MSG)
    assert outcome.result.category == "order_status"
    assert outcome.attempts == 1
    assert outcome.model == "fake:model"


def test_invalid_json_is_retried_with_feedback():
    provider = FakeProvider("this is not json", VALID)
    outcome = classify(provider, MSG)
    assert outcome.attempts == 2
    assert "previous answer was invalid" in provider.prompts[1]


def test_schema_violation_is_retried():
    bad = {**VALID, "category": "shipping"}  # not an allowed category
    outcome = classify(FakeProvider(bad, VALID), MSG)
    assert outcome.attempts == 2


def test_provider_error_is_retried():
    outcome = classify(FakeProvider(LLMError("timeout"), VALID), MSG)
    assert outcome.attempts == 2


def test_gives_up_after_max_attempts():
    with pytest.raises(ClassificationError) as info:
        classify(FakeProvider("nope", "still nope"), MSG)
    assert info.value.attempts == 2


def test_values_are_normalised():
    messy = {**VALID, "category": " Refund ", "urgency": "HIGH", "order_id": "null"}
    outcome = classify(FakeProvider(messy), SupportMessage(message="refund please"))
    assert outcome.result.category == "refund"
    assert outcome.result.urgency == "high"
    assert outcome.result.order_id is None


def test_form_order_id_fills_in_when_model_misses_it():
    outcome = classify(FakeProvider({**VALID, "order_id": None}), MSG)
    assert outcome.result.order_id == "ORD-1042"


def test_spam_is_always_low_urgency():
    spam = {**VALID, "category": "spam", "urgency": "high", "order_id": None}
    outcome = classify(FakeProvider(spam), SupportMessage(message="You won a free iPhone!!!"))
    assert outcome.result.urgency == "low"


def test_prompt_includes_worked_examples_with_valid_labels():
    import re

    from app.classifier import SYSTEM_PROMPT, ClassificationResult

    examples = re.findall(r"^\{.*\}$", SYSTEM_PROMPT, flags=re.MULTILINE)
    assert len(examples) >= 3
    for example in examples:
        ClassificationResult.model_validate(json.loads(example))  # examples must obey the schema


def test_ollama_provider_sends_schema_and_parses_reply():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": json.dumps(VALID)},
            "prompt_eval_count": 410,
            "eval_count": 52,
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OllamaProvider("http://ollama.test/", "qwen2.5:3b", client=client)
    result = provider.complete_json("sys", "user", SCHEMA)

    assert json.loads(result.text) == VALID
    assert (result.input_tokens, result.output_tokens) == (410, 52)
    assert seen["url"] == "http://ollama.test/api/chat"
    assert seen["body"]["model"] == "qwen2.5:3b"
    assert seen["body"]["stream"] is False
    assert seen["body"]["format"] == SCHEMA
    assert seen["body"]["options"]["temperature"] == 0


def test_ollama_http_error_becomes_llm_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    provider = OllamaProvider("http://ollama.test", "m", client=client)
    with pytest.raises(LLMError, match="HTTP 500"):
        provider.complete_json("s", "u", SCHEMA)


def test_ollama_unreachable_becomes_llm_error():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    provider = OllamaProvider("http://ollama.test", "m", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(LLMError, match="Could not reach Ollama"):
        provider.complete_json("s", "u", SCHEMA)


@pytest.mark.parametrize(
    "reply",
    [{"message": None}, {"message": {"content": None}}, {"message": "text"}, ["not", "a", "dict"]],
)
def test_ollama_malformed_reply_becomes_llm_error(reply):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply)))
    provider = OllamaProvider("http://ollama.test", "m", client=client)
    with pytest.raises(LLMError, match="Unexpected response shape"):
        provider.complete_json("s", "u", SCHEMA)


def test_ollama_malformed_reply_is_recorded_as_classification_failure():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"message": None}))
    )
    provider = OllamaProvider("http://ollama.test", "m", client=client)
    with pytest.raises(ClassificationError, match="provider error"):
        classify(provider, MSG)


def test_ollama_connect_timeout_is_short_but_read_timeout_is_configurable():
    provider = OllamaProvider("http://ollama.test", "m", timeout=300)
    timeout = provider._client.timeout
    assert timeout.read == 300
    assert timeout.connect == 10.0


def test_long_order_id_from_model_is_truncated_to_column_size():
    outcome = classify(FakeProvider({**VALID, "order_id": "X" * 500}), MSG)
    assert outcome.result.order_id == "X" * 64


def test_long_form_order_id_is_truncated_too():
    message = SupportMessage(message="where is it", order_id="Y" * 100)
    outcome = classify(FakeProvider({**VALID, "order_id": None}), message)
    assert outcome.result.order_id == "Y" * 64
