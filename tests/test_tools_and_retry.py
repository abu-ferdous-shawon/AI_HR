import json
from dataclasses import replace

import pytest
import requests
from starlette.requests import Request

from app import main
from app.agent import orchestrator
from app.agent.retriever import ReadContext
from app.auth.credentials import Credentials, current_holder
from app.auth.security import UserContext
from app.core import agent, audit
from app.core.llm.base import LLMError, LLMResponse, ToolCall
from app.frappe_client.client import FrappeClient

USER = UserContext("admin@example.com", "EMP-1", ("System Manager",), "session")
TOKEN = Credentials(authorization="token test:key")
PROTECTED = "phase6-private-value"
FIELDS = [{"fieldname": "name", "fieldtype": "Data"}, {"fieldname": "posting_date", "fieldtype": "Date"}]


def response(code, data):
    reply = requests.Response()
    reply.status_code = code
    reply._content = json.dumps(data).encode()
    return reply


def _audit_rows(monkeypatch):
    rows = []
    monkeypatch.setattr(audit, "record", lambda event, **data: rows.append({"event": event, **data}))
    return rows


def _configure(monkeypatch, model, transport, user=USER, *, reports=None):
    def client():
        holder = current_holder()
        assert holder.credentials == TOKEN
        if holder.client is None:
            holder.client = FrappeClient(credentials=holder.credentials)
            monkeypatch.setattr(holder.client.session, "get", transport)
        return holder.client

    monkeypatch.setattr(main.settings, "frappe_auth_mode", "user_session")
    monkeypatch.setattr(orchestrator, "get_frappe_client", client)
    monkeypatch.setattr(agent, "get_llm", lambda: model)
    monkeypatch.setattr(orchestrator, "retrieve", lambda _site, frappe, ctx, _query: ReadContext(
        frappe, ctx, schemas={"Sales Invoice": {
            "name": "Sales Invoice", "fields": FIELDS, "issingle": False, "child_tables": {},
        }}, reports=reports or {},
    ))

    def chat(message="Show Sales Invoice this month", debug=True):
        request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 12345)})
        return main.chat(request, main.ChatRequest(message=message, debug=debug), user, TOKEN)

    return chat


@pytest.mark.parametrize("tool", ["query_records", "aggregate_records", "run_frappe_report"])
def test_correction_recovers_from_real_frappe_error_payload(monkeypatch, tool):
    calls = []
    report = {"Sales Summary": {"ref_doctype": "Sales Invoice", "filters": [FIELDS[1] | {"reqd": True}]}}
    if tool == "run_frappe_report":
        args = {"report_name": "Sales Summary", "filters": {"posting_date": PROTECTED}}
        corrected = {"report_name": "Sales Summary", "filters": {"posting_date": "2026-11-01"}}
        success = {"message": {"result": [{"name": "SINV-1"}], "columns": ["name"]}}
    else:
        args = {"doctype": "Sales Invoice", "filters": [{"field": "posting_date", "op": "=", "value": PROTECTED}]}
        if tool == "query_records":
            args["fields"] = ["name"]
            success = {"data": [{"name": "SINV-1"}]}
        else:
            args["aggregate_fn"] = "count"
            success = {"data": [{"_metric": 1}]}
        corrected = {**args, "filters": [{**args["filters"][0], "value": "2026-11-01"}]}

    def transport(url, *, params, **_):
        calls.append(params)
        if len(calls) == 1:
            return response(417, {
                "exc_type": "ValidationError",
                "_server_messages": json.dumps([json.dumps({"message": "Invalid filter: <strong>posting_date</strong>"})]),
                "exc": PROTECTED,
            })
        assert "2026-11-01" in params["filters"]
        return response(200, success)

    class Model:
        def generate(self, prompt, messages, tools):
            if not calls:
                return LLMResponse(tool_calls=[ToolCall(tool, args)])
            if len(calls) == 1:
                recovery = messages[-1].tool_result["correction"]
                assert recovery["validation_message"] == "Invalid filter: posting_date"
                assert recovery["attempt"] == 1
                assert any(field["fieldname"] == "posting_date" for field in recovery["verified_fields"])
                assert PROTECTED not in json.dumps(recovery)
                return LLMResponse(tool_calls=[ToolCall(tool, corrected)])
            assert "result" in messages[-1].tool_result
            return LLMResponse(text="One invoice matched.")

    chat = _configure(monkeypatch, Model(), transport, reports=report)
    audit_rows = _audit_rows(monkeypatch)
    response_model = chat()
    assert response_model.answer == "One invoice matched."
    debug = response_model.debug.model_dump()
    assert debug["retry_count"] == 1 and len(calls) == 2
    assert list(debug["schemas"]) == ["Sales Invoice"]
    assert debug["rewritten_query"]["date_range"] is not None
    assert [call["kind"] for call in debug["tool_calls"]] == ["invalid", "ok"]
    assert [call["retry"] for call in debug["tool_calls"]] == [0, 1]
    assert all(call["latency_ms"] >= 0 for call in debug["tool_calls"])
    assert debug["error_trace"] == [{"stage": "tool", "outcome": "invalid", "tool": tool,
                                     "http_status": 417, "error_type": "ValidationError", "retry": 0}]
    event, = audit_rows
    assert event["session_id"] == response_model.conversation_id
    assert event["user"] == USER.user_id
    assert event["matched_doctypes"] == ["Sales Invoice"] and event["matched_reports"] == ["Sales Summary"]
    assert event["retry_count"] == 1 and event["error_trace"] == debug["error_trace"]
    serialized = json.dumps([event, debug])
    for text in (PROTECTED, "Invalid filter:", "token test:key", "SINV-1"):
        assert text not in serialized
    assert "[redacted]" in json.dumps(debug["tool_calls"])
    assert "2026-11-01" not in json.dumps(debug["tool_calls"])


