import datetime
import json
import sys

from app.core.frappe_client import FrappeError, get_frappe_client

EMP = sys.argv[1] if len(sys.argv) > 1 else "HR-EMP-00004"
frappe = get_frappe_client()


def show(label, fn):
    print(f"\n--- {label}")
    try:
        print(json.dumps(fn(), indent=2, ensure_ascii=False, default=str))
    except FrappeError as err:
        print("FAILED:", err)


show("employee (reports_to check)", lambda: frappe.get_list(
    "Employee", ["name", "employee_name", "reports_to", "user_id"], limit=10))
show("leave details (balance method)", lambda: frappe.call_method(
    "hrms.hr.doctype.leave_application.leave_application.get_leave_details",
    {"employee": EMP, "date": datetime.date.today().isoformat()}))
show("leave allocations", lambda: frappe.get_list(
    "Leave Allocation", ["*"], filters={"employee": EMP, "docstatus": 1}, limit=3))
show("leave applications", lambda: frappe.get_list(
    "Leave Application", ["*"], filters={"employee": EMP}, limit=3))
show("attendance", lambda: frappe.get_list(
    "Attendance", ["*"], filters={"employee": EMP, "docstatus": 1},
    order_by="attendance_date desc", limit=5))