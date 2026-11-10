import datetime
from collections import Counter

from app.core.frappe_client import get_frappe_client
from app.core.security import ToolInputError, UserContext, require_access
from app.tools.registry import tool

MAX_DAYS = 93  # about three months per request


def _parse_date(value: str, label: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise ToolInputError(
            f"{label} must be a real date written as YYYY-MM-DD."
        ) from None


@tool(
    name="get_attendance",
    description=(
        "Get attendance records (Present, Absent, On Leave, Half Day, Work From "
        "Home) for a date range, with counts per status. By default it covers "
        "the last 30 days. Use it for the logged-in person, or for one of their "
        "direct reports by passing that person's employee_id (find it with "
        "list_my_team). Maximum range is about three months."
    ),
    parameters={
        "type": "object",
        "properties": {
            "employee_id": {
                "type": "string",
                "description": "Employee ID such as HR-EMP-00001. Leave out for your own attendance.",
            },
            "from_date": {"type": "string", "description": "Start date, YYYY-MM-DD."},
            "to_date": {"type": "string", "description": "End date, YYYY-MM-DD."},
        },
    },
)
def get_attendance(ctx: UserContext, employee_id: str | None = None,
                   from_date: str | None = None, to_date: str | None = None) -> dict:
    # The access check comes FIRST, before any data is fetched.
    target = require_access(ctx, employee_id)

    end = _parse_date(to_date, "to_date") if to_date else datetime.date.today()
    start = (_parse_date(from_date, "from_date") if from_date
             else end - datetime.timedelta(days=29))
    if start > end:
        raise ToolInputError("from_date must not be after to_date.")
    if (end - start).days + 1 > MAX_DAYS:
        raise ToolInputError("Please ask for at most three months at a time.")

    frappe = get_frappe_client()
    rows = frappe.get_list(
        "Attendance",
        ["attendance_date", "status", "leave_type", "late_entry", "early_exit"],
        filters=[
            ["employee", "=", target],
            ["docstatus", "=", 1],  # submitted records only, not drafts or cancelled
            ["attendance_date", ">=", start.isoformat()],
            ["attendance_date", "<=", end.isoformat()],
        ],
        order_by="attendance_date asc",
        limit=MAX_DAYS * 2,
    )
    names = frappe.get_list("Employee", ["employee_name"],
                            filters={"name": target}, limit=1)

    return {
        "employee_id": target,
        "employee_name": (names[0]["employee_name"] or "").strip() if names else "",
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "summary": dict(Counter(r["status"] for r in rows)),
        "records": rows,
    }