import datetime
from collections import defaultdict

from app.core.frappe_client import get_frappe_client
from app.core.security import UserContext
from app.tools.common import resolve_range
from app.tools.registry import tool

LEAVE_DETAILS = (
    "hrms.hr.doctype.leave_application.leave_application.get_leave_details"
)
MAX_APPLICATIONS = 500

# Only these fields are passed on. Missing ones are skipped, never an error.
APPLICATION_FIELDS = {
    "name": "application_id", "leave_type": "leave_type", "from_date": "from_date",
    "to_date": "to_date", "total_leave_days": "days", "half_day": "half_day",
    "status": "status", "posting_date": "applied_on", "description": "reason",
}


def fetch_leave_balance(ctx: UserContext) -> dict:
    today = datetime.date.today().isoformat()
    details = get_frappe_client().call_method(
        LEAVE_DETAILS, {"employee": ctx.employee_id, "date": today}
    ) or {}
    allocation = details.get("leave_allocation") or {}
    balances = [
        {
            "leave_type": leave_type,
            "total": values.get("total_leaves"),
            "taken": values.get("leaves_taken"),
            "pending_approval": values.get("leaves_pending_approval"),
            "remaining": values.get("remaining_leaves"),
        }
        for leave_type, values in allocation.items()
    ]
    # We deliberately drop 'leave_approver' and 'lwps' from Frappe's reply.
    return {"as_of": today, "balances": balances}


def fetch_leave_history(ctx: UserContext, start: datetime.date, end: datetime.date,
                        status: str | None = None) -> dict:
    """This employee's leave applications that overlap the date range."""
    rows = get_frappe_client().get_all(
        "Leave Application", ["*"],
        filters=[
            ["employee", "=", ctx.employee_id],      # always the caller's own ID
            ["docstatus", "!=", 2],                  # skip cancelled documents
            ["from_date", "<=", end.isoformat()],
            ["to_date", ">=", start.isoformat()],
        ],
        order_by="from_date asc", max_rows=MAX_APPLICATIONS,
    )

    applications = []
    for row in rows:
        item = {}
        for source, target in APPLICATION_FIELDS.items():
            value = row.get(source)
            if value in (None, ""):
                continue
            item[target] = value.strip()[:200] if isinstance(value, str) else value
        if status and str(item.get("status", "")).lower() != status.strip().lower():
            continue
        applications.append(item)

    approved_days: dict[str, float] = defaultdict(float)
    for app in applications:
        if str(app.get("status", "")).lower() == "approved":
            approved_days[app.get("leave_type", "?")] += float(app.get("days") or 0)

    return {"from_date": start.isoformat(), "to_date": end.isoformat(),
            "count": len(applications), "applications": applications,
            "approved_days_by_leave_type": dict(approved_days)}


@tool(
    name="get_my_leave_balance",
    description=(
        "Get the logged-in employee's own leave balance as of today, for every "
        "leave type (for example Annual, Casual, Sick): total allowed, taken, "
        "pending approval and remaining."
    ),
)
def get_my_leave_balance(ctx: UserContext) -> dict:
    return fetch_leave_balance(ctx)


@tool(
    name="get_my_leave_history",
    description=(
        "Get the logged-in person's own leave applications (what they applied "
        "for, dates, days, and whether each is Open, Approved or Rejected) for a "
        "year or date range (default: this year), plus the approved days used "
        "per leave type. Optionally filter by status."
    ),
    parameters={"type": "object", "properties": {
        "year": {"type": "integer", "description": "Calendar year, e.g. 2026."},
        "from_date": {"type": "string", "description": "Start date, YYYY-MM-DD."},
        "to_date": {"type": "string", "description": "End date, YYYY-MM-DD."},
        "status": {"type": "string", "description": "Open, Approved, Rejected or Cancelled."},
    }},
)
def get_my_leave_history(ctx: UserContext, year=None, from_date=None,
                         to_date=None, status=None) -> dict:
    start, end = resolve_range(year, from_date, to_date)
    return fetch_leave_history(ctx, start, end, status)