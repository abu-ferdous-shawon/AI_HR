import re
from dataclasses import dataclass

from app.frappe_client.client import get_service_client
from app.auth.security import (
    AccessDenied, ToolInputError, UserContext, get_team, has_role, is_hr_admin, require_employee,
)
from app.frappe_client.service_purpose import for_purpose, service_purpose
from app.registry.models import Profile, RecordType, norm

SCOPES = ("self", "team", "company")
MAX_NAME_LENGTH = 60
MAX_CANDIDATES = 5
_UNSAFE = re.compile(r"[%_\\\x00-\x1f]")      # LIKE wildcards and control characters


@dataclass(frozen=True)
class Access:
    scope: str
    employee_ids: tuple[str, ...] | None   # None = every employee (HR, company scope)
    employee: dict | None                  # {"id", "name"} when one person was chosen


def _clean_name(text) -> str:
    cleaned = " ".join(_UNSAFE.sub(" ", str(text)).split())
    if not cleaned:
        raise ToolInputError("Please give the employee's name.")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ToolInputError("That employee name is too long.")
    return cleaned


def _same(text: str, emp_id: str, emp_name: str) -> bool:
    wanted = norm(text)
    return wanted == norm(emp_id) or wanted in norm(emp_name)


_ME_WORDS = {"me", "myself", "my", "i", "self", "own"}


def _is_me(text: str, emp_id: str, emp_name: str) -> bool:
    """Stricter than _same: in self scope only the person themselves, never a look-alike."""
    wanted = norm(text)
    return wanted in _ME_WORDS or wanted == norm(emp_id) or wanted == norm(emp_name)

def _one(matches: list[dict], text: str) -> dict:
    if len(matches) == 1:
        return matches[0]
    shown = "; ".join(f"{m['name']} ({m['id']})" for m in matches[:MAX_CANDIDATES])
    raise ToolInputError(f"More than one employee matches '{text}': {shown}. "
                         "Ask which one is meant, and use the full name or the ID.")


@for_purpose("employee_search")
def _find_employee(profile: Profile, text: str) -> list[dict]:
    """Name search for HR. A lookup of structure, so it uses the key."""
    cfg, client = profile.employee, get_service_client()
    rows = client.get_list(cfg.doctype, ["name", cfg.name_field],
                           filters={"name": text}, limit=2)
    if not rows:
        rows = client.get_list(cfg.doctype, ["name", cfg.name_field],
                               filters=[[cfg.name_field, "like", f"%{text}%"]],
                               limit=MAX_CANDIDATES + 1)
    matches = [{"id": r["name"], "name": (r.get(cfg.name_field) or "").strip()} for r in rows]
    if not matches:
        raise ToolInputError(f"No employee matches '{text}'.")
    return matches


def resolve_access(ctx: UserContext, profile: Profile, rt: RecordType,
                   scope: str | None, employee: str | None) -> Access:
    """Decide whose records may be read. Raises AccessDenied or ToolInputError.

    self    everyone, their own records.
    team    people with direct reports (and the manager role, if the profile asks for one).
    company HR roles, or an employee listed in HR_ADMIN_EMPLOYEES.
    """
    level = (scope or "self").strip().lower()
    if level not in SCOPES:
        raise ToolInputError("scope must be one of: self, team, company.")
    if level not in rt.scopes:
        raise AccessDenied(f"{rt.label} are not available at the '{level}' level. "
                           f"Available: {', '.join(rt.scopes)}.")
    text = _clean_name(employee) if employee and str(employee).strip() else None
    if level == "self":
        own_id = require_employee(ctx)
        if text:
            cfg = profile.employee
            with service_purpose("identity"):
                own = get_service_client().get_list(
                    cfg.doctype, [cfg.name_field], filters={"name": own_id}, limit=1)
            own_name = (own[0].get(cfg.name_field) or "").strip() if own else ""
            if not _is_me(text, own_id, own_name):
                raise AccessDenied("You can only view your own records here. For other people "
                                   "use scope 'team' (your direct reports) or 'company' (HR only).")
        return Access("self", (own_id,), None)

    if level == "team":
        needed = profile.access.manager_roles
        if needed and not has_role(ctx, needed):
            raise AccessDenied("Team records are available to managers only.")
        name_field = profile.employee.name_field
        team = [{"id": m["name"], "name": (m.get(name_field) or "").strip()}
                for m in get_team(ctx)]
        if not team:
            raise AccessDenied("You have no direct reports, so team records are not available.")
        if text:
            matches = [m for m in team if _same(text, m["id"], m["name"])]
            if not matches:
                raise AccessDenied(f"'{text}' is not one of your direct reports.")
            chosen = _one(matches, text)
            return Access("team", (chosen["id"],), chosen)
        return Access("team", tuple(m["id"] for m in team), None)

    if not is_hr_admin(ctx, profile):
        raise AccessDenied(f"Company-wide {rt.label.lower()} are available to HR only.")
    if text:
        chosen = _one(_find_employee(profile, text), text)
        return Access("company", (chosen["id"],), chosen)
    return Access("company", None, None)
