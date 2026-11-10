from functools import lru_cache

from app.config import settings
from app.core.llm.base import LLMError, LLMProvider


@lru_cache
def get_llm() -> LLMProvider:
    """Return the provider chosen by LLM_PROVIDER in .env."""
    name = settings.llm_provider.lower()
    if name == "gemini":
        from app.core.llm.gemini import GeminiProvider
        return GeminiProvider()
    raise LLMError(f"Unknown LLM_PROVIDER '{name}'. Supported: gemini")