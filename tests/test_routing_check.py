from app.core.llm.base import ToolCall
from app.registry import get_profile
from app.tools import registry
from tests.manual.routing_check import CASES, describe, judge


def test_judge_checks_the_tool_and_the_arguments():
    expect = {"tools": ["get_employee_records"],
              "args": {"record_type": ["employee_promotion"], "scope": [None, "self"]}}
    assert judge(expect, [ToolCall("get_employee_records",
                                   {"record_type": "employee_promotion"})]) == (True, "")
    assert judge(expect, [ToolCall("get_employee_records", {
        "record_type": "Employee_Promotion", "scope": "SELF"})])[0]            # case does not matter
    wrong_type = judge(expect, [ToolCall("get_employee_records", {"record_type": "appraisal"})])
    assert wrong_type == (False, "called get_employee_records {'record_type': 'appraisal'}")
    assert not judge(expect, [ToolCall("get_employee_records", {
        "record_type": "employee_promotion", "scope": "company"})])[0]
    assert not judge(expect, [ToolCall("get_my_profile", {})])[0]
    assert judge(expect, []) == (False, "no tool was called")


def test_judge_supports_contains_and_any_matching_call():
    contains = {"tools": ["x"], "args": {"employee": "~rahim"}}
    assert judge(contains, [ToolCall("x", {"employee": "Rahim Uddin"})])[0]
    assert not judge(contains, [ToolCall("x", {})])[0]
    assert judge({"tools": ["b"]}, [ToolCall("a", {}), ToolCall("b", {})])[0]


def test_the_cases_only_name_real_tools_and_record_types():
    names = {t["name"] for t in registry.get_tool_specs()}
    keys = set(get_profile().enabled_types())
    assert len(CASES) >= 10
    for question, expect in CASES:
        assert set(expect["tools"]) <= names, question
        for kind in expect.get("args", {}).get("record_type", []):
            assert kind in keys, question
        assert describe(expect)