import pytest
from app.config import settings

from app.tools.registry import load_all_tools


@pytest.fixture(scope="session", autouse=True)
def _tools_loaded():
    """Register every tool once, before any test runs."""
    load_all_tools()

@pytest.fixture(autouse=True)
def _service_mode_by_default(monkeypatch):
    """Most tests have no ERP login. Tests of the auth modes set the mode themselves."""
    monkeypatch.setattr(settings, "frappe_auth_mode", "service_account")