import datetime
import sys

from app.core.frappe_client import FrappeError
from app.core.security import UserContext
from app.tools.hr.holidays import fetch_holidays
from app.tools.hr.leave import fetch_leave_history
from app.tools.hr.payroll import fetch_payslips

ctx = UserContext("manual", sys.argv[1] if len(sys.argv) > 1 else "HR-EMP-00001")
start, end = datetime.date(2026, 1, 1), datetime.date(2026, 12, 31)


def show(label, call, key):
    try:
        data = call()
        items = data[key]
        print(f"{label}: {len(items)} found; fields: "
              f"{sorted(items[0]) if items else 'none (no data to check)'}")
    except FrappeError as err:
        print(f"{label}: FAILED - {err}")


show("holidays", lambda: fetch_holidays(ctx, start, end, True), "holidays")
show("leave applications", lambda: fetch_leave_history(ctx, start, end), "applications")
show("salary slips", lambda: fetch_payslips(ctx, start, end, True), "payslips")