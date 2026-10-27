from app.config import Settings
from app.llm.base import FallbackProvider, LLMError, LLMProvider, LLMResult


def _build(name: str, settings: Settings) -> LLMProvider:
    if name == "ollama":
        from app.llm.ollama import OllamaProvider

        return OllamaProvider(
            settings.ollama_url, settings.ollama_model, timeout=settings.llm_timeout_seconds
        )
    if name == "anthropic":
        from app.llm.claude import AnthropicProvider

        return AnthropicProvider(
            model=settings.anthropic_model,
            api_key=settings.anthropic_api_key,
            effort=settings.anthropic_effort,
            timeout=settings.llm_timeout_seconds,
        )
    raise ValueError(f"unknown LLM provider: {name!r}")


def get_provider(settings: Settings) -> LLMProvider:
    provider = _build(settings.llm_provider, settings)
    if settings.llm_fallback_provider and settings.llm_fallback_provider != settings.llm_provider:
        provider = FallbackProvider(provider, _build(settings.llm_fallback_provider, settings))
    return provider


__all__ = ["FallbackProvider", "LLMError", "LLMProvider", "LLMResult", "get_provider"]
