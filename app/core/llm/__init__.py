from functools import lru_cache

from pydantic import SecretStr

from app.core.llm.base import LLMError, LLMProvider
from app.core.sites import current_site


def get_llm() -> LLMProvider:
    """Return the active site's chat provider."""
    site = current_site()
    return _build_llm(site.name, site.llm_provider, site.llm_model, site.gemini_api_key)


@lru_cache(maxsize=32)
def _build_llm(site_name: str, provider: str, model: str, api_key: SecretStr) -> LLMProvider:
    if provider == "gemini":
        from app.core.llm.gemini import GeminiProvider
        return GeminiProvider(api_key=api_key.get_secret_value(), model=model)
    raise LLMError(f"Unknown LLM provider '{provider}'. Supported: gemini")
