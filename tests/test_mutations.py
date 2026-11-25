import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.agent import mutation_planner
from app.auth.credentials import Credentials, extract_credentials, use_credentials, current_holder
from app.auth.security import UserContext
from app.core import sites
from app.core.llm.base import LLMResponse, ToolCall
from app.core.llm.gemini import GeminiProvider
from app.frappe_client.client import FrappeClient, FrappeError
from app.mutations import confirmation
from app.mutations.confirmation import Binding, ConfirmationStore
from app.mutations.service import MutationFailure, MutationRequest, prepare_mutation

USER = UserContext("worker@example.com", "EMP-1")
CREDS = Credentials(authorization="token worker:test")
STAMP = "2026-11-22 10:00:00.000000"


class FakeFrappe:
    def __init__(self):
        self.acts_as_user = True
        self.session = SimpleNamespace(close=lambda: None)
        self.allow = True
        self.writes = []
        self.failure = None
        self.doc = {"name": "TASK-1", "modified": STAMP, "docstatus": 0, "description": "Before"}

    def has_document_permission(self, doctype, permission, name=None):
        return self.allow

    def get_meta(self, doctype):
        return [{"name": doctype, "is_submittable": int(doctype == "Test Transaction"), "fields": [
            {"fieldname": "description", "fieldtype": "Text Editor", "reqd": 1},
            {"fieldname": "priority", "fieldtype": "Select", "options": "Low\nHigh"},
            {"fieldname": "company", "fieldtype": "Link", "options": "Company"},
            {"fieldname": "posting_date", "fieldtype": "Date"},
            {"fieldname": "api_secret", "fieldtype": "Data"},
            {"fieldname": "docstatus", "fieldtype": "Int"},
            *([{"fieldname": "cell_number", "fieldtype": "Data"}] if doctype == "Employee" else []),
        ]}]

    def get_doc(self, doctype, name):
        return deepcopy(self.doc)

    def get_list(self, doctype, fields, filters, limit):
        assert doctype == "Company" and fields == ["name"] and limit == 1
        return [{"name": "Allowed Co"}] if filters["name"] == "Allowed Co" else []

    def _record(self, method, doctype, name, payload, modified=None):
        self.writes.append((method, doctype, name, payload, modified))
        if self.failure:
            raise self.failure
        return {"name": name or "NEW-1"}

    def create_document(self, doctype, payload):
        return self._record("POST", doctype, None, payload)

    def update_document(self, doctype, name, payload, *, modified):
        return self._record("PUT", doctype, name, payload, modified)

    def delete_document(self, doctype, name):
        self._record("DELETE", doctype, name, None)


@pytest.fixture
def site(monkeypatch, request):
    write_doctypes = getattr(request, "param", ["ToDo", "Test Transaction"])
    site = sites.get_default_site().model_copy(update={"write_doctypes": write_doctypes})
    monkeypatch.setattr(sites, "_loaded", lambda: ({site.name: site}, site.name))
    monkeypatch.setattr(main.settings, "frappe_auth_mode", "user_session")
    store = ConfirmationStore()
    monkeypatch.setattr(main, "confirmations", store)
    monkeypatch.setattr(confirmation, "confirmations", store)
    client = FakeFrappe()

    def as_user():
        assert current_holder().credentials == CREDS
        current_holder().client = client
        return client

    monkeypatch.setattr(main, "get_frappe_client", as_user)
    from app.agent import orchestrator
    monkeypatch.setattr(orchestrator, "get_frappe_client", as_user)
    return client


def _request():
    return Request({"type": "http", "headers": [], "client": ("127.0.0.1", 12345)})


def _propose(monkeypatch, operation, doctype, name=None, *, target=None, **payload):
    arguments = {"operation": operation, "doctype": doctype, "name": name, "payload": payload}

    class Model:
        def generate(self, prompt, messages, tools):
            assert [spec["name"] for spec in tools] == ["propose_mutation"]
            assert "api_secret" not in prompt
            GeminiProvider._to_tools(tools)
            return LLMResponse(tool_calls=[ToolCall("propose_mutation", arguments)])

    monkeypatch.setattr(mutation_planner, "get_llm", lambda: Model())
    text = f"{operation} {target or doctype}" + (f" {name}" if name else "")
    response = main.chat(_request(), main.ChatRequest(message=text, conversation_id="mutation_1"), USER, CREDS)
    assert response.confirmation is not None
    assert response.confirmation.token not in response.answer
    assert response.confirmation.token not in repr(main._store)
    return response.confirmation


def _decision(card, decision="confirm", conversation_id="mutation_1"):
    return main.MutationDecision(token=card.token, conversation_id=conversation_id, decision=decision)


