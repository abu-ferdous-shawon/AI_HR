import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.agent import orchestrator
from app.agent.intent_router import route_intent
from app.agent.query_rewriter import rewrite_query
from app.agent.retriever import ReadContext, render_context, retrieve, use_read_context
from app.auth.credentials import Credentials, current_holder, use_credentials
from app.auth.security import UserContext
from app.core.llm.base import LLMResponse, Message, ToolCall
from app.core.llm.gemini import GeminiProvider
from app.frappe_client.client import FrappeClient, FrappeError
from app.tools.registry import get_tool_specs, run_tool
from tests.test_metadata_index import site_index  # pytest fixture

USER = UserContext("employee@example.com", "EMP-1")
TOKEN = Credentials(authorization="token example:test")


def schema(name, *fields):
    return {"name": name, "issingle": False,
            "fields": [{"fieldname": key, "fieldtype": kind} for key, kind in fields],
            "detail_fields": [key for key, _ in fields], "child_tables": {}}


def test_router_and_date_follow_up():
    assert route_intent("Hello!").reply
    assert route_intent("What is the leave policy?").kind == "conversational"
    assert route_intent("Can you cancel my leave?").kind == "erp_write"
    assert route_intent("Create a CSV report of my leave history").kind == "erp_read"
    parsed = rewrite_query("What about last month?", [Message("user", "Show sales invoices this month")],
                           date(2026, 1, 5))
    assert parsed.search_text.endswith("What about last month?")
    assert parsed.previous_topic == "Show sales invoices this month"
    assert parsed.date_range == ("2025-12-01", "2025-12-31")


def test_fast_path_skips_retrieval_and_tools(monkeypatch):
    monkeypatch.setattr(orchestrator, "retrieve", lambda *_: pytest.fail("must not retrieve"))
    assert orchestrator.run_agent("Hello", USER).tool_log == []
    assert "confirmation" in orchestrator.run_agent("Delete the Sales Invoice", USER).answer

    class Model:
        turn = 0

        def generate(self, system, messages, tools):
            self.turn += 1
            assert {spec["name"] for spec in tools} <= {"search_hr_policy", "calculate"}
            if self.turn == 1:
                return LLMResponse(tool_calls=[ToolCall("get_my_profile", {})])
            assert messages[-1].tool_result["error"]
            return LLMResponse(text="The policy handbook explains this.")

    assert orchestrator.run_agent("What is our leave policy?", USER, llm=Model()).answer


def test_indexed_metadata_is_checked_with_the_user_and_pruned_before_model(site_index):
    from app.indexer.sync import sync_metadata

    site, _ = site_index
    sync_metadata(site)

    class UserClient:
        def get_list(self, doctype, fields, filters, limit):
            assert limit == 1
            if doctype == "Leave Application":
                assert filters == {}
                return [{"name": "LEAVE-1", "employee": "EMP-1"}]
            if doctype == "Employee":
                return [{"company": "Example Ltd"}]
            raise FrappeError(403, "Denied")

        def call_method(self, method, params):
            raise FrappeError(403, "Denied")

        def get_doc(self, doctype, name, fields):
            raise FrappeError(403, "Denied")

    result = retrieve(site, UserClient(), USER, "Show Leave Application and the Leave Summary")
    assert result.denied_target
    assert set(result.schemas) == {"Leave Application"}
    assert {field["fieldname"] for field in result.schemas["Leave Application"]["fields"]} == {"name", "employee"}
    assert not result.reports
    view = render_context(result)
    assert "Leave Summary" not in view
    assert "child_tables" not in view
    assert "LEAVE-1" not in view


