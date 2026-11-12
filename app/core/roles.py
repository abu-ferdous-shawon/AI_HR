import logging
import time
from dataclasses import dataclass
from threading import Lock

from app.config import settings
from app.core.credentials import Credentials
from app.core.frappe_client import FrappeClient, FrappeError, get_service_client
from app.registry import Profile, get_profile
from app.registry.models import AccessConfig, EmployeeConfig, RoleRule, norm

log = logging.getLogger("hr_ai.roles")

# Roles everyone has automatically. They say nothing about the person.
AUTOMATIC_ROLES = {"all", "guest", "desk user", "authenticated"}


@dataclass(frozen=True)
class RoleResult:
    roles: tuple[str, ...]
    source: str                    # "session", "key", "mapping" or "none"
    notes: tuple[str, ...] = ()    # what each attempt did; never contains a secret


def _names(value, name_field: str) -> list[str]:
    """Role names from a list of strings, or of rows like {'role': 'HR Manager'}."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and item.get(name_field):
            out.append(str(item[name_field]))
    return out


def _real(names: list[str]) -> list[str]:
    seen, out = set(), []
    for name in names:
        name = name.strip()
        if name and norm(name) not in AUTOMATIC_ROLES and norm(name) not in seen:
            seen.add(norm(name))
            out.append(name)
    return out


def _read_roles(client, login: str, access: AccessConfig) -> tuple[list[str], list[str]]:
    """Try each way of asking the ERP for a person's roles. Returns (roles, notes).

    An empty answer is not 'no roles': ERPs often hide the role list from people who
    may not see it, so it counts as inconclusive and the next way is tried.
    """
    attempts = [("user record", lambda: _names(
        client.get_doc(access.user_doctype, login).get(access.user_roles_field),
        access.role_name_field))]
    for method in access.role_methods:
        attempts.append((f"method {method}", lambda m=method: _names(
            client.call_method(m, {access.role_method_param: login}),
            access.role_name_field)))

    notes: list[str] = []
    for label, attempt in attempts:
        try:
            roles = _real(attempt())
        except FrappeError as exc:
            kind = "denied" if exc.status_code in (401, 403) else "failed"
            notes.append(f"{label}: {kind} ({exc.status_code})")
            continue
        if roles:
            notes.append(f"{label}: ok")
            return roles, notes
        notes.append(f"{label}: no roles returned")
    return [], notes


def _session_client(credentials: Credentials) -> FrappeClient:
    return FrappeClient(credentials=credentials)      # replaced by a fake in tests


def _employee_facts(employee_id: str, cfg: EmployeeConfig) -> dict:
    rows = get_service_client().get_list(cfg.doctype, ["*"],
                                         filters={"name": employee_id}, limit=1)
    row = rows[0] if rows else {}
    return {"login": row.get(cfg.user_field) or None,
            "designation": row.get(cfg.designation_field),
            "department": row.get(cfg.department_field)}


def _matches(rule: RoleRule, employee_id, login, designation, department) -> bool:
    def has(values: list[str], value) -> bool:
        return bool(value) and norm(value) in {norm(v) for v in values}
    return (has(rule.employees, employee_id) or has(rule.users, login)
            or has(rule.designations, designation) or has(rule.departments, department))


def _resolve(employee_id: str, credentials: Credentials | None, profile: Profile) -> RoleResult:
    access, cfg = profile.access, profile.employee
    notes: list[str] = []
    try:
        facts = _employee_facts(employee_id, cfg)
    except FrappeError as exc:
        notes.append(f"employee lookup failed ({exc.status_code})")
        facts = {"login": None, "designation": None, "department": None}
    login = facts["login"]

    if login and credentials is not None:
        client = _session_client(credentials)
        try:
            roles, found = _read_roles(client, login, access)
        finally:
            client.session.close()
        notes += [f"session: {n}" for n in found]
        if roles:
            return RoleResult(tuple(roles), "session", tuple(notes))
    elif login:
        notes.append("session: skipped (no user credentials)")

    if login:
        roles, found = _read_roles(get_service_client(), login, access)
        notes += [f"key: {n}" for n in found]
        if roles:
            return RoleResult(tuple(roles), "key", tuple(notes))
    else:
        notes.append("this employee has no linked login")

    mapped = [rule.role for rule in access.role_map
              if _matches(rule, employee_id, login, facts["designation"], facts["department"])]
    if mapped:
        return RoleResult(tuple(dict.fromkeys(mapped)), "mapping", tuple(notes))
    return RoleResult((), "none", tuple(notes))


_cache: dict[str, tuple[float, RoleResult]] = {}
_lock = Lock()


def clear_role_cache() -> None:
    with _lock:
        _cache.clear()


def resolve_roles(employee_id: str, credentials: Credentials | None = None, *,
                  profile: Profile | None = None) -> RoleResult:
    """A person's roles, from the first source that can tell: session, key, mapping."""
    ttl = settings.role_cache_seconds
    if ttl > 0:
        with _lock:
            hit = _cache.get(employee_id)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]

    result = _resolve(employee_id, credentials, profile or get_profile())
    if ttl > 0:
        with _lock:
            _cache[employee_id] = (time.monotonic(), result)
    log.info("roles of %s found by %s (%d roles)", employee_id, result.source, len(result.roles))
    for note in result.notes:
        log.debug("roles of %s: %s", employee_id, note)
    return result