@pytest.mark.parametrize(("operation", "doctype", "status", "method", "payload", "expected"), [
    ("create", "ToDo", 0, "POST", {"description": "After"}, {"description": "After"}),
    ("update", "ToDo", 0, "PUT", {"description": "After"}, {"description": "After"}),
    ("submit", "Test Transaction", 0, "PUT", {}, {"docstatus": 1}),
    ("cancel", "Test Transaction", 1, "PUT", {}, {"docstatus": 2}),
    ("delete", "ToDo", 0, "DELETE", {}, None),
])
def test_chat_proposes_and_the_confirmation_endpoint_writes_once(
    site, monkeypatch, operation, doctype, status, method, payload, expected,
):
    site.doc["docstatus"] = status
    name = None if operation == "create" else "TASK-1"
    card = _propose(monkeypatch, operation, doctype, name, **payload)
    assert site.writes == []
    assert card.operation == operation and card.document_name == name
    assert {change.field: change.after for change in card.changes} == (expected or {})
    if operation == "update":
        assert card.changes[0].before == "Before"

    response = main.confirm_change(_decision(card), USER, CREDS)
    assert response.state == "executed" and response.operation == operation
    assert site.writes == [(method, doctype, name, expected, STAMP if method == "PUT" else None)]
    with pytest.raises(HTTPException) as repeated:
        main.confirm_change(_decision(card), USER, CREDS)
    assert repeated.value.status_code == 409
    assert len(site.writes) == 1


@pytest.mark.parametrize("site", [[]], indirect=True)
def test_create_uses_live_frappe_permissions_without_a_doctype_list(site, monkeypatch):
    card = _propose(monkeypatch, "create", "Custom Employee Request",
                    target='DocType "Custom Employee Request"', description="New")
    assert site.writes == []
    response = main.confirm_change(_decision(card), USER, CREDS)
    assert response.state == "executed"
    assert site.writes == [("POST", "Custom Employee Request", None, {"description": "New"}, None)]


@pytest.mark.parametrize("site", [[]], indirect=True)
@pytest.mark.parametrize("allowed", [True, False])
def test_update_my_phone_uses_the_linked_employee_and_frappe_permission(site, monkeypatch, allowed):
    def has_permission(doctype, permission, name=None):
        assert doctype == "Employee"
        if permission == "write":
            assert name == USER.employee_id
            return allowed
        assert permission == "read"
        return True

    def unexpected_model():
        pytest.fail("A self-service phone update does not need the model")

    monkeypatch.setattr(site, "has_document_permission", has_permission)
    monkeypatch.setattr(mutation_planner, "get_llm", unexpected_model)
    site.doc.update(name=USER.employee_id, cell_number="01700000000")
    number = "01800000001"
    request = main.ChatRequest(message=f"update my phone number to {number}\\",
                               conversation_id="mutation_1")
    response = main.chat(_request(), request, USER, CREDS)
    assert site.writes == []
    if allowed:
        card = response.confirmation
        assert card.doctype == "Employee" and card.document_name == USER.employee_id
        assert [(change.field, change.after) for change in card.changes] == [("cell_number", number)]
        assert main.confirm_change(_decision(card), USER, CREDS).state == "executed"
        assert site.writes == [("PUT", "Employee", USER.employee_id, {"cell_number": number}, STAMP)]
    else:
        assert response.confirmation is None
        assert "Frappe denied permission" in response.answer


@pytest.mark.parametrize("site", [[]], indirect=True)
def test_indexed_update_checks_the_signed_in_users_record_permission(site, monkeypatch, tmp_path):
    (tmp_path / "alias_map.json").write_text("{}", encoding="utf-8")

    @contextmanager
    def snapshot(_site, _packs):
        yield tmp_path, {"schemas": {"Custom Request": {}}}

    monkeypatch.setattr(mutation_planner, "open_snapshot", snapshot)
    checks = []

    def has_permission(doctype, permission, name=None):
        checks.append((doctype, permission, name))
        return permission == "read"

    monkeypatch.setattr(site, "has_document_permission", has_permission)
    reply = LLMResponse(tool_calls=[ToolCall("propose_mutation", {
        "operation": "update", "doctype": "Custom Request", "name": "TASK-1",
        "payload": {"description": "Changed"},
    })])
    monkeypatch.setattr(mutation_planner, "get_llm", lambda: SimpleNamespace(generate=lambda *args: reply))
    response = main.chat(_request(), main.ChatRequest(message="Update Custom Request TASK-1"), USER, CREDS)
    assert response.confirmation is None
    assert "Frappe denied permission" in response.answer
    assert checks == [("Custom Request", "read", None), ("Custom Request", "write", "TASK-1")]
    assert site.writes == []


