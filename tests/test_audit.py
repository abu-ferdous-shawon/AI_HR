import json
import logging
import os

import pytest

from app.core import agent, audit
from app.core.agent import run_agent
from app.core.frappe_client import FrappeError
from app.core.llm.base import LLMProvider, LLMResponse, ToolCall
from app.core.security import AccessDenied, ToolInputError, UserContext
from app.tools import registry

CTX = UserContext("u@x.com", "HR-EMP-00001")


@pytest.fixture
def log_file(tmp_path):
    path = tmp_path / "logs" / "audit.jsonl"
    audit.configure(path)
    yield path
    audit.close()


def lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_safe_args_keep_plain_filters_and_hide_free_text():
    out = audit.safe_args({"record_type": "leave", "from_date": "2026-01-01", "limit": 5,
                           "scope": "team", "include_weekly_off": True,
                           "employee": "Rahim Uddin", "query": "my salary"})
    assert out["record_type"] == "leave" and out["limit"] == 5
    assert out["include_weekly_off"] is True
    assert out["employee"].startswith("<hidden") and out["query"].startswith("<hidden")
    assert "Rahim" not in str(out) and "salary" not in str(out)


def test_safe_args_hide_numbers_and_long_text_even_in_allowed_keys():
    assert audit.safe_args({"status": "01711234567"})["status"].startswith("<hidden")
    assert audit.safe_args({"status": "x" * 200})["status"].startswith("<hidden")
    assert audit.safe_args({"status": ["a"]})["status"] == "<hidden>"


def test_describe_result_gives_counts_not_values():
    assert audit.describe_result({
        "outcome": "records", "count": 3, "truncated": True, "scope": "team",
        "record_type": "leave", "employee": {"id": "E2", "name": "Rahim"},
        "records": [{"a": 1}]}) == {
            "rows": 3, "outcome": "records", "truncated": True, "scope": "team",
            "record_type": "leave", "target": "E2"}
    assert audit.describe_result({"records_counted": 12})["rows"] == 12
    assert audit.describe_result({"payslips": [1, 2]})["rows"] == 2
    assert audit.describe_result([{"name": "a"}]) == {"rows": 1}
    assert audit.describe_result({"file": {"rows": 40}})["rows"] == 40
    assert audit.describe_result({"employee_id": "E9", "records": []}) == {"rows": 0, "target": "E9"}
    assert audit.describe_result({"outcome": "no_records"}) == {"rows": 0, "outcome": "no_records"}
    assert audit.describe_result("text") == {}


def test_tool_entry_drops_error_text_and_names():
    entry = audit.tool_entry({"tool": "t", "args": {"employee": "Rahim"}, "ok": False,
                              "error": "'Rahim' is not one of your direct reports",
                              "kind": "denied"})
    assert "Rahim" not in json.dumps(entry) and entry["kind"] == "denied"


def test_record_writes_one_json_line(log_file):
    audit.record("chat", user="u@x.com", status="ok")
    row = lines(log_file)[0]
    assert row["event"] == "chat" and row["user"] == "u@x.com" and "time" in row


@pytest.mark.skipif(os.name == "nt", reason="file modes work differently on Windows")
def test_the_log_file_is_private(log_file):
    audit.record("x")
    assert log_file.stat().st_mode & 0o777 == 0o600


def test_the_log_rotates(tmp_path):
    path = tmp_path / "a.jsonl"
    audit.configure(path, max_bytes=300, backups=2)
    try:
        for i in range(30):
            audit.record("chat", n=i, pad="x" * 50)
    finally:
        audit.close()
    assert (tmp_path / "a.jsonl.1").exists() and not (tmp_path / "a.jsonl.3").exists()


def test_json_formatter_makes_valid_json():
    rec = logging.LogRecord("n", logging.WARNING, "f", 1, "hello %s", ("x",), None)
    data = json.loads(audit.JsonFormatter().format(rec))
    assert data["message"] == "hello x" and data["level"] == "WARNING"


# ---- how each tool call ended ----

@pytest.fixture
def add_tool(monkeypatch):
    def _add(name, func, hr_only=False):
        monkeypatch.setitem(registry._TOOLS, name, registry.Tool(
            name, "t", {"type": "object", "properties": {}}, func, frozenset(), hr_only))
    return _add


def test_every_ending_is_labelled_and_read_only_once(add_tool, monkeypatch):
    def denied(ctx): raise AccessDenied("no")
    def invalid(ctx): raise ToolInputError("bad")
    def failed(ctx): raise FrappeError(500, "x")
    def crash(ctx): raise RuntimeError("x")
    for name, func in {"ok": lambda ctx: {"a": 1}, "denied": denied, "invalid": invalid,
                       "failed": failed, "crash": crash}.items():
        add_tool(name, func)
    monkeypatch.setattr(registry, "is_hr_admin", lambda ctx: False)
    add_tool("hr", lambda ctx: {}, hr_only=True)

    expected = {"ok": "ok", "denied": "denied", "invalid": "invalid",
                "failed": "failed", "crash": "failed", "hr": "denied"}
    for name, kind in expected.items():
        registry.run_tool(name, {}, CTX)
        assert registry.take_outcome() == kind, name
        assert registry.take_outcome() is None
    registry.run_tool("nope", {}, CTX)
    assert registry.take_outcome() == "unknown_tool"


# ---- the agent records counts in its tool log ----

class OneCall(LLMProvider):
    def generate(self, system_prompt, messages, tools=None):
        if messages[-1].role != "tool":
            return LLMResponse(tool_calls=[ToolCall(
                "get_employee_records", {"record_type": "leave", "employee": "Rahim Uddin"})])
        return LLMResponse(text="done")


def test_the_tool_log_has_kind_rows_and_target(monkeypatch):
    payload = {"result": {"outcome": "records", "count": 3, "truncated": True,
                          "scope": "team", "employee": {"id": "E2", "name": "Rahim Uddin"},
                          "records": [{}, {}, {}]}}
    monkeypatch.setattr(agent, "run_tool", lambda name, args, ctx: payload)
    entry = run_agent("team leave", CTX, llm=OneCall()).tool_log[0]
    assert (entry["kind"], entry["rows"], entry["truncated"], entry["target"]) == (
        "ok", 3, True, "E2")


def test_a_failed_call_is_labelled_failed(monkeypatch):
    monkeypatch.setattr(agent, "run_tool", lambda name, args, ctx: {"error": "x"})
    entry = run_agent("team leave", CTX, llm=OneCall()).tool_log[0]
    assert entry["ok"] is False and entry["kind"] == "failed"