import pytest

from app.core import masking
from app.core.frappe_client import FrappeError
from app.records import engine
from app.registry import get_profile
from app.tools import registry
from app.tools.registry import run_tool
from tests.records_helpers import ME, World, leave_row


@pytest.fixture
def world(monkeypatch):
    saved = set(masking.EXTRA_CONFIDENTIAL)
    w = World(monkeypatch)
    monkeypatch.setattr(engine, "get_profile", lambda: w.profile)
    yield w
    masking.EXTRA_CONFIDENTIAL.clear()
    masking.EXTRA_CONFIDENTIAL.update(saved)


def spec(name):
    return next(t for t in registry.get_tool_specs() if t["name"] == name)


def props(name):
    return set(spec(name)["parameters"]["properties"])


def test_both_tools_have_exactly_the_agreed_inputs():
    assert props("get_employee_records") == {
        "record_type", "from_date", "to_date", "status", "scope", "employee", "limit"}
    assert props("summarize_employee_records") == {
        "record_type", "aggregation", "from_date", "to_date", "status", "scope", "group_by"}
    for name in ("get_employee_records", "summarize_employee_records"):
        assert not ({"employee_id", "fields", "filters", "doctype"} & props(name))


def test_record_types_come_from_the_profile():
    enum = spec("get_employee_records")["parameters"]["properties"]["record_type"]["enum"]
    assert enum == list(get_profile().enabled_types())
    assert "salary_slip" in spec("get_employee_records")["description"]
    assert "total_net" in spec("summarize_employee_records")["description"]


def test_the_profiles_confidential_names_are_masked():
    for name in ("interview_summary", "base", "final_score", "total_receivable_amount"):
        assert masking.is_confidential(name), name
    assert not masking.is_confidential("employee_name")


def test_empty_denied_and_failed_are_three_different_answers(world):
    empty = run_tool("get_employee_records", {"record_type": "leave"}, ME)
    assert empty["result"]["outcome"] == "no_records" and "error" not in empty
    denied = run_tool("get_employee_records", {"record_type": "leave", "scope": "company"}, ME)
    assert "HR only" in denied["error"]
    world.error = FrappeError(500, "boom")
    failed = run_tool("get_employee_records", {"record_type": "leave"}, ME)
    assert failed == {"error": "The HR system could not complete this request."}


def test_the_model_cannot_pick_an_employee_id(world):
    world.rows["Leave Thing"] = [leave_row("L1")]
    res = run_tool("get_employee_records",
                   {"record_type": "leave", "employee_id": "EMP-9", "filters": [["x", "=", "y"]]}, ME)
    assert res["result"]["outcome"] == "records"
    assert world.filters == [["employee", "=", "EMP-1"], ["docstatus", "in", [0, 1]]]


def test_unknown_types_and_summaries_through_the_tools(world):
    assert "Unknown record type" in run_tool(
        "get_employee_records", {"record_type": "weather"}, ME)["error"]
    world.rows["Leave Thing"] = [leave_row("L1", days=2.0), leave_row("L2", days=1.5)]
    res = run_tool("summarize_employee_records",
                   {"record_type": "leave", "aggregation": "total_days"}, ME)
    assert res["result"]["value"] == 3.5