@pytest.mark.parametrize("site", [[]], indirect=True)
def test_unclear_write_suggests_similar_doctypes_and_relevant_fields(site, monkeypatch, tmp_path):
    monkeypatch.setattr(mutation_planner, "open_snapshot",
                        lambda *args: nullcontext((tmp_path, {"schemas": {}})))
    (tmp_path / "alias_map.json").write_text("{}", encoding="utf-8")

    def search(config, tier, query, limit):
        assert tier == "schema_index"
        assert "priority" in query and "01800000001" not in query
        return [
            {"key": "ToDo", "score": 0.8},
            {"key": "Private Record", "score": 0.7},
            {"key": "User", "score": 0.6},
            {"key": "Task", "score": 0.5},
            {"key": "Unrelated Record", "score": 0.1},
        ]

    def has_permission(doctype, permission, name=None):
        assert permission == "read" and name is None
        return doctype != "Private Record"

    monkeypatch.setattr(mutation_planner, "search_metadata", search)
    monkeypatch.setattr(site, "has_document_permission", has_permission)
    body = main.ChatRequest(message="Update the priority of my work item for 01800000001")
    response = main.chat(_request(), body, USER, CREDS)
    assert response.confirmation is None
    assert "Which DocType did you mean?" in response.answer
    assert "**ToDo** (priority," in response.answer and "**Task** (priority," in response.answer
    assert all(name not in response.answer for name in ("Private Record", "User", "Unrelated Record", "api_secret"))
    assert site.writes == []


@pytest.mark.parametrize(("site", "doctype"), [
    ([], "user"),
    (["ToDo"], "Custom Employee Request"),
], indirect=["site"])
def test_write_restrictions_are_enforced_before_loading_schema(site, monkeypatch, doctype):
    def unexpected(*args):
        pytest.fail("A blocked DocType must not reach Frappe")

    monkeypatch.setattr(site, "has_document_permission", unexpected)
    request = main.ChatRequest(message=f'Create a record in DocType "{doctype}"')
    response = main.chat(_request(), request, USER, CREDS)
    assert response.confirmation is None
    assert "Chat writes are unavailable for this DocType" in response.answer
    assert site.writes == []


def test_wrong_conversation_cannot_consume_proposal_and_discard_has_no_write(site, monkeypatch):
    card = _propose(monkeypatch, "delete", "ToDo", "TASK-1")
    with pytest.raises(HTTPException) as err:
        main.confirm_change(_decision(card, conversation_id="other"), USER, CREDS)
    assert err.value.status_code == 409
    response = main.confirm_change(_decision(card, "discard"), USER, CREDS)
    assert response.state == "discarded" and site.writes == []
    with pytest.raises(HTTPException) as err:
        main.confirm_change(_decision(card), USER, CREDS)
    assert err.value.status_code == 409


def test_modified_record_or_revoked_permission_blocks_the_write(site, monkeypatch):
    card = _propose(monkeypatch, "update", "ToDo", "TASK-1", description="After")
    site.doc["modified"] = "2026-11-22 10:01:00.000000"
    with pytest.raises(HTTPException) as stale:
        main.confirm_change(_decision(card), USER, CREDS)
    assert stale.value.status_code == 409 and site.writes == []

    card = _propose(monkeypatch, "delete", "ToDo", "TASK-1")
    site.allow = False
    with pytest.raises(HTTPException) as revoked:
        main.confirm_change(_decision(card), USER, CREDS)
    assert revoked.value.status_code == 403 and site.writes == []


@pytest.mark.parametrize(("payload", "message"), [
    ({}, "required fields"),
    ({"description": "New", "priority": "Urgent"}, "available option"),
    ({"description": "New", "posting_date": "wrong"}, "valid Date"),
    ({"description": "New", "company": "Unknown Co"}, "existing link"),
    ({"description": "New", "api_secret": "hidden"}, "unavailable"),
    ({"description": "New", "docstatus": 1}, "unavailable"),
])
def test_invalid_values_never_create_a_draft(site, payload, message):
    request = MutationRequest(operation="create", doctype="ToDo", payload=payload)
    with pytest.raises(MutationFailure, match=message):
        prepare_mutation(site, USER, request)
    assert site.writes == []


