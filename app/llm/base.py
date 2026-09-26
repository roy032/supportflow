"""The interface every LLM provider implements.

The pipeline only talks to this interface, so switching from a local model (Ollama)
to a cloud API is a new class plus one setting, with no pipeline changes.
"""

from typing import Any, Protocol


class LLMError(RuntimeError):
    """The provider could not produce a response (network error, timeout, bad status)."""


class LLMProvider(Protocol):
    name: str

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> str:
        """Return the model's raw text response, which should be JSON matching `schema`."""
        ...
