"""The interface every LLM provider implements.

The pipeline only talks to this interface, so switching from a local model (Ollama)
to a cloud API is a new class plus one setting, with no pipeline changes.
"""

from dataclasses import dataclass
from typing import Any, Protocol


class LLMError(RuntimeError):
    """The provider could not produce a response (network error, timeout, bad status)."""


@dataclass
class LLMResult:
    text: str
    # Token counts when the provider reports them; used for cost tracking.
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Which model actually answered, if not the provider's own `name` (see FallbackProvider).
    model: str | None = None


class LLMProvider(Protocol):
    name: str

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        """Return the model's raw response, whose text should be JSON matching `schema`."""
        ...


class FallbackProvider:
    """Try `primary`; if it fails, ask `secondary` instead (for example a local model
    with a cloud model behind it). Bad output is not a failure here: the classifier
    already retries that with feedback."""

    def __init__(self, primary: LLMProvider, secondary: LLMProvider) -> None:
        self.primary = primary
        self.secondary = secondary
        self.name = f"{primary.name}|{secondary.name}"

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        try:
            result = self.primary.complete_json(system, user, schema)
            result.model = result.model or self.primary.name
        except LLMError:
            result = self.secondary.complete_json(system, user, schema)
            result.model = result.model or self.secondary.name
        return result
