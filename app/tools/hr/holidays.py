import datetime

from app.core.frappe_client import get_frappe_client
from app.core.security import UserContext
from app.tools.common import resolve_range
from app.tools.registry import tool

MAX_HOLIDAYS = 60
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def fetch_holidays(ctx: UserContext, start: datetime.date, end: datetime.date,
                   include_weekly_off: bool = False, limit: int = MAX_HOLIDAYS) -> dict:
    """Holidays of the list assigned to this employee (or the company default)."""
    frappe = get_frappe_client()
    rows = frappe.get_list("Employee", ["*"], filters={"name": ctx.employee_id}, limit=1)
    row = rows[0] if rows else {}

    list_name = row.get("holiday_list")
    if not list_name and row.get("company"):
        list_name = frappe.get_doc("Company", row["company"]).get("default_holiday_list")
    if not list_name:
        return {"holiday_list": None, "holidays": [],
                "note": "No holiday list is assigned to you or to your company."}

    entries = []
    for h in frappe.get_doc("Holiday List", list_name).get("holidays") or []:
        try:
            day = datetime.date.fromisoformat(str(h.get("holiday_date")))
        except ValueError:
            continue
        entries.append((day, {"date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()],
                              "holiday": (h.get("description") or "").strip(),
                              "weekly_off": bool(h.get("weekly_off"))}))
    entries.sort(key=lambda pair: pair[0])

    today = datetime.date.today()
    public: list[dict] = []        # public holidays inside the asked range
    weekly: list[dict] = []        # weekly offs inside the asked range
    weekly_days: set[int] = set()  # which weekdays are the weekly off
    next_public = next_weekly = None
    for day, item in entries:
        if item["weekly_off"]:
            weekly_days.add(day.weekday())
            if day >= today and next_weekly is None:
                next_weekly = item
        elif day >= today and next_public is None:
            next_public = item
        if start <= day <= end:
            (weekly if item["weekly_off"] else public).append(item)

    shown = sorted(public + (weekly if include_weekly_off else []), key=lambda i: i["date"])
    return {
        "holiday_list": list_name,
        "holidays": shown[:limit],              # public holidays (plus weekly offs only if asked)
        "truncated": len(shown) > limit,
        "public_holiday_count": len(public),
        "weekly_off_count": len(weekly),
        "weekly_off_days": [WEEKDAYS[i] for i in sorted(weekly_days)],
        "next_public_holiday": next_public,     # from today onward, whatever the range
        "next_weekly_off": next_weekly,
    }


@tool(
    name="get_holidays",
    description=(
        "Get the days off that apply to the logged-in person, for a year or a "
        "date range (default: this year). Public holidays are in 'holidays'. "
        "Weekly offs (such as Friday and Saturday) are summarised in "
        "'weekly_off_days', 'weekly_off_count' and 'next_weekly_off', and are "
        "listed in 'holidays' only when include_weekly_off is true. Also gives "
        "'next_public_holiday'. Use it for 'when is the next holiday?', "
        "'holidays in March' or 'which days am I off?'."
    ),
    parameters={"type": "object", "properties": {
        "year": {"type": "integer", "description": "Calendar year, e.g. 2026."},
        "from_date": {"type": "string", "description": "Start date, YYYY-MM-DD."},
        "to_date": {"type": "string", "description": "End date, YYYY-MM-DD."},
        "include_weekly_off": {"type": "boolean",
                               "description": "Also list every weekly off day. Default false."},
    }},
)
def get_holidays(ctx: UserContext, year=None, from_date=None, to_date=None,
                 include_weekly_off=None) -> dict:
    start, end = resolve_range(year, from_date, to_date)
    result = fetch_holidays(ctx, start, end, bool(include_weekly_off))
    return {"from_date": start.isoformat(), "to_date": end.isoformat(), **result}