import csv
import json
from types import SimpleNamespace

import pytest

from app.core import reports, sites
from app.core.agent import run_agent
from app.core.llm.base import LLMResponse, ToolCall
from app.auth.security import UserContext
from app.frappe_client.client import FrappeClient, FrappeError
from app.knowledge_packs.loader import KnowledgePackError, active_packs, load_packs
from app.knowledge_packs.redactor import current_policy
from app.tools.hr import profile

PACK = """
pack_name: local_guard
modules: [HR]
never_send:
  global_fields: [integration_code, _integration_code]
  doctype_fields:
    Employee: [custom_private_note]
    Employee Education: [custom_private_grade]
"""
DENIED_VALUE = "PRIVATE-SENTINEL-ONLY-FOR-THE-TEST"


@pytest.fixture
def guard(tmp_path, monkeypatch):
    (tmp_path / "local_guard.yaml").write_text(PACK)
    monkeypatch.setattr(sites.settings, "knowledge_pack_dir", str(tmp_path))
    site = sites.get_default_site().model_copy(update={"knowledge_packs": ["hrms_core", "local_guard"]})
    monkeypatch.setattr(sites, "_loaded", lambda: ({site.name: site}, site.name))
    return current_policy()


def test_full_packs_load_and_only_selected_packs_are_active(guard):
    hr, custom = active_packs()
    assert (hr.pack_name, custom.pack_name) == ("hrms_core", "local_guard")
    assert hr.overview.flows and hr.overview.supported_topics
    assert hr.doctypes["Salary Slip"].states[1] == "Submitted"
    assert hr.kpis["active headcount"].target_doctype == "Employee"
    assert hr.recommended_reports[0].name == "Monthly Attendance Sheet"
    assert hr.master_hints and hr.examples[0].target_doctype == "Leave Application"
    assert guard.blocked("custom_private_note", "EMPLOYEE")
    assert not guard.blocked("custom_private_note", "Leave Application")
    assert guard.blocked("integration_code", "Leave Application")
    assert guard.blocked("_integration_code", "Leave Application")
    assert [pack.pack_name for pack in load_packs(["erpnext_core", "custom_app_example"])] == [
        "erpnext_core", "custom_app_example",
    ]


@pytest.mark.parametrize("bad_yaml", [
    "never_send: {}\nnever_send: {}\n",
    "never_send:\n  global_fileds: [secret]\n",
    "never_send:\n  global_fields: broken\n",
])
def test_invalid_policy_is_rejected(tmp_path, bad_yaml):
    (tmp_path / "bad.yaml").write_text(f"pack_name: bad\n{bad_yaml}")
    with pytest.raises(KnowledgePackError, match="bad.yaml"):
        load_packs(["bad"], str(tmp_path))


def test_list_projection_removes_blocked_fields_before_network_and_filters_response(guard, monkeypatch):
    client = FrappeClient()
    requests = []

    def fetch(path, params):
        requests.append(json.loads(params["fields"]))
        return {"data": [{"name": "E1", "custom_private_note": DENIED_VALUE,
                          "integration_code": DENIED_VALUE}]}

    monkeypatch.setattr(client, "_get", fetch)
    monkeypatch.setattr(client, "get_meta", lambda doctype: [{
        "name": doctype, "fields": [
            {"fieldname": "employee_name", "fieldtype": "Data"},
            {"fieldname": "custom_private_note", "fieldtype": "Small Text"},
            {"fieldname": "integration_code", "fieldtype": "Data"},
            {"fieldname": "custom_password", "fieldtype": "Password"},
            {"fieldname": "virtual_name", "fieldtype": "Data", "is_virtual": 1},
        ],
    }])
    assert client.get_list("Employee", ["name", "custom_private_note"]) == [{"name": "E1"}]
    assert requests[0] == ["name"]
    assert client.get_all("Employee", ["*"]) == [{"name": "E1"}]
    assert "employee_name" in requests[1]
    assert not {"*", "custom_private_note", "integration_code", "custom_password", "virtual_name"} & set(requests[1])
    with pytest.raises(FrappeError, match="filter or sort"):
        client.get_list("Employee", ["name"], filters={"custom_private_note": DENIED_VALUE})
    with pytest.raises(FrappeError, match="filter or sort"):
        client.get_list("Employee", ["name"], order_by="custom_private_note desc")
    with pytest.raises(FrappeError, match="simple field names"):
        client.get_list("Employee", ["custom_private_note as details"])
    assert len(requests) == 2


