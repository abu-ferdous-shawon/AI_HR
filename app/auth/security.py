from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.auth.credentials import Credentials
from app.frappe_client.client import get_service_client
from app.core.masking import is_confidential
from app.auth.roles import resolve_login_roles, resolve_roles
from app.registry import Profile, get_profile
from app.registry.models import norm
from app.frappe_client.service_purpose import for_purpose



class AccessDenied(Exception):
    """Raised when someone asks for something they may not have."""


class ToolInputError(Exception):
    """Raised when the model passes a value a tool cannot use (e.g. a bad date)."""


@dataclass(frozen=True)
class UserContext:
    """Who is asking. Built by our code, never from the chat text."""
    user_id: str                       # the ERP login, e.g. an email
    employee_id: str | None            # an active Employee linked to the login, if any
    roles: tuple[str, ...] = ()        # the person's roles; empty = unknown, so no special access
    role_source: str = ""              # how the roles were found: session, key, mapping or none
    display_name: str = field(default="", compare=False, repr=False)


def build_context(user_id: str, employee_id: str,
                  credentials: Credentials | None = None, *, display_name: str | None = None) -> UserContext:
    """A context for a known employee, with their roles looked up."""
    found = resolve_roles(employee_id, credentials)
    return UserContext(user_id=user_id, employee_id=employee_id,
                       roles=found.roles, role_source=found.source,
                       display_name=display_name.strip() if isinstance(display_name, str) else "")

@for_purpose("identity")
def resolve_context(user_id: str, credentials: Credentials | None = None) -> UserContext:
    """Bind a verified login to its Employee when present and resolve its roles."""
    cfg = get_profile().employee
    rows = get_service_client().get_list(
        cfg.doctype, ["name", cfg.name_field],
        filters={cfg.user_field: user_id, cfg.status_field: cfg.active_value},
        limit=2,
    )
    if not rows and credentials is not None:
        found = resolve_login_roles(user_id, credentials)
        return UserContext(user_id, None, found.roles, found.source)
    if len(rows) != 1:
        raise AccessDenied("No active employee record is linked to this login."
                           if not rows else "Multiple active employee records are linked to this login.")
    return build_context(user_id, rows[0]["name"], credentials,
                         display_name=rows[0].get(cfg.name_field))


@for_purpose("team")
def get_team(ctx: UserContext) -> list[dict]:
    """Active employees who report directly to this person."""
    employee_id = require_employee(ctx)
    cfg = get_profile().employee
    return get_service_client().get_list(
        cfg.doctype, ["name", cfg.name_field],
        filters={cfg.reports_to_field: employee_id, cfg.status_field: cfg.active_value},
        limit=200,
    )


def require_employee(ctx: UserContext) -> str:
    if ctx.employee_id is None:
        raise AccessDenied("This request requires an active Employee record linked to your Frappe login.")
    return ctx.employee_id


def require_access(ctx: UserContext, employee_id: str | None) -> str:
    """Return the employee ID a tool may use: yourself or a direct report.

    Every tool that accepts an employee_id MUST call this first.
    """
    own_id = require_employee(ctx)
    if not employee_id or employee_id == own_id:
        return own_id
    if employee_id in {m["name"] for m in get_team(ctx)}:
        return employee_id
    raise AccessDenied(
        "You can only view your own records and those of your direct reports."
    )


def has_role(ctx: UserContext, names) -> bool:
    """True if the person holds any of these roles (compared without case)."""
    wanted = {norm(n) for n in names}
    return any(norm(r) in wanted for r in ctx.roles)


def require_role(ctx: UserContext, *names: str, message: str | None = None) -> None:
    """Refuse unless the person holds one of these roles."""
    if not has_role(ctx, names):
        raise AccessDenied(message or "You do not have the role this needs.")


def _listed(text: str) -> set[str]:
    return {part.strip() for part in str(text).split(",") if part.strip()}


def is_hr_admin(ctx: UserContext, profile: Profile | None = None) -> bool:
    """HR: listed in HR_ADMIN_EMPLOYEES (an explicit grant) or holding an HR role."""
    if ctx.employee_id in _listed(settings.hr_admin_employees):
        return True
    return has_role(ctx, (profile or get_profile()).access.hr_roles)


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
