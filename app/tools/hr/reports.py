from app.core.frappe_client import get_frappe_client
from app.core.reports import save_report
from app.core.security import (
    AccessDenied, ToolInputError, UserContext, get_team, require_access,
)
from app.tools.common import resolve_range
from app.tools.hr.holidays import fetch_holidays
from app.tools.hr.leave import fetch_leave_balance, fetch_leave_history
from app.tools.hr.payroll import fetch_payslips
from app.tools.registry import tool

MAX_ROWS = 2000


# Each builder returns (title, column names, rows). To add a new kind of
# report, write one more builder like these and list it in REPORTS below.

def _attendance(ctx: UserContext, p: dict):
    start, end = resolve_range(p["year"], p["from_date"], p["to_date"], default="30days")
    if p["scope"] == "team":
        team = get_team(ctx)
        if not team:
            raise AccessDenied("You have no direct reports, so there is no team report.")
        who = ["employee", "in", [m["name"] for m in team]]
    else:
        who = ["employee", "=", p["target"]]
    rows = get_frappe_client().get_all(
        "Attendance",
        ["employee", "employee_name", "attendance_date", "status",
         "leave_type", "late_entry", "early_exit"],
        filters=[who, ["docstatus", "=", 1],
                 ["attendance_date", ">=", start.isoformat()],
                 ["attendance_date", "<=", end.isoformat()]],
        order_by="employee asc, attendance_date asc", max_rows=MAX_ROWS)
    if len(rows) >= MAX_ROWS:
        raise ToolInputError("That is too many records. Please choose a shorter period.")
    yes_no = lambda v: "Yes" if v else "No"
    table = [[r["employee"], (r.get("employee_name") or "").strip(), r["attendance_date"],
              r["status"], r.get("leave_type") or "", yes_no(r.get("late_entry")),
              yes_no(r.get("early_exit"))] for r in rows]
    return (f"Attendance {start} to {end}",
            ["Employee ID", "Name", "Date", "Status", "Leave type", "Late entry", "Early exit"],
            table)


def _leave_history(ctx: UserContext, p: dict):
    start, end = resolve_range(p["year"], p["from_date"], p["to_date"])
    apps = fetch_leave_history(ctx, start, end)["applications"]
    table = [[a.get("application_id"), a.get("leave_type"), a.get("from_date"),
              a.get("to_date"), a.get("days"), a.get("status"), a.get("applied_on"),
              a.get("reason", "")] for a in apps]
    return (f"Leave history {start} to {end}",
            ["Application", "Leave type", "From", "To", "Days", "Status", "Applied on", "Reason"],
            table)


def _leave_balance(ctx: UserContext, p: dict):
    data = fetch_leave_balance(ctx)
    table = [[b["leave_type"], b["total"], b["taken"], b["pending_approval"], b["remaining"]]
             for b in data["balances"]]
    return (f"Leave balance as of {data['as_of']}",
            ["Leave type", "Total", "Taken", "Pending approval", "Remaining"], table)


def _holidays(ctx: UserContext, p: dict):
    start, end = resolve_range(p["year"], p["from_date"], p["to_date"])
    items = fetch_holidays(ctx, start, end, include_weekly_off=True, limit=500)["holidays"]
    table = [[h["date"], h["weekday"], h["holiday"], "Yes" if h["weekly_off"] else "No"]
             for h in items]
    return (f"Holidays {start} to {end}", ["Date", "Day", "Holiday", "Weekly off"], table)


def _payslips(ctx: UserContext, p: dict):
    start, end = resolve_range(p["year"], p["from_date"], p["to_date"])
    slips = fetch_payslips(ctx, start, end)["payslips"]
    table = [[s.get("period_start"), s.get("period_end"), s.get("currency"),
              s.get("gross_pay"), s.get("total_deduction"), s.get("net_pay"),
              s.get("status")] for s in slips]
    return (f"Salary slips {start} to {end}",
            ["Period start", "Period end", "Currency", "Gross pay",
             "Total deduction", "Net pay", "Status"], table)


REPORTS = {
    "attendance": _attendance, "leave_history": _leave_history,
    "leave_balance": _leave_balance, "holidays": _holidays, "payslips": _payslips,
}


@tool(
    name="create_report",
    description=(
        "Create a downloadable Excel or CSV report file. report_type is one of: "
        "attendance (yours, or scope=team for all your direct reports), "
        "leave_history, leave_balance, holidays, payslips (yours only). Use it "
        "when the person asks for a report, a file, a spreadsheet, Excel or CSV. "
        "The file is made on the server; you only learn how many rows it has."
    ),
    parameters={"type": "object", "properties": {
        "report_type": {"type": "string", "enum": list(REPORTS),
                        "description": "Which report to create."},
        "file_format": {"type": "string", "enum": ["excel", "csv"],
                        "description": "Default excel."},
        "scope": {"type": "string", "enum": ["me", "team"],
                  "description": "Attendance only: 'team' covers all direct reports."},
        "employee_id": {"type": "string",
                        "description": "Attendance of one direct report. Leave out for your own."},
        "from_date": {"type": "string", "description": "Start date, YYYY-MM-DD."},
        "to_date": {"type": "string", "description": "End date, YYYY-MM-DD."},
        "year": {"type": "integer", "description": "Calendar year, e.g. 2026."},
    }, "required": ["report_type"]},
)
def create_report(ctx: UserContext, report_type=None, file_format=None, scope=None,
                  employee_id=None, from_date=None, to_date=None, year=None) -> dict:
    target = require_access(ctx, employee_id)  # access check FIRST, as for every tool

    if report_type not in REPORTS:
        raise ToolInputError(f"report_type must be one of: {', '.join(REPORTS)}.")
    fmt = (file_format or "excel").lower()
    if fmt not in ("excel", "csv"):
        raise ToolInputError("file_format must be excel or csv.")
    scope = (scope or "me").lower()
    if scope not in ("me", "team"):
        raise ToolInputError("scope must be me or team.")
    if (scope == "team" or target != ctx.employee_id) and report_type != "attendance":
        raise AccessDenied("Only attendance reports can cover other people.")

    title, columns, rows = REPORTS[report_type](ctx, {
        "target": target, "scope": scope, "year": year,
        "from_date": from_date, "to_date": to_date})
    if not rows:
        return {"message": "There are no records for that period, so no report was created."}
    return {"file": save_report(ctx.user_id, title, columns, rows, fmt),
            "message": f"Report '{title}' created with {len(rows)} rows."}