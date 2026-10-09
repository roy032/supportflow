"""Local LLM through Ollama's chat API, with JSON-schema constrained output."""

from typing import Any

import httpx

from app.llm.base import LLMError

# Connecting should be quick even when generation is slow; an unreachable host
# should fail in seconds, not after the full generation timeout.
CONNECT_TIMEOUT_SECONDS = 10.0


class OllamaProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT_SECONDS))
        )

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            # Ollama constrains generation to this JSON schema.
            "format": schema,
            "options": {"temperature": 0},
        }
        try:
            response = self._client.post(f"{self.base_url}/api/chat", json=body)
            response.raise_for_status()
            content = response.json()["message"]["content"]
        except httpx.TimeoutException as exc:
            raise LLMError(f"Ollama timed out: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"Ollama returned HTTP {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise LLMError(f"Could not reach Ollama at {self.base_url}: {exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMError(f"Unexpected response shape from Ollama: {exc!r}") from exc
        if not isinstance(content, str):
            raise LLMError(
                f"Unexpected response shape from Ollama: content is {type(content).__name__}"
            )
        return content
