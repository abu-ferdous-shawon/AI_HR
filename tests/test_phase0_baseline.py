"""Phase 0 baseline: locks today's public behaviour before the Selling work starts. Offline."""
import pytest

from app import main
from app.core.security import AccessDenied
from app.registry import load_profile
from app.tools import registry
from tests.records_helpers import World, leave_row

BASE_TOOLS = {
    "calculate", "company_summary", "create_report", "get_attendance", "get_employee_records",
    "get_holidays", "get_my_leave_balance", "get_my_leave_history", "get_my_payslips",
    "get_my_profile", "list_my_team", "search_hr_policy", "show_image",
    "summarize_employee_records",
}


@pytest.fixture(scope="module")
def shipped():
    return load_profile("frappe_hrms_v16")


@pytest.fixture(scope="module")
def starkology():
    return load_profile("customer_profiles/starkology.yaml")


def test_profiles_load_with_35_types(shipped, starkology):
    assert len(shipped.enabled_types()) == 35
    assert len(starkology.enabled_types()) == 35


def test_multilingual_aliases_resolve(shipped, starkology):
    for profile in (shipped, starkology):
        assert profile.resolve("হাজিরা") == "attendance"
        assert profile.resolve("hajira") == "attendance"
        assert profile.resolve("payslip") == "salary_slip"


def test_starkology_overlay_removes_the_benefit_claim_date(shipped, starkology):
    assert shipped.get("employee_benefit_claim").date_field == "claim_date"
    assert starkology.get("employee_benefit_claim").date_field is None


def test_tool_names_baseline():
    assert {t["name"] for t in registry.get_tool_specs()} == BASE_TOOLS


def test_chat_response_contract():
    assert set(main.ChatResponse.model_fields) == {
        "answer", "conversation_id", "files", "pictures"}
    assert set(main.ChatRequest.model_fields) == {"message", "conversation_id", "images"}


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def test_employee_self_service_request(world):
    world.rows["Leave Thing"] = [leave_row("L1")]
    res = world.fetch("leave")
    assert res["outcome"] == "records" and res["filters_used"]["scope"] == "self"
    assert ["employee", "=", "EMP-1"] in world.filters


def test_manager_is_denied_team_payroll(world, shipped):
    world.profile = shipped
    world.team = [{"name": "EMP-2", "employee_name": "Rahim"}]
    with pytest.raises(AccessDenied, match="not available at the 'team' level"):
        world.fetch("salary_slip", scope="team")


def test_alias_lookup_through_the_engine(world, shipped):
    world.profile = shipped
    world.rows["Attendance"] = []
    assert world.fetch("হাজিরা")["outcome"] == "no_records"
    assert world.fetch("hajira")["record_type"] == "attendance"