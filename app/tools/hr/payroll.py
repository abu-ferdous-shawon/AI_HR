import calendar
import datetime

from app.core.frappe_client import get_frappe_client
from app.core.security import UserContext
from app.tools.common import to_int
from app.tools.registry import tool

SLIP_FIELDS = {
    "name": "slip_id", "start_date": "period_start", "end_date": "period_end",
    "currency": "currency", "gross_pay": "gross_pay",
    "total_deduction": "total_deduction", "net_pay": "net_pay", "status": "status",
}


def fetch_payslips(ctx: UserContext, start: datetime.date, end: datetime.date,
                   with_breakdown: bool = False) -> dict:
    """The caller's own submitted salary slips whose period starts inside the range."""
    frappe = get_frappe_client()
    rows = frappe.get_all(
        "Salary Slip", ["*"],
        filters=[
            ["employee", "=", ctx.employee_id],      # always the caller's own ID
            ["docstatus", "=", 1],                   # submitted slips only
            ["start_date", ">=", start.isoformat()],
            ["start_date", "<=", end.isoformat()],
        ],
        order_by="start_date asc", max_rows=60,
    )
    slips = [{target: row[source] for source, target in SLIP_FIELDS.items()
              if row.get(source) not in (None, "")} for row in rows]

    if with_breakdown:
        for slip in slips:
            doc = frappe.get_doc("Salary Slip", slip["slip_id"])
            if doc.get("employee") != ctx.employee_id:   # defence in depth
                continue

            def lines(key):
                return [{"component": r.get("salary_component"), "amount": r.get("amount")}
                        for r in doc.get(key) or []]
            slip["breakdown"] = {"earned": lines("earnings"), "deducted": lines("deductions")}
    return {"payslips": slips}


@tool(
    name="get_my_payslips",
    description=(
        "Get the logged-in person's own salary slips for a year (default: this "
        "year) or one month of a year. With a month, each slip also includes its "
        "earnings and deductions breakdown. Amounts are confidential."
    ),
    parameters={"type": "object", "properties": {
        "year": {"type": "integer", "description": "Calendar year, e.g. 2026."},
        "month": {"type": "integer", "description": "Month number 1-12."},
    }},
    allow=("*",),  # amounts are masked by the agent before the model sees them
)
def get_my_payslips(ctx: UserContext, year=None, month=None) -> dict:
    y = to_int(year, "year", 2000, 2100) or datetime.date.today().year
    m = to_int(month, "month", 1, 12)
    if m:
        start = datetime.date(y, m, 1)
        end = datetime.date(y, m, calendar.monthrange(y, m)[1])
    else:
        start, end = datetime.date(y, 1, 1), datetime.date(y, 12, 31)
    return fetch_payslips(ctx, start, end, with_breakdown=bool(m))