def test_new_proposal_expires_superseded_card_and_confirmation_is_atomic(site, monkeypatch):
    binding = Binding("site", "user", "conversation", "session")
    change = prepare_mutation(site, USER, MutationRequest(
        operation="create", doctype="ToDo", payload={"description": "New", "company": "Allowed Co"},
    ))
    store = ConfirmationStore()
    first = store.issue(binding, change)
    second = store.issue(binding, change)
    assert store.pending_card(binding) == second
    for field in ("site", "user", "conversation_id", "session"):
        assert store.pending_card(replace(binding, **{field: "different"})) is None
    with pytest.raises(MutationFailure):
        store.take(binding, first["token"])
    with pytest.raises(MutationFailure):
        store.take(Binding("site", "user", "conversation", "different session"), second["token"])

    def consume():
        try:
            store.take(binding, second["token"])
            return True
        except MutationFailure:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: consume(), range(2))) == [False, True]
    assert store.pending_card(binding) is None
    monkeypatch.setattr(confirmation, "TTL_SECONDS", 0)
    expired = store.issue(binding, change)
    assert store.pending_card(binding) is None
    with pytest.raises(MutationFailure):
        store.take(binding, expired["token"])


def test_frappe_mandatory_error_is_safe_and_a_failed_write_is_not_replayed(site, monkeypatch):
    card = _propose(monkeypatch, "create", "ToDo", description="New")
    site.failure = FrappeError(417, "upstream value must not appear", error_type="MandatoryError")
    with pytest.raises(HTTPException) as err:
        main.confirm_change(_decision(card), USER, CREDS)
    assert err.value.status_code == 422
    assert "additional fields" in err.value.detail and "upstream" not in err.value.detail
    with pytest.raises(HTTPException) as repeated:
        main.confirm_change(_decision(card), USER, CREDS)
    assert repeated.value.status_code == 409 and len(site.writes) == 1


def test_frappe_transport_is_user_only_and_forwards_csrf_and_version(monkeypatch):
    sid = extract_credentials(None, "sid=example-session; theme=dark", "csrf-for-session")
    client = FrappeClient(credentials=sid)
    assert client.session.headers["X-Frappe-CSRF-Token"] == "csrf-for-session"
    assert client.session.headers["Cookie"] == "sid=example-session"
    observed = {}

    def send(method, url, **kwargs):
        observed.update(method=method, url=url, **kwargs)
        return SimpleNamespace(status_code=200, json=lambda: {"data": {"name": "TASK-1"}})

    monkeypatch.setattr(client.session, "request", send)
    assert client.update_document("Test Transaction", "TASK-1", {"docstatus": 1}, modified=STAMP)
    assert observed["method"] == "PUT" and observed["url"].endswith("/Test%20Transaction/TASK-1")
    assert observed["json"] == {"docstatus": 1, "modified": STAMP}
    assert observed["allow_redirects"] is False
    with pytest.raises(FrappeError, match="no user login"):
        FrappeClient().delete_document("ToDo", "TASK-1")


def test_free_text_confirmation_never_writes_and_service_mode_cannot_confirm(site, monkeypatch):
    card = _propose(monkeypatch, "delete", "ToDo", "TASK-1")

    def no_model(*args, **kwargs):
        pytest.fail("A pending confirmation reply should skip the model")

    monkeypatch.setattr(main, "run_agent", no_model)
    for message in ("reviewed", "Yes, confirm!", "Discard"):
        response = main.chat(_request(), main.ChatRequest(message=message, conversation_id="mutation_1"), USER, CREDS)
        assert response.confirmation == card and site.writes == []
        assert "Confirm delete" in response.answer
    monkeypatch.setattr(main.settings, "frappe_auth_mode", "service_account")
    with pytest.raises(HTTPException) as err:
        main.confirm_change(_decision(card), USER, CREDS)
    assert err.value.status_code == 403 and site.writes == []


def test_http_confirmation_rejects_model_payload_and_requires_confirmation_token(site, monkeypatch):
    from fastapi.testclient import TestClient

    card = _propose(monkeypatch, "update", "ToDo", "TASK-1", description="After")
    monkeypatch.setitem(main.app.dependency_overrides, main.current_user, lambda: USER)
    headers = {"Authorization": CREDS.authorization, "Origin": "http://testserver"}
    browser = TestClient(main.app)
    data = _decision(card).model_dump()
    response = browser.post("/chat", headers=headers, json={
        "message": "reviewed", "conversation_id": data["conversation_id"],
    })
    assert response.status_code == 200
    assert response.json()["confirmation"] == card.model_dump()
    assert site.writes == []
    assert browser.post("/chat/confirm", headers=headers, json={
        **data, "payload": {"description": "tampered"},
    }).status_code == 422
    assert site.writes == []
    response = browser.post("/chat/confirm", headers=headers, json=data)
    assert response.status_code == 200 and response.json()["state"] == "executed"
    assert browser.post("/chat/confirm", headers=headers, json=data).status_code == 409
    assert browser.get("/mutation_confirmation.js").status_code == 200
    assert site.writes == [("PUT", "ToDo", "TASK-1", {"description": "After"}, STAMP)]
