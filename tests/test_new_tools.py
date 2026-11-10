import datetime
from app.core.masking import Vault
from app.core.security import UserContext
from app.tools.hr import holidays, leave, payroll
from app.tools.registry import run_tool

CTX = UserContext("u@x.com", "HR-EMP-00001")


class FakeHolidays:
    def get_list(self, doctype, fields=None, filters=None, order_by=None, limit=20, start=0):
        return [{"name": "HR-EMP-00001", "holiday_list": "BD 2026", "company": "Starkology"}]

    def get_doc(self, doctype, name):
        return {"holidays": [
            {"holiday_date": "2026-02-21", "description": "Language Martyrs Day", "weekly_off": 0},
            {"holiday_date": "2026-02-27", "description": "Friday", "weekly_off": 1},
            {"holiday_date": "2026-12-16", "description": "Victory Day", "weekly_off": 0}]}


def test_holidays_hide_weekly_offs_and_respect_the_range(monkeypatch):
    monkeypatch.setattr(holidays, "get_frappe_client", lambda: FakeHolidays())
    res = run_tool("get_holidays", {"year": 2026, "to_date": "2026-03-31",
                                    "from_date": "2026-01-01"}, CTX)["result"]
    assert [h["holiday"] for h in res["holidays"]] == ["Language Martyrs Day"]
    both = run_tool("get_holidays", {"from_date": "2026-01-01", "to_date": "2026-03-31",
                                     "include_weekly_off": True}, CTX)["result"]
    assert len(both["holidays"]) == 2


class FakeLeave:
    filters = None

    def get_all(self, doctype, fields=None, filters=None, order_by=None, max_rows=2000):
        FakeLeave.filters = filters
        return [
            {"name": "LAP-1", "leave_type": "Sick Leave", "from_date": "2026-03-01",
             "to_date": "2026-03-02", "total_leave_days": 2.0, "status": "Approved",
             "posting_date": "2026-02-28", "description": "=cmd", "owner": "someone"},
            {"name": "LAP-2", "leave_type": "Casual Leave", "from_date": "2026-04-01",
             "to_date": "2026-04-01", "total_leave_days": 1.0, "status": "Open"}]


def test_leave_history_is_own_data_with_totals(monkeypatch):
    monkeypatch.setattr(leave, "get_frappe_client", lambda: FakeLeave())
    res = run_tool("get_my_leave_history", {"year": 2026, "employee_id": "HR-EMP-00002"}, CTX)["result"]
    assert ["employee", "=", "HR-EMP-00001"] in FakeLeave.filters   # the other ID was ignored
    assert res["count"] == 2 and "owner" not in res["applications"][0]
    assert res["approved_days_by_leave_type"] == {"Sick Leave": 2.0}
    only = run_tool("get_my_leave_history", {"year": 2026, "status": "open"}, CTX)["result"]
    assert only["count"] == 1


class FakePayroll:
    filters = None

    def get_all(self, doctype, fields=None, filters=None, order_by=None, max_rows=2000):
        FakePayroll.filters = filters
        return [{"name": "SAL-1", "start_date": "2026-03-01", "end_date": "2026-03-31",
                 "currency": "BDT", "gross_pay": 55000.0, "total_deduction": 5000.0,
                 "net_pay": 50000.0, "status": "Submitted"}]

    def get_doc(self, doctype, name):
        return {"employee": "HR-EMP-00001",
                "earnings": [{"salary_component": "Basic Salary", "amount": 55000.0}],
                "deductions": [{"salary_component": "Tax", "amount": 5000.0}]}


def test_payslips_are_own_data_and_amounts_are_masked_for_the_model(monkeypatch):
    monkeypatch.setattr(payroll, "get_frappe_client", lambda: FakePayroll())
    res = run_tool("get_my_payslips", {"year": 2026, "month": 3}, CTX)
    assert ["employee", "=", "HR-EMP-00001"] in FakePayroll.filters
    assert res["result"]["payslips"][0]["breakdown"]["earned"][0]["amount"] == 55000.0

    seen_by_model = str(Vault().mask(res))
    for secret in ("55000", "5000", "50000"):
        assert secret not in seen_by_model
    assert "Basic Salary" in seen_by_model and "BDT" in seen_by_model

def test_a_list_with_only_weekly_offs_still_answers_next_holiday(monkeypatch):
    today = datetime.date.today()
    rows = []
    for i in range(30):
        day = today + datetime.timedelta(days=i)
        if day.weekday() in (4, 5):                       # Friday and Saturday
            rows.append({"holiday_date": day.isoformat(), "description": "", "weekly_off": 1})

    class OnlyWeekends(FakeHolidays):
        def get_doc(self, doctype, name):
            return {"holidays": rows}

    monkeypatch.setattr(holidays, "get_frappe_client", lambda: OnlyWeekends())
    res = run_tool("get_holidays", {"from_date": today.isoformat(),
                                    "to_date": (today + datetime.timedelta(days=29)).isoformat()},
                   CTX)["result"]
    assert res["holidays"] == []                          # no public holidays
    assert res["weekly_off_days"] == ["Friday", "Saturday"]
    assert res["next_public_holiday"] is None
    assert res["next_weekly_off"]["date"] == rows[0]["holiday_date"]
    assert res["weekly_off_count"] == len(rows)