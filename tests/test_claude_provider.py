"""Claude provider, schema adaptation and provider fallback. The SDK client is faked."""

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.classifier import SCHEMA, classify
from app.config import Settings
from app.llm import FallbackProvider, get_provider
from app.llm.base import LLMError, LLMResult
from app.llm.claude import FALLBACK_BETA, AnthropicProvider, strict_schema
from app.tally import SupportMessage
from tests.test_classifier import VALID, FakeProvider


class FakeMessages:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def fake_client(outcome):
    messages = FakeMessages(outcome)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def response(text=None, stop_reason="end_turn", stop_details=None, model="claude-opus-5-5"):
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        usage=SimpleNamespace(input_tokens=900, output_tokens=60),
    )


def test_strict_schema_closes_objects_and_drops_unsupported_keywords():
    strict = strict_schema(SCHEMA)
    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == set(SCHEMA["properties"])
    blob = json.dumps(strict)
    for keyword in ("minLength", "maxLength", '"title"', '"default"'):
        assert keyword not in blob
    # order_id stays optional by being nullable.
    assert {"type": "null"} in strict["properties"]["order_id"]["anyOf"]
    assert "required" in SCHEMA and "additionalProperties" not in SCHEMA  # input untouched


def test_request_uses_structured_output_effort_and_fallbacks():
    client, messages = fake_client(response(json.dumps(VALID)))
    provider = AnthropicProvider(model="claude-opus-5-5", effort="low", client=client)
    result = provider.complete_json("sys", "user", SCHEMA)

    assert json.loads(result.text) == VALID
    assert (result.input_tokens, result.output_tokens) == (900, 60)
    assert result.model == "anthropic:claude-opus-5-5"
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["system"] == "sys"
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": strict_schema(SCHEMA)}
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert "thinking" not in call and "temperature" not in call


def test_served_by_fallback_model_is_reported():
    client, _ = fake_client(response(json.dumps(VALID), model="claude-opus-4-8"))
    result = AnthropicProvider(client=client).complete_json("s", "u", SCHEMA)
    assert result.model == "anthropic:claude-opus-4-8"


def test_refusal_is_a_provider_error():
    details = SimpleNamespace(category="cyber", explanation="...")
    client, _ = fake_client(response(None, stop_reason="refusal", stop_details=details))
    with pytest.raises(LLMError, match="declined.*cyber"):
        AnthropicProvider(client=client).complete_json("s", "u", SCHEMA)


def test_truncated_output_is_a_provider_error():
    client, _ = fake_client(response('{"category": "ref', stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="max_tokens"):
        AnthropicProvider(client=client).complete_json("s", "u", SCHEMA)


def _status_error(cls, status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("error", "match"),
    [
        (lambda: _status_error(anthropic.RateLimitError, 429), "rate limit"),
        (lambda: _status_error(anthropic.AuthenticationError, 401), "ANTHROPIC_API_KEY"),
        (lambda: _status_error(anthropic.InternalServerError, 500), "HTTP 500"),
        (
            lambda: anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://api.anthropic.com")
            ),
            "Could not reach",
        ),
        (
            lambda: anthropic.APITimeoutError(
                request=httpx.Request("POST", "https://api.anthropic.com")
            ),
            "timed out",
        ),
    ],
)
def test_sdk_errors_become_provider_errors(error, match):
    try:
        exc = error()
    except TypeError as construct_error:  # pragma: no cover - SDK signature changed
        pytest.skip(f"cannot build SDK error: {construct_error}")
    client, _ = fake_client(exc)
    with pytest.raises(LLMError, match=match):
        AnthropicProvider(client=client).complete_json("s", "u", SCHEMA)


def test_fallback_provider_uses_secondary_when_primary_fails():
    primary = FakeProvider(LLMError("ollama down"))
    secondary = FakeProvider(LLMResult(json.dumps(VALID), input_tokens=5, output_tokens=7))
    secondary.name = "anthropic:claude-opus-5-5"
    outcome = classify(FallbackProvider(primary, secondary), SupportMessage(message="hi"))
    assert outcome.model == "anthropic:claude-opus-5-5"
    assert (outcome.input_tokens, outcome.output_tokens) == (5, 7)


def test_fallback_provider_prefers_primary():
    primary = FakeProvider(VALID)
    outcome = classify(FallbackProvider(primary, FakeProvider()), SupportMessage(message="hi"))
    assert outcome.model == "fake:model"


def test_tokens_are_summed_across_attempts():
    provider = FakeProvider(
        LLMResult("not json", input_tokens=100, output_tokens=10),
        LLMResult(json.dumps(VALID), input_tokens=120, output_tokens=20),
    )
    outcome = classify(provider, SupportMessage(message="hi"))
    assert (outcome.attempts, outcome.input_tokens, outcome.output_tokens) == (2, 220, 30)


def test_settings_build_the_configured_providers():
    settings = Settings(
        _env_file=None, llm_provider="ollama", llm_fallback_provider="anthropic",
        anthropic_api_key="sk-test", job_lease_seconds=600,
    )
    provider = get_provider(settings)
    assert isinstance(provider, FallbackProvider)
    assert provider.name == "ollama:qwen2.5:3b|anthropic:claude-opus-5-5"


def test_fallback_doubles_the_minimum_lease():
    with pytest.raises(ValueError, match="JOB_LEASE_SECONDS"):
        Settings(_env_file=None, llm_fallback_provider="anthropic", job_lease_seconds=500)
