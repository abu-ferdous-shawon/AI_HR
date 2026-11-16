import pytest

from app.tools.registry import load_all_tools


@pytest.fixture(scope="session", autouse=True)
def _tools_loaded():
    """Register every tool once, before any test runs."""
    load_all_tools()


@pytest.fixture(autouse=True)
def _fresh_limiters(monkeypatch):
    """Every test starts with generous, empty rate limits."""
    from app.core import gateway
    from app.core.ratelimit import RateLimiter
    monkeypatch.setattr(gateway, "chat_limiter", RateLimiter(1000))
    monkeypatch.setattr(gateway, "auth_failures", RateLimiter(1000))


@pytest.fixture(autouse=True)
def _service_account_by_default(monkeypatch):
    """Tests that fake the login send no headers. The ones that care set user_session."""
    from app.config import settings
    monkeypatch.setattr(settings, "frappe_auth_mode", "service_account")