def test_list_and_aggregate_forward_fixed_permission_checked_orm_parameters(monkeypatch):
    client = FrappeClient(credentials=TOKEN)
    observed = []

    def dispatch(path, params):
        observed.append((path, params))
        if isinstance(json.loads(params["fields"])[-1], dict):
            return {"data": [{"_metric": 3, "status": "Approved"}]}
        return {"data": [{"name": "A", "employee": USER.employee_id},
                         {"name": "B", "employee": USER.employee_id}]}

    monkeypatch.setattr(client, "_get", dispatch)
    args = {"doctype": "Leave Application", "fields": ["name"], "limit": 1,
            "or_filters": [{"field": "status", "op": "=", "value": "Approved"}]}
    catalog = {"Leave Application": schema("Leave Application", ("name", "Data"), ("status", "Select"))}
    assert "error" in run_tool("query_records", args, USER)
    with use_credentials(TOKEN):
        current_holder().client = client
        with use_read_context(ReadContext(client, USER, schemas=catalog)):
            listed = run_tool("query_records", args, USER)["result"]
            assert listed["records"] == [{"name": "A"}] and listed["next_start"] == 1
            assert "error" in run_tool("query_records", {**args, "fields": ["api_secret"]}, USER)
            assert "error" in run_tool("query_records", {**args, "limit": "1"}, USER)
            assert "error" in run_tool("query_records", args, UserContext("other", "EMP-2"))
            assert "error" in run_tool("get_record_detail", {"doctype": "Employee", "name": "EMP-2"}, USER)
            result = run_tool("aggregate_records", {"doctype": "Leave Application", "aggregate_fn": "count",
                                                    "group_by": "status"}, USER)["result"]
    assert result["groups"] == [{"count": 3, "status": "Approved"}]
    list_params, agg_params = (params for _, params in observed)
    assert "filters" not in list_params or not json.loads(list_params["filters"])
    assert json.loads(list_params["or_filters"]) == [["status", "=", "Approved"]]
    assert json.loads(agg_params["fields"]) == ["status", {"COUNT": "name", "as": "_metric"}]
    assert agg_params["group_by"] == "status"


def test_report_calls_frappe_with_the_current_session_and_masks_positional_amounts(monkeypatch):
    client = FrappeClient(credentials=TOKEN)

    def fetch(path, params):
        assert path == "/api/method/frappe.desk.query_report.run"
        assert params["ignore_prepared_report"] == 1
        assert "user" not in params
        assert json.loads(params["filters"]) == {}
        return {"message": {"columns": ["Item:Data:120", "Gross Pay:Currency:99"],
                            "result": [["A", 12345]]}}

    monkeypatch.setattr(client, "_get", fetch)
    report = {"filters": [], "ref_doctype": "Salary Slip"}
    monkeypatch.setattr(orchestrator, "retrieve", lambda *args: ReadContext(
        client, USER, reports={"Inventory Test": report},
    ))

    class Model:
        def generate(self, system, messages, tools):
            assert '"name": "Inventory Test"' in system
            GeminiProvider._to_tools(tools)
            if messages[-1].role == "user":
                return LLMResponse(tool_calls=[ToolCall("run_frappe_report", {"report_name": "Inventory Test"})])
            assert "12345" not in repr(messages)
            return LLMResponse(text="Gross pay is [[MASK_1]].")

    with use_credentials(TOKEN):
        current_holder().client = client
        result = orchestrator.run_agent("Run Inventory Test report", USER, llm=Model())
    assert result.answer == "Gross pay is 12345."
    assert result.tool_log[0]["tool"] == "run_frappe_report"


@pytest.mark.parametrize(("status", "error_type", "attempts", "reply"), [
    (403, "PermissionError", 1, "denied access"),
    (417, "PermissionError", 1, "denied access"),
    (417, "ValidationError", 3, "check the record ID"),
    (417, "", 1, "check the record ID"),
    (404, "DoesNotExistError", 1, "check the record ID"),
    (500, "OperationalError", 1, "could not complete"),
])
def test_only_correctable_errors_are_retried(monkeypatch, status, error_type, attempts, reply):
    client = FrappeClient(credentials=TOKEN)
    calls = []

    def fetch(path, params):
        calls.append(path)
        raise FrappeError(status, "upstream details", error_type=error_type)

    monkeypatch.setattr(client, "_get", fetch)
    monkeypatch.setattr(orchestrator, "retrieve", lambda *args: ReadContext(
        client, USER, schemas={"Item": schema("Item", ("name", "Data"))},
    ))

    class Model:
        def generate(self, *args):
            return LLMResponse(tool_calls=[ToolCall("query_records", {"doctype": "Item", "fields": ["name"]})])

    with use_credentials(TOKEN):
        current_holder().client = client
        result = orchestrator.run_agent("List items", USER, llm=Model())
    assert len(calls) == attempts
    assert reply in result.answer
    assert "upstream details" not in repr(result.messages)


