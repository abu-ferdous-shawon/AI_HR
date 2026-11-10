import pytest

from app.core.frappe_client import FrappeError, get_frappe_client
from app.core.security import AccessDenied, UserContext, get_team, resolve_context
from app.tools.registry import run_tool

pytestmark = pytest.mark.integration

EMP1 = UserContext("test-only", "HR-EMP-00001")
JAN = {"from_date": "2026-01-25", "to_date": "2026-01-29"}


@pytest.fixture(scope="module", autouse=True)
def frappe_is_up():
    try:
        get_frappe_client().call_method("frappe.auth.get_logged_user")
    except FrappeError as exc:
        pytest.skip(f"Frappe is not reachable: {exc}")


def test_known_login_maps_to_one_employee():
    assert resolve_context("test@mail.com").employee_id == "HR-EMP-00004"


def test_unlinked_login_is_refused():
    with pytest.raises(AccessDenied):
        resolve_context("nobody@example.com")


def test_leave_balance_has_the_expected_shape():
    res = run_tool("get_my_leave_balance", {}, EMP1)["result"]
    by_type = {b["leave_type"]: b["remaining"] for b in res["balances"]}
    assert by_type == {"Annual Leave": 15.0, "Casual Leave": 15.0, "Sick Leave": 10.0}


def test_own_attendance_summary():
    res = run_tool("get_attendance", JAN, EMP1)["result"]
    assert res["summary"] == {"Present": 4, "Absent": 1}


def test_profile_is_complete_and_own_data_only():
    mine = run_tool("get_my_profile", {}, EMP1)["result"]
    other = run_tool("get_my_profile", {"employee_id": "HR-EMP-00002"}, EMP1)["result"]
    assert mine["details"]["name"] == "HR-EMP-00001"
    assert other["details"]["name"] == "HR-EMP-00001"  # the other ID was ignored
    assert not ({"owner", "creation", "modified", "docstatus"} & set(mine["details"]))
    assert isinstance(mine["no_information_on_record"], list)


def test_manager_reads_a_report_but_not_an_outsider():
    manager = resolve_context("test@mail.com")
    team = get_team(manager)
    if not team:
        pytest.skip("Set 'Reports To' on an employee to HR-EMP-00004 in Frappe first")
    ok = run_tool("get_attendance", {"employee_id": team[0]["name"], **JAN}, manager)
    assert "result" in ok
    outsiders = {"HR-EMP-00001", "HR-EMP-00002", "HR-EMP-00003"} - {m["name"] for m in team}
    refused = run_tool("get_attendance", {"employee_id": sorted(outsiders)[0], **JAN}, manager)
    assert "error" in refused