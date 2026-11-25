from functools import lru_cache

from pydantic import SecretStr

from app.core.llm.base import LLMError, LLMProvider
from app.core.llm.fallback import FallbackTurn, GeminiCooldown
from app.core.sites import current_site


def get_llm() -> LLMProvider:
    """Return the active site's chat provider."""
    site = current_site()
    model = site.groq_fallback_model.strip()
    primary = _build_llm(site.name, site.llm_provider, site.llm_model,
                         site.gemini_api_key, bool(model))
    if not model:
        return primary
    return FallbackTurn(primary, _groq(site.groq_api_key, model),
                        _cooldown(site.gemini_api_key, site.llm_model))


@lru_cache(maxsize=32)
def _build_llm(site_name: str, provider: str, model: str, api_key: SecretStr,
               fail_fast: bool = False) -> LLMProvider:
    if provider == "gemini":
        from app.core.llm.gemini import GeminiProvider
        if fail_fast:
            return GeminiProvider(api_key=api_key.get_secret_value(), model=model, fail_fast=True)
        return GeminiProvider(api_key=api_key.get_secret_value(), model=model)
    raise LLMError(f"Unknown LLM provider '{provider}'. Supported: gemini")


@lru_cache(maxsize=32)
def _groq(api_key: SecretStr, model: str) -> LLMProvider:
    from app.core.llm.chat_completions import GroqProvider
    return GroqProvider(api_key.get_secret_value(), model)


@lru_cache(maxsize=32)
def _cooldown(api_key: SecretStr, model: str) -> GeminiCooldown:
    return GeminiCooldown()
