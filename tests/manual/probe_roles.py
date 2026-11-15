import sys

from app.config import settings
from app.core.frappe_client import FrappeClient, FrappeError
from app.core.roles import clear_role_cache, resolve_roles
from app.core.security import AccessDenied, UserContext, get_team, has_role, resolve_context
from app.registry import get_profile
from tests.manual._login import HOW_TO, credentials_from_env


def scope_lines(ctx: UserContext, profile, team_size: int) -> list[str]:
    """What this person could read, as the app decides it."""
    access = profile.access
    lines = ["self     yes (everyone)"]
    if not team_size:
        lines.append("team     no  (no direct reports)")
    elif access.manager_roles and not has_role(ctx, access.manager_roles):
        lines.append(f"team     no  ({team_size} direct reports, but the profile requires "
                     f"a manager role)")
    else:
        lines.append(f"team     yes ({team_size} direct reports)")

    listed = {p.strip() for p in settings.hr_admin_employees.split(",") if p.strip()}
    if has_role(ctx, access.hr_roles):
        lines.append("company  yes (HR role)")
    elif ctx.employee_id in listed:
        lines.append("company  yes (listed in HR_ADMIN_EMPLOYEES)")
    else:
        lines.append("company  no  (not HR)")
    return lines


def advice(result) -> str:
    if result.roles:
        return f"Roles were found by '{result.source}'."
    return ("No roles could be found automatically. Add a role_map rule to the profile "
            "(by employee, login, designation or department), or list the employee in "
            "HR_ADMIN_EMPLOYEES.")


def logged_in_as(credentials) -> str | None:
    client = FrappeClient(credentials=credentials)
    try:
        return client.call_method("frappe.auth.get_logged_user")
    except FrappeError:
        return None
    finally:
        client.session.close()


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python -m tests.manual.probe_roles <login of the person>")
    login = sys.argv[1]
    profile, credentials = get_profile(), credentials_from_env()
    try:
        ctx = resolve_context(login)
    except AccessDenied as exc:
        raise SystemExit(f"Cannot continue: {exc}")
    except FrappeError as exc:
        raise SystemExit(f"Could not reach the ERP: {exc}")

    print(f"Profile: {profile.name}")
    print(f"Employee: {ctx.employee_id}")
    if credentials is None:
        print("Person's own login supplied: no, so the 'session' attempts are skipped.\n" + HOW_TO)
    else:
        owner = logged_in_as(credentials)
        print("Person's own login supplied: yes")
        if owner is None:
            print("  WARNING: the ERP did not accept that login.")
        elif owner.lower() != login.lower():
            print("  WARNING: that login belongs to a different account than the one you "
                  "named, so the 'session' attempts describe the wrong person.")

    clear_role_cache()
    found = resolve_roles(ctx.employee_id, credentials)
    context = UserContext(ctx.user_id, ctx.employee_id, found.roles, found.source)
    print(f"\nRoles found by: {found.source}")
    print("Roles:", ", ".join(found.roles) or "none")
    print("Role methods tried:", ", ".join(profile.access.role_methods) or "none configured")
    print("Attempts:")
    for note in found.notes:
        print("  -", note)
    print("\nWhat this person could read:")
    for line in scope_lines(context, profile, len(get_team(context))):
        print("  " + line)
    print("\n" + advice(found))


if __name__ == "__main__":
    main()