def test_legacy_catalog_retains_its_tools():
    names = {spec["name"] for spec in get_tool_specs()}
    assert "get_my_profile" in names
    assert "query_records" not in names


def test_chat_entry_point_uses_signed_in_context_and_indexed_tool(site_index, monkeypatch):
    from starlette.requests import Request
    from app import main
    from app.config import settings
    from app.core import agent
    from app.indexer.sync import sync_metadata

    site, _ = site_index
    sync_metadata(site)
    monkeypatch.setattr(settings, "frappe_auth_mode", "user_session")
    calls = []

    def fetch(client, path, params=None):
        assert client.acts_as_user
        assert client.session.headers["Authorization"] == TOKEN.authorization
        calls.append(path)
        if path.endswith("/Employee"):
            return {"data": [{"company": "Example Ltd"}]}
        if path.endswith("/Leave%20Application"):
            filters = json.loads(params.get("filters", "[]"))
            assert "docstatus" in json.loads(params["fields"])
            if calls.count("/api/resource/Leave%20Application") == 2:
                assert ["docstatus", "=", 1] in filters
                assert ["employee", "=", USER.employee_id] in filters
            return {"data": [{"name": "LEAVE-1", "employee": USER.employee_id,
                              "status": "Approved", "docstatus": 1}]}
        raise FrappeError(403, "Denied")

    monkeypatch.setattr(FrappeClient, "_get", fetch)

    class Model:
        def generate(self, system, messages, tools):
            assert "Example Ltd" in system and USER.user_id not in system
            assert "query_records" in {tool["name"] for tool in tools}
            if messages[-1].role == "user":
                return LLMResponse(tool_calls=[ToolCall("query_records", {
                    "doctype": "Leave Application", "fields": ["name", "status", "docstatus"], "limit": 1,
                    "filters": [{"field": "docstatus", "op": "=", "value": 1},
                                {"field": "employee", "op": "=", "value": USER.employee_id}],
                })])
            assert messages[-1].tool_result["result"]["records"] == [
                {"name": "LEAVE-1", "status": "Approved", "docstatus": 1},
            ]
            return LLMResponse(text="LEAVE-1 is approved.")

    monkeypatch.setattr(agent, "get_llm", lambda: Model())
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 12345)})
    answer = main.chat(request, main.ChatRequest(message="Show my Leave Application"), USER, TOKEN)
    assert answer.answer == "LEAVE-1 is approved."
    assert calls.count("/api/resource/Leave%20Application") == 2


@pytest.mark.parametrize("may_read", [True, False])
def test_login_without_employee_uses_that_accounts_frappe_permissions(site_index, monkeypatch, may_read):
    from starlette.requests import Request
    from app import main
    from app.auth import security
    from app.core import agent
    from app.indexer.sync import sync_metadata

    site, _ = site_index
    sync_metadata(site)
    login = "reviewer@example.com"
    monkeypatch.setattr(main.settings, "frappe_auth_mode", "service_account")
    monkeypatch.setattr(main, "identify_user", lambda authorization, cookie: login)

    class Directory:
        def get_list(self, doctype, fields, filters, limit):
            assert doctype == "Employee" and filters["user_id"] == login
            return []

    monkeypatch.setattr(security, "get_service_client", Directory)

    def fetch(client, path, params=None):
        assert client.acts_as_user
        assert client.session.headers["Authorization"] == TOKEN.authorization
        if path == "/api/resource/User/reviewer%40example.com":
            return {"data": {"roles": [{"role": "HR User"}, {"role": "All"}]}}
        assert path == "/api/resource/Leave%20Application"
        assert "filters" not in params
        if not may_read:
            raise FrappeError(403, "denied", error_type="PermissionError")
        return {"data": [{"name": "LEAVE-1", "employee": "EMP-1", "status": "Approved"}]}

    monkeypatch.setattr(FrappeClient, "_get", fetch)
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 12345)})
    ctx = main.current_user(request, TOKEN.authorization, None, None)
    assert (ctx.user_id, ctx.employee_id, ctx.roles, ctx.role_source) == (login, None, ("HR User",), "session")
    assert "requires an active Employee" in run_tool("get_my_leave_balance", {}, ctx)["error"]

    class Model:
        def generate(self, system, messages, tools):
            assert may_read
            assert "has no linked active Employee" in system
            names = {tool["name"] for tool in tools}
            assert "query_records" in names
            assert not names.intersection({"get_my_profile", "create_report", "list_my_team", "search_hr_policy"})
            if messages[-1].role == "user":
                return LLMResponse(tool_calls=[ToolCall("query_records", {
                    "doctype": "Leave Application", "fields": ["name", "status"],
                })])
            assert messages[-1].tool_result["result"]["records"] == [{"name": "LEAVE-1", "status": "Approved"}]
            return LLMResponse(text="LEAVE-1 is approved.")

    monkeypatch.setattr(agent, "get_llm", lambda: Model())
    answer = main.chat(request, main.ChatRequest(message="Show Leave Application records"), ctx, TOKEN)
    assert answer.answer == ("LEAVE-1 is approved." if may_read else
                             "Frappe denied access to that record or report for your account.")