def test_full_document_is_filtered_before_the_tool_model_history_or_answer(guard, monkeypatch):
    client = FrappeClient()
    doc = {
        "name": "E1", "employee_name": "Example Employee",
        "bank_ac_no": DENIED_VALUE, "integration_code": DENIED_VALUE,
        "custom_private_note": DENIED_VALUE,
        "education": [{"doctype": "Employee Education", "parenttype": "Employee",
                       "parentfield": "education", "qualification": "BSc",
                       "custom_private_grade": DENIED_VALUE}],
    }
    monkeypatch.setattr(client, "_get", lambda path: {"data": doc})
    monkeypatch.setattr(profile, "get_frappe_client", lambda: client)
    assert client.get_doc("Employee", "E1", ["name", "bank_ac_no"]) == {"name": "E1"}
    assert client.get_doc("Employee", "E1")["education"] == [
        {"doctype": "Employee Education", "parenttype": "Employee",
         "parentfield": "education", "qualification": "BSc"},
    ]

    class Model:
        def generate(self, system, messages, tools):
            model_input = repr((system, messages, tools))
            assert DENIED_VALUE not in model_input
            assert "custom_private_note" not in model_input
            assert "custom_private_grade" not in model_input
            if messages[-1].role == "user":
                return LLMResponse(tool_calls=[ToolCall("get_my_profile", {})])
            assert "Example Employee" in model_input and "BSc" in model_input
            return LLMResponse(text="Your profile is for Example Employee.")

    result = run_agent("Show my profile", UserContext("u", "E1"), llm=Model())
    assert result.answer == "Your profile is for Example Employee."
    assert DENIED_VALUE not in repr(result)


def test_report_columns_dictionary_aliases_and_downloads_are_redacted(guard, monkeypatch, tmp_path):
    client = FrappeClient()
    raw = {
        "columns": ["Employee:Data:100", {"fieldname": "masked_alias", "label": "Custom Private Note"}],
        "result": [["E1", DENIED_VALUE], {"employee": "E2", "masked_alias": DENIED_VALUE}],
        "chart": {"labels": [DENIED_VALUE]}, "report_summary": [{"value": DENIED_VALUE}],
    }
    monkeypatch.setattr(client, "_get", lambda path, params: {"message": raw})
    report = client.call_method("frappe.desk.query_report.run", {"report_name": "Custom Report"})
    assert report == {"columns": ["Employee:Data:100"], "result": [["E1"], {"employee": "E2"}]}

    monkeypatch.setattr(reports, "REPORT_DIR", tmp_path / "report_output")
    file = reports.save_report(
        "u", "Test", ["Employee", "Note"], [["E1", DENIED_VALUE]], "csv",
        source_doctype="Employee", column_fields=("name", "custom_private_note"),
    )
    path = reports.get_report(file["id"], "u").path
    with path.open(encoding="utf-8-sig") as handle:
        assert list(csv.reader(handle)) == [["Employee"], ["E1"]]


def test_frappe_error_payload_is_not_exposed(guard, monkeypatch, caplog):
    client = FrappeClient()
    response = SimpleNamespace(status_code=500, json=lambda: {"exception": DENIED_VALUE})
    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: response)
    monkeypatch.setattr(profile, "get_frappe_client", lambda: client)

    class Model:
        def generate(self, system, messages, tools):
            if messages[-1].role == "user":
                return LLMResponse(tool_calls=[ToolCall("get_my_profile", {})])
            return LLMResponse(text="Could not load your profile.")

    result = run_agent("Show my profile", UserContext("u", "E1"), llm=Model())
    assert "HTTP 500" in caplog.text
    assert DENIED_VALUE not in caplog.text + repr(result)
