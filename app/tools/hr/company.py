import calendar
import datetime
from collections import Counter, defaultdict

from app.core.frappe_client import get_frappe_client
from app.core.security import ToolInputError, UserContext
from app.tools.common import resolve_range, to_int
from app.tools.registry import tool

MAX_ROWS = 2000
METRICS = ["total_hr_cost", "expense_claims", "payroll_cost", "headcount", "leave_taken"]


def _period(year, month, from_date, to_date):
    """Default is the current month, as in 'this month'."""
    if from_date or to_date:
        return resolve_range(None, from_date, to_date, max_days=366)
    today = datetime.date.today()
    y = to_int(year, "year", 2000, 2100)
    m = to_int(month, "month", 1, 12)
    if y and not m:
        return datetime.date(y, 1, 1), datetime.date(y, 12, 31)
    y, m = y or today.year, m or today.month
    return datetime.date(y, m, 1), datetime.date(y, m, calendar.monthrange(y, m)[1])


def _rows(doctype, fields, filters=None, order_by=None):
    rows = get_frappe_client().get_all(doctype, fields, filters=filters,
                                       order_by=order_by, max_rows=MAX_ROWS)
    if len(rows) >= MAX_ROWS:
        raise ToolInputError("That period has too many records to total. "
                             "Please choose a shorter period.")
    return rows


def _number(row, key) -> float:
    return float(row.get(key) or 0)


def _expense_claims(start, end) -> dict:
    rows = _rows("Expense Claim", ["*"],
                 [["docstatus", "=", 1], ["posting_date", ">=", start.isoformat()],
                  ["posting_date", "<=", end.isoformat()]], "posting_date asc")
    counted = [r for r in rows if str(r.get("approval_status", "")).lower() != "rejected"]
    by_department = defaultdict(lambda: [0, 0.0])
    for r in counted:
        entry = by_department[r.get("department") or "No department"]
        entry[0] += 1
        entry[1] += _number(r, "total_sanctioned_amount")
    departments = sorted(by_department.items(), key=lambda kv: -kv[1][1])[:15]
    return {
        "claims_counted": len(counted),
        "rejected_not_counted": len(rows) - len(counted),
        "sanctioned_amount": round(sum(_number(r, "total_sanctioned_amount") for r in counted), 2),
        "claimed_amount": round(sum(_number(r, "total_claimed_amount") for r in counted), 2),
        "by_department": [{"department": d, "claims": n, "amount": round(a, 2)}
                          for d, (n, a) in departments],
    }


def _payslip_totals(start, end) -> dict:
    rows = _rows("Salary Slip", ["*"],
                 [["docstatus", "=", 1], ["start_date", ">=", start.isoformat()],
                  ["start_date", "<=", end.isoformat()]], "start_date asc")
    by_department = defaultdict(float)
    for r in rows:
        by_department[r.get("department") or "No department"] += _number(r, "gross_pay")
    currencies = sorted({r["currency"] for r in rows if r.get("currency")})
    out = {
        "payslips_counted": len(rows),
        "employees_paid": len({r.get("employee") for r in rows if r.get("employee")}),
        "gross_pay": round(sum(_number(r, "gross_pay") for r in rows), 2),
        "total_deduction": round(sum(_number(r, "total_deduction") for r in rows), 2),
        "net_pay": round(sum(_number(r, "net_pay") for r in rows), 2),
        "currency": currencies[0] if len(currencies) == 1 else None,
        "by_department": [{"department": d, "gross_pay": round(v, 2)}
                          for d, v in sorted(by_department.items(), key=lambda kv: -kv[1])[:15]],
    }
    if len(currencies) > 1:
        out["note"] = "More than one currency was used; amounts are added without conversion."
    return out


def _headcount() -> dict:
    rows = _rows("Employee", ["name", "status", "department"])
    active = Counter(r.get("department") or "No department"
                     for r in rows if r.get("status") == "Active")
    return {
        "employees_total": len(rows),
        "by_status": [{"status": s, "count": n}
                      for s, n in Counter(r.get("status") or "Unknown" for r in rows).items()],
        "active_by_department": [{"department": d, "count": n} for d, n in active.most_common()],
    }


def _leave_taken(start, end) -> dict:
    rows = _rows("Leave Application", ["*"],
                 [["docstatus", "=", 1], ["status", "=", "Approved"],
                  ["from_date", "<=", end.isoformat()], ["to_date", ">=", start.isoformat()]])
    days = defaultdict(float)
    for r in rows:
        days[r.get("leave_type") or "Unknown"] += _number(r, "total_leave_days")
    return {
        "approved_applications": len(rows),
        "employees_on_leave": len({r.get("employee") for r in rows if r.get("employee")}),
        "days_by_leave_type": [{"leave_type": t, "days": round(d, 2)} for t, d in days.items()],
        "note": "Whole applications that overlap the period are counted, not prorated.",
    }


@tool(
    name="company_summary",
    description=(
        "Company-wide figures for HR only. metric: total_hr_cost (approved expense "
        "claims plus salary slips), expense_claims, payroll_cost, headcount (current "
        "employees by status and department), or leave_taken (approved leave days by "
        "type). The period defaults to the current month; give year and month, or "
        "from_date and to_date, for another. Money amounts come back as placeholders; "
        "use calculate to combine them."
    ),
    parameters={"type": "object", "properties": {
        "metric": {"type": "string", "enum": METRICS, "description": "What to total."},
        "year": {"type": "integer", "description": "Calendar year, e.g. 2026."},
        "month": {"type": "integer", "description": "Month number 1-12."},
        "from_date": {"type": "string", "description": "Start date, YYYY-MM-DD."},
        "to_date": {"type": "string", "description": "End date, YYYY-MM-DD."},
    }, "required": ["metric"]},
    allow=("*",),    # amounts are masked by the agent before the model sees them
    hr_only=True,    # enforced centrally in run_tool
)
def company_summary(ctx: UserContext, metric=None, year=None, month=None,
                    from_date=None, to_date=None) -> dict:
    if metric not in METRICS:
        raise ToolInputError(f"metric must be one of: {', '.join(METRICS)}.")
    start, end = _period(year, month, from_date, to_date)
    out: dict = {"from_date": start.isoformat(), "to_date": end.isoformat()}

    if metric in ("total_hr_cost", "expense_claims"):
        out["expense_claims"] = _expense_claims(start, end)
    if metric in ("total_hr_cost", "payroll_cost"):
        out["payslip_totals"] = _payslip_totals(start, end)
    if metric == "total_hr_cost":
        out["total_cost_amount"] = round(out["expense_claims"]["sanctioned_amount"]
                                         + out["payslip_totals"]["gross_pay"], 2)
        out["includes"] = ("approved expense claims (sanctioned amounts) plus the gross pay "
                           "of submitted salary slips. Accounting ledger expenses such as "
                           "rent, utilities or purchases are not included.")
    if metric == "headcount":
        out["headcount"] = _headcount()
    if metric == "leave_taken":
        out["leave_taken"] = _leave_taken(start, end)
    return out
