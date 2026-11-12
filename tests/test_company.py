import pytest

from app.core import security
from app.core.frappe_client import FrappeError
from app.core.masking import Vault
from app.core.security import UserContext
from app.tools import registry
from app.tools.hr import company
from app.tools.registry import run_tool

HR = UserContext("hr@x.com", "HR-EMP-00004")
STAFF = UserContext("s@x.com", "HR-EMP-00001")


class FakeFrappe:
    def get_all(self, doctype, fields=None, filters=None, order_by=None, max_rows=2000):
        if doctype == "Expense Claim":
            return [
                {"approval_status": "Approved", "department": "IT",
                 "total_claimed_amount": 1200.0, "total_sanctioned_amount": 1000.0},
                {"approval_status": "Approved", "department": "HR",
                 "total_claimed_amount": 500.0, "total_sanctioned_amount": 500.0},
                {"approval_status": "Rejected", "department": "IT",
                 "total_claimed_amount": 9000.0, "total_sanctioned_amount": 0.0}]
        if doctype == "Salary Slip":
            return [
                {"employee": "E1", "department": "IT", "gross_pay": 60000.0,
                 "total_deduction": 6000.0, "net_pay": 54000.0, "currency": "BDT"},
                {"employee": "E2", "department": "HR", "gross_pay": 40000.0,
                 "total_deduction": 4000.0, "net_pay": 36000.0, "currency": "BDT"}]
        if doctype == "Employee":
            return [{"name": "E1", "status": "Active", "department": "IT"},
                    {"name": "E2", "status": "Active", "department": "HR"},
                    {"name": "E3", "status": "Left", "department": "IT"}]
        if doctype == "Leave Application":
            return [{"employee": "E1", "leave_type": "Sick Leave", "total_leave_days": 2.0},
                    {"employee": "E2", "leave_type": "Sick Leave", "total_leave_days": 1.0},
                    {"employee": "E1", "leave_type": "Casual Leave", "total_leave_days": 1.0}]
        return []


@pytest.fixture
def as_hr(monkeypatch):
    monkeypatch.setattr(registry, "is_hr_admin", lambda ctx: True)
    monkeypatch.setattr(company, "get_frappe_client", lambda: FakeFrappe())


def ask(**args):
    return run_tool("company_summary", {"year": 2026, "month": 11, **args}, HR)["result"]


def test_total_hr_cost_adds_claims_and_payroll(as_hr):
    res = ask(metric="total_hr_cost")
    assert res["expense_claims"]["sanctioned_amount"] == 1500.0
    assert res["expense_claims"]["claimed_amount"] == 1700.0
    assert res["expense_claims"]["rejected_not_counted"] == 1
    assert res["payslip_totals"]["gross_pay"] == 100000.0
    assert res["total_cost_amount"] == 101500.0
    assert (res["from_date"], res["to_date"]) == ("2026-11-01", "2026-11-30")


def test_headcount_and_leave_taken(as_hr):
    head = ask(metric="headcount")["headcount"]
    assert head["employees_total"] == 3
    assert {"department": "IT", "count": 1} in head["active_by_department"]
    leave = ask(metric="leave_taken")["leave_taken"]
    assert {"leave_type": "Sick Leave", "days": 3.0} in leave["days_by_leave_type"]
    assert leave["employees_on_leave"] == 2


def test_only_hr_may_ask(monkeypatch):
    monkeypatch.setattr(registry, "is_hr_admin", lambda ctx: False)
    monkeypatch.setattr(company, "get_frappe_client", lambda: pytest.fail("must not reach Frappe"))
    assert run_tool("company_summary", {"metric": "total_hr_cost"}, STAFF) == {
        "error": "This information is available to HR only."}


def test_the_model_sees_no_amounts(as_hr):
    full = run_tool("company_summary", {"metric": "total_hr_cost", "year": 2026, "month": 11}, HR)
    seen = str(Vault().mask(full))
    for secret in ("1500", "1700", "100000", "10000", "90000", "101500"):
        assert secret not in seen
    assert "IT" in seen and "claims_counted" in seen      # counts and names stay readable

def test_hr_admin_by_allowlist(monkeypatch):
    monkeypatch.setattr(security.settings, "hr_admin_employees", "HR-EMP-00004, HR-EMP-00009")
    assert security.is_hr_admin(UserContext("x", "HR-EMP-00009")) is True
    assert security.is_hr_admin(UserContext("x", "HR-EMP-00001")) is False


def test_hr_admin_by_role(monkeypatch):
    monkeypatch.setattr(security.settings, "hr_admin_employees", "")
    assert security.is_hr_admin(UserContext("x", "E1", ("Employee", "HR Manager"))) is True
    assert security.is_hr_admin(UserContext("x", "E1", ("hr user",))) is True
    assert security.is_hr_admin(UserContext("x", "E1", ("Employee",))) is False
    assert security.is_hr_admin(UserContext("x", "E1")) is False      # roles unknown: no access