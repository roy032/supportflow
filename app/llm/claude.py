"""Claude through the Anthropic API, with JSON-schema structured output."""

from copy import deepcopy
from typing import Any

import anthropic

from app.llm.base import LLMError, LLMResult

# Keywords the structured-output API does not accept. The classifier validates the
# full schema (lengths included) with Pydantic afterwards, so dropping them here is safe.
_UNSUPPORTED_KEYWORDS = {
    "title", "default", "minLength", "maxLength", "minimum", "maximum", "multipleOf",
}

# Re-runs a request that a safety classifier declined on Anthropic's recommended
# fallback model, inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt a Pydantic JSON schema to what structured outputs require: every object
    closed (`additionalProperties: false`) with all of its properties required, and
    no unsupported constraint keywords. Optional fields stay nullable via `anyOf`."""

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYWORDS}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out

    return walk(deepcopy(schema))


class AnthropicProvider:
    def __init__(
        self,
        model: str = "claude-opus-5-5",
        api_key: str | None = None,
        effort: str = "low",
        timeout: float = 120.0,
        max_tokens: int = 2048,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        self.name = f"anthropic:{model}"
        self.effort = effort
        self.max_tokens = max_tokens
        # No SDK-level retries: each call must end within `timeout` so the job lease
        # (sized from LLM_TIMEOUT_SECONDS) holds. The classifier retries once, and the
        # job queue retries whole runs with backoff, which also covers 429s and 5xx.
        self._client = client or anthropic.Anthropic(
            api_key=api_key or None, timeout=timeout, max_retries=0
        )

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        try:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                # Classification needs little reasoning; low effort keeps it fast and cheap.
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": strict_schema(schema)},
                },
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.APITimeoutError as exc:
            raise LLMError(f"Anthropic API timed out: {exc}") from exc
        except anthropic.AuthenticationError as exc:
            raise LLMError(f"Anthropic API rejected the key (check ANTHROPIC_API_KEY): {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(f"Anthropic API rate limit: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API returned HTTP {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc

        usage = response.usage
        tokens = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise LLMError(f"Claude declined to classify the message (category: {category})")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"Claude ran out of output tokens (max_tokens={self.max_tokens})")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("Claude returned no text block")
        # With a fallback, response.model names the model that actually answered.
        return LLMResult(text=text, model=f"anthropic:{response.model}", **tokens)
