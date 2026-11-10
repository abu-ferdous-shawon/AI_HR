import logging
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.core.frappe_client import FrappeError, get_frappe_client
from app.core.masking import is_confidential

log = logging.getLogger("hr_ai.security")


class AccessDenied(Exception):
    """Raised when someone asks for something they may not have."""


class ToolInputError(Exception):
    """Raised when the model passes a value a tool cannot use (e.g. a bad date)."""


@dataclass(frozen=True)
class UserContext:
    """Who is asking. Built by our code, never from the chat text."""
    user_id: str      # the Frappe login, e.g. an email
    employee_id: str  # the Employee record linked to that login


def resolve_context(user_id: str) -> UserContext:
    """Turn a logged-in user into their one Employee record, or refuse."""
    rows = get_frappe_client().get_list(
        "Employee",
        ["name"],
        filters={"user_id": user_id, "status": "Active"},
        limit=2,
    )
    # Exactly one match is required. Zero or several are both unsafe.
    if len(rows) != 1:
        raise AccessDenied("No active employee record is linked to this login.")
    return UserContext(user_id=user_id, employee_id=rows[0]["name"])


def get_team(ctx: UserContext) -> list[dict]:
    """Active employees who report directly to this person."""
    return get_frappe_client().get_list(
        "Employee",
        ["name", "employee_name"],
        filters={"reports_to": ctx.employee_id, "status": "Active"},
        limit=200,
    )


def require_access(ctx: UserContext, employee_id: str | None) -> str:
    """Return the employee ID a tool may use: yourself or a direct report.

    Every tool that accepts an employee_id MUST call this first.
    """
    if not employee_id or employee_id == ctx.employee_id:
        return ctx.employee_id
    if employee_id in {m["name"] for m in get_team(ctx)}:
        return employee_id
    raise AccessDenied(
        "You can only view your own records and those of your direct reports."
    )


def is_hr_admin(ctx: UserContext) -> bool:
    """True for people who may see company-wide figures (HR managers)."""
    listed = {e.strip() for e in settings.hr_admin_employees.split(",") if e.strip()}
    if ctx.employee_id in listed:
        return True
    wanted = {r.strip() for r in settings.hr_admin_roles.split(",") if r.strip()}
    if not wanted:
        return False

    frappe = get_frappe_client()
    rows = frappe.get_list("Employee", ["user_id"], filters={"name": ctx.employee_id}, limit=1)
    login = rows[0].get("user_id") if rows else None
    if not login:
        return False
    try:
        user = frappe.get_doc("User", login)
    except FrappeError as exc:
        # Fail closed: if we cannot read the roles, nobody gets in this way.
        log.warning("Could not read the roles of %s (%s). Add the person to "
                    "HR_ADMIN_EMPLOYEES instead.", login, exc)
        return False
    return bool(wanted & {r.get("role") for r in user.get("roles") or []})


def scrub(data: Any, allow: frozenset = frozenset()) -> Any:
    """Remove confidential fields from a result, including nested ones.

    allow = confidential field names this tool may return, or {"*"} for all
    of them. A tool may only use "*" because the agent masks confidential
    values before the model sees them.
    """
    if "*" in allow:
        return data
    if isinstance(data, dict):
        return {k: scrub(v, allow) for k, v in data.items()
                if not is_confidential(k) or k in allow}
    if isinstance(data, list):
        return [scrub(item, allow) for item in data]
    return data