import pytest

from app.tools.registry import load_all_tools


@pytest.fixture(scope="session", autouse=True)
def _tools_loaded():
    """Register every tool once, before any test runs."""
    load_all_tools()