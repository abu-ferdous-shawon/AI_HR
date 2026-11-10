import pytest

from app.core.frappe_client import FrappeError
from app.core.security import AccessDenied, ToolInputError, UserContext
from app.tools import registry

CTX = UserContext("u@x.com", "HR-EMP-00001")


@pytest.fixture
def add_tool(monkeypatch):
    """Register a temporary tool that disappears after the test."""
    def _add(name, func, props=None, allow=frozenset()):
        params = {"type": "object", "properties": props or {}}
        monkeypatch.setitem(registry._TOOLS, name,
                            registry.Tool(name, "test tool", params, func, allow))
    return _add


def test_unknown_tool_is_refused():
    assert "error" in registry.run_tool("run_sql", {"query": "x"}, CTX)


def test_arguments_the_tool_did_not_declare_are_dropped(add_tool):
    add_tool("t", lambda ctx: {"ok": True})  # takes no inputs at all
    res = registry.run_tool("t", {"employee_id": "HR-EMP-00002"}, CTX)
    assert res == {"result": {"ok": True}}


def test_results_are_scrubbed(add_tool):
    add_tool("t", lambda ctx: {"n": 1, "cell_number": "123"})
    assert registry.run_tool("t", {}, CTX) == {"result": {"n": 1}}


def test_access_denied_and_input_errors_pass_their_message_on(add_tool):
    def denied(ctx): raise AccessDenied("not yours")
    def bad(ctx): raise ToolInputError("use YYYY-MM-DD")
    add_tool("d", denied)
    add_tool("b", bad)
    assert registry.run_tool("d", {}, CTX) == {"error": "not yours"}
    assert registry.run_tool("b", {}, CTX) == {"error": "use YYYY-MM-DD"}


def test_internal_details_never_reach_the_model(add_tool):
    def frappe_fails(ctx): raise FrappeError(500, "SELECT secret FROM tabX")
    def crashes(ctx): raise RuntimeError("password=hunter2")
    add_tool("f", frappe_fails)
    add_tool("c", crashes)
    for name in ("f", "c"):
        text = registry.run_tool(name, {}, CTX)["error"]
        assert "SELECT" not in text and "hunter2" not in text