def test_denial_skips_remaining_batch_and_never_calls_model_again(monkeypatch):
    first = ToolCall("query_records", {"doctype": "Sales Invoice", "fields": ["name"]})
    transport_calls = []

    def transport(*args, **kwargs):
        transport_calls.append(args)
        return response(403, {"exc_type": "PermissionError", "_server_messages": PROTECTED})

    class Model:
        def generate(self, *args):
            assert not transport_calls
            return LLMResponse(tool_calls=[first, first])

    result = _configure(monkeypatch, Model(), transport)()
    assert "denied access" in result.answer
    assert len(transport_calls) == 1 and result.debug.retry_count == 0
    assert [entry["kind"] for entry in result.debug.tool_calls] == ["denied", "skipped"]


def test_missing_report_filter_can_trigger_a_clarifying_question(monkeypatch):
    report = {"Sales Summary": {"ref_doctype": "Sales Invoice", "filters": [FIELDS[1] | {"reqd": True}]}}

    class Model:
        def generate(self, _prompt, messages, _tools):
            if messages[-1].role == "user":
                return LLMResponse(tool_calls=[ToolCall("run_frappe_report", {"report_name": "Sales Summary"})])
            correction = messages[-1].tool_result["correction"]
            assert correction["verified_fields"] == [FIELDS[1] | {"reqd": True}]
            return LLMResponse(text="Which posting date should I use?")

    chat = _configure(monkeypatch, Model(), lambda *a, **kw: pytest.fail("required filter is missing"), reports=report)
    result = chat(message="Run Sales Summary report")
    assert result.answer == "Which posting date should I use?"
    assert result.debug.retry_count == 1
    assert len(result.debug.tool_calls) == 1


def test_correction_cannot_switch_to_a_different_tool(monkeypatch):
    calls = []

    def transport(*args, **kwargs):
        calls.append(args)
        return response(417, {"exc_type": "ValidationError", "_error_message": "Invalid date format"})

    class Model:
        def generate(self, _prompt, messages, _tools):
            if messages[-1].role == "user":
                return LLMResponse(tool_calls=[ToolCall("query_records", {"doctype": "Sales Invoice", "fields": ["name"]})])
            assert messages[-1].tool_result["correction"]["validation_message"] == "Invalid date format"
            return LLMResponse(tool_calls=[ToolCall("get_record_detail", {"doctype": "Sales Invoice", "name": "SINV-1"})])

    result = _configure(monkeypatch, Model(), transport)()
    assert len(calls) == 1 and result.debug.retry_count == 1
    assert [item["kind"] for item in result.debug.tool_calls] == ["invalid", "skipped"]


def test_retry_redacts_never_send_field_and_survives_model_failure_in_audit(monkeypatch):
    def transport(*args, **kwargs):
        return response(417, {"exc_type": "ValidationError", "_error_message": "Invalid fieldname: api_secret"})

    class Model:
        def generate(self, system, messages, tools):
            if messages[-1].role == "user":
                return LLMResponse(tool_calls=[ToolCall("query_records", {
                    "doctype": "Sales Invoice", "fields": ["name"],
                })])
            correction = messages[-1].tool_result["correction"]
            assert correction["validation_message"] == "Invalid fieldname: [field omitted]"
            assert "api_secret" not in json.dumps(correction)
            raise LLMError(PROTECTED)

    chat = _configure(monkeypatch, Model(), transport)
    audit_rows = _audit_rows(monkeypatch)
    with pytest.raises(main.HTTPException) as exc:
        chat(message=f"Show Sales Invoice {PROTECTED}")
    assert exc.value.status_code == 503
    event, = audit_rows
    assert event["retry_count"] == 1 and event["status"] == "llm_error"
    assert [e["stage"] for e in event["error_trace"]] == ["tool", "chat"]
    assert PROTECTED not in json.dumps(event) and "api_secret" not in json.dumps(event)


@pytest.mark.parametrize("user,requested,allowed", [
    (USER, False, False), (USER, True, True),
    (replace(USER, roles=("Employee",)), True, False),
    (replace(USER, role_source="mapping"), True, False),
])
def test_http_debug_is_opt_in_and_requires_a_verified_admin(monkeypatch, user, requested, allowed):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main.settings, "frappe_auth_mode", "user_session")
    monkeypatch.setitem(main.app.dependency_overrides, main.current_user, lambda: user)
    browser = TestClient(main.app)
    reply = browser.post("/chat", headers={"Authorization": TOKEN.authorization},
                         json={"message": "Hello", "debug": requested})
    assert reply.status_code == 200
    assert ("debug" in reply.json()) == allowed
    if allowed:
        assert reply.json()["debug"]["retry_count"] == 0
        assert reply.json()["debug"]["rewritten_query"]["intent"] == "conversational"
