import pytest

from app.core.frappe_client import FrappeError
from app.core.security import UserContext
from app.tools.hr import profile
from app.tools.registry import run_tool

CTX = UserContext("u@x.com", "HR-EMP-00001")

DOC = {
    "name": "HR-EMP-00001", "employee_name": "Rahim Uddin ", "department": "IT",
    "designation": None, "branch": "", "cell_number": "01711000000",
    "blood_group": "  ", "ctc": 0.0, "bank_ac_no": "123456",
    "owner": "Administrator", "creation": "2026-01-01", "docstatus": 1, "_comments": "x",
    "education": [{
        "name": "abc123", "parent": "HR-EMP-00001", "parentfield": "education",
        "parenttype": "Employee", "idx": 1, "doctype": "Employee Education",
        "school_univ": "BRAC University ", "qualification": "BSc in CSE",
        "level": "Graduate", "year_of_passing": "2026", "class_per": "",
        "maj_opt_subj": None,
    }],
    "external_work_history": [],
}


class FakeFrappe:
    asked = None

    def get_doc(self, doctype, name):
        FakeFrappe.asked = (doctype, name)
        return DOC


class MissingRecord:
    def get_doc(self, doctype, name):
        raise FrappeError(404, "not found")


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setattr(profile, "get_frappe_client", lambda: FakeFrappe())


def result():
    return run_tool("get_my_profile", {}, CTX)["result"]


def test_filled_fields_are_returned_and_trimmed():
    assert result()["details"]["employee_name"] == "Rahim Uddin"
    assert result()["details"]["department"] == "IT"


def test_empty_fields_and_empty_tables_are_reported_as_no_information():
    res = result()
    assert set(res["no_information_on_record"]) == {
        "designation", "branch", "blood_group", "ctc", "external_work_history"}
    assert "designation" not in res["details"]


def test_education_table_is_returned_without_housekeeping_columns():
    assert result()["details"]["education"] == [{
        "school_univ": "BRAC University", "qualification": "BSc in CSE",
        "level": "Graduate", "year_of_passing": "2026"}]


def test_system_fields_are_hidden():
    assert not ({"owner", "creation", "docstatus", "_comments"} & set(result()["details"]))


def test_confidential_values_reach_the_agent_which_masks_them():
    assert result()["details"]["cell_number"] == "01711000000"
    assert result()["details"]["bank_ac_no"] == "123456"


def test_only_the_callers_own_record_is_requested():
    run_tool("get_my_profile", {"employee_id": "HR-EMP-00002"}, CTX)
    assert FakeFrappe.asked == ("Employee", "HR-EMP-00001")


def test_a_missing_record_gives_an_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(profile, "get_frappe_client", lambda: MissingRecord())
    assert "error" in run_tool("get_my_profile", {}, CTX)