def test_salary_slip_reads_apply_current_frappe_access_and_field_visibility(monkeypatch):
    from starlette.requests import Request
    from app import main
    from app.core import agent

    client = FrappeClient(credentials=TOKEN)
    allowed = True
    calls = []

    def fetch(path, params=None):
        calls.append((path, params))
        assert client.session.headers["Authorization"] == TOKEN.authorization
        if not allowed:
            raise FrappeError(403, "Denied", error_type="PermissionError")
        if path.endswith("/SLIP-2"):
            # Frappe omitted net_pay for this user, even though it is in the schema.
            return {"data": {"name": "SLIP-2", "employee": "HR-EMP-00002", "gross_pay": 12345}}
        assert json.loads(params["filters"]) == [
            ["employee", "=", "HR-EMP-00002"], ["docstatus", "=", 1],
        ]
        return {"data": [{"name": "SLIP-2"}]}

    monkeypatch.setattr(client, "_get", fetch)
    catalog = {"Salary Slip": schema("Salary Slip", ("name", "Data"), ("employee", "Link"),
                                    ("docstatus", "Int"), ("gross_pay", "Currency"), ("net_pay", "Currency"))}
    monkeypatch.setattr(orchestrator, "retrieve", lambda *args: ReadContext(client, USER, schemas=catalog))

    class Model:
        def generate(self, system, messages, tools):
            assert "get_employee_records" not in {tool["name"] for tool in tools}
            assert "including another employee's salary slip" in system
            assert "details or salary slips of anyone else" not in system
            if messages[-1].role == "user":
                return LLMResponse(tool_calls=[ToolCall("query_records", {
                    "doctype": "Salary Slip", "fields": ["name"],
                    "filters": [{"field": "employee", "op": "=", "value": "HR-EMP-00002"},
                                {"field": "docstatus", "op": "=", "value": 1}],
                })])
            record = messages[-1].tool_result["result"]
            if "records" in record:
                return LLMResponse(tool_calls=[ToolCall("get_record_detail", {
                    "doctype": "Salary Slip", "name": record["records"][0]["name"],
                })])
            assert "net_pay" not in record["record"]
            assert "12345" not in repr(messages)
            return LLMResponse(text=f"Gross pay is {record['record']['gross_pay']}.")

    question = "Show the submitted salary slip for employee HR-EMP-00002"
    retrieve = orchestrator.retrieve

    def retrieve_with_test_client(*args):
        current_holder().client = client
        return retrieve(*args)

    monkeypatch.setattr(orchestrator, "retrieve", retrieve_with_test_client)
    monkeypatch.setattr(main.settings, "frappe_auth_mode", "user_session")
    monkeypatch.setattr(main, "_store", main.OrderedDict())
    monkeypatch.setattr(agent, "get_llm", lambda: Model())
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 12345)})
    first = main.chat(request, main.ChatRequest(message=question), USER, TOKEN)
    assert first.answer == "Gross pay is 12345."

    class FollowUpModel(Model):
        def generate(self, system, messages, tools):
            assert len(messages) == 2
            assert messages[0] == Message("user", question)
            assert "12345" not in repr(messages)
            assert "[[MASK_1]]" not in system
            return super().generate(system, messages, tools)

    monkeypatch.setattr(agent, "get_llm", lambda: FollowUpModel())
    allowed = False
    follow_up = main.ChatRequest(message="What about last month?", conversation_id=first.conversation_id)
    assert "denied access" in main.chat(request, follow_up, USER, TOKEN).answer
    assert len(calls) == 3
