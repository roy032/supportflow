from app.config import Settings
from app.llm.base import LLMError, LLMProvider


def get_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "ollama":
        from app.llm.ollama import OllamaProvider

        return OllamaProvider(
            settings.ollama_url, settings.ollama_model, timeout=settings.llm_timeout_seconds
        )
    raise ValueError(f"unknown LLM_PROVIDER: {settings.llm_provider!r}")


__all__ = ["LLMError", "LLMProvider", "get_provider"]
