import pytest

from app.core import security
from app.core.roles import RoleResult
from app.core.security import AccessDenied, UserContext, has_role, require_role
from app.records import scope
from app.registry import build_profile


def person(*roles, emp="E1"):
    return UserContext("u@x.com", emp, tuple(roles))


def make(**access):
    return build_profile({"name": "t", "access": access, "record_types": {"thing": {
        "doctype": "Thing", "label": "Things", "description": "t",
        "aliases": {"en": ["thing"]}, "employee_field": "employee",
        "fields": ["name", "employee"], "scopes": ["self", "team", "company"]}}})


class Erp:
    def __init__(self):
        self.team, self.employees = [], []

    def get_list(self, doctype, fields=None, filters=None, order_by=None, limit=20, start=0):
        return list(self.employees)[:limit]


@pytest.fixture
def erp(monkeypatch):
    fake = Erp()
    monkeypatch.setattr(scope, "get_service_client", lambda: fake)
    monkeypatch.setattr(scope, "get_team", lambda ctx: fake.team)
    monkeypatch.setattr(security.settings, "hr_admin_employees", "")
    return fake


def access(who, profile=None, level="self", employee=None):
    profile = profile or make()
    return scope.resolve_access(who, profile, profile.get("thing"), level, employee)


# ---------- self ----------

def test_everyone_may_read_their_own_records(erp):
    result = access(person())
    assert (result.scope, result.employee_ids) == ("self", ("E1",))


def test_a_user_cannot_read_another_employee_in_self_scope(erp):
    erp.employees = [{"name": "E1", "employee_name": "Me Myself"}]
    with pytest.raises(AccessDenied, match="only view your own"):
        access(person("Employee", "HR Manager"), employee="Rahim")   # even HR must use company scope
    assert access(person(), employee="me").employee_ids == ("E1",)


# ---------- team ----------

def test_a_manager_cannot_read_someone_who_does_not_report_to_them(erp):
    erp.team = [{"name": "E2", "employee_name": "Rahim Uddin"}]
    with pytest.raises(AccessDenied, match="not one of your direct reports"):
        access(person("Employee"), level="team", employee="Karim")
    chosen = access(person(), level="team", employee="rahim")
    assert chosen.employee_ids == ("E2",)
    assert chosen.employee == {"id": "E2", "name": "Rahim Uddin"}


def test_team_scope_covers_only_direct_reports(erp):
    erp.team = [{"name": "E2", "employee_name": "A"}, {"name": "E3", "employee_name": "B"}]
    assert access(person(), level="team").employee_ids == ("E2", "E3")
    erp.team = []
    with pytest.raises(AccessDenied, match="no direct reports"):
        access(person(), level="team")


def test_a_manager_role_can_be_required_for_team_scope(erp):
    erp.team = [{"name": "E2", "employee_name": "A"}]
    strict = make(manager_roles=["Line Manager"])
    with pytest.raises(AccessDenied, match="managers only"):
        access(person("Employee"), strict, "team")
    assert access(person("line manager"), strict, "team").employee_ids == ("E2",)
    erp.team = []
    with pytest.raises(AccessDenied, match="no direct reports"):
        access(person("Line Manager"), strict, "team")      # the role alone is not enough


# ---------- company ----------

def test_hr_roles_unlock_company_scope(erp):
    for roles in (("HR Manager",), ("hr user",), ("Employee", "HR Manager")):
        result = access(person(*roles), level="company")
        assert result.scope == "company" and result.employee_ids is None


def test_other_roles_and_unknown_roles_do_not(erp):
    for who in (person("Employee"), person("Line Manager"), person()):
        with pytest.raises(AccessDenied, match="HR only"):
            access(who, level="company")


def test_hr_role_names_come_from_the_profile(erp):
    custom = make(hr_roles=["People Ops"])
    assert access(person("people ops"), custom, "company").scope == "company"
    with pytest.raises(AccessDenied):
        access(person("HR Manager"), custom, "company")


def test_the_explicit_hr_list_grants_company_scope(erp, monkeypatch):
    monkeypatch.setattr(security.settings, "hr_admin_employees", "E9, E8")
    assert access(person(emp="E9"), level="company").scope == "company"
    with pytest.raises(AccessDenied):
        access(person(emp="E1"), level="company")


def test_hr_can_read_one_named_employee(erp):
    erp.employees = [{"name": "E7", "employee_name": "Rahim Uddin"}]
    result = access(person("HR Manager"), level="company", employee="Rahim")
    assert result.employee_ids == ("E7",) and result.employee["name"] == "Rahim Uddin"


# ---------- roles on the context ----------

def test_has_role_and_require_role():
    who = person("Employee", "HR Manager")
    assert has_role(who, ["hr manager"]) and not has_role(who, ["Line Manager"])
    require_role(who, "HR Manager")                                  # passes silently
    with pytest.raises(AccessDenied, match="role"):
        require_role(person(), "HR Manager")
    with pytest.raises(AccessDenied, match="custom text"):
        require_role(person(), "HR Manager", message="custom text")


def test_a_context_without_roles_has_no_special_access(monkeypatch):
    monkeypatch.setattr(security.settings, "hr_admin_employees", "")
    ctx = UserContext("u", "E1")
    assert ctx.roles == () and ctx.role_source == "" and not security.is_hr_admin(ctx)


def test_a_context_is_built_with_the_resolved_roles(monkeypatch):
    monkeypatch.setattr(security.settings, "hr_admin_employees", "")
    monkeypatch.setattr(security, "resolve_roles",
                        lambda employee_id, credentials=None: RoleResult(("HR Manager",), "key"))
    built = security.build_context("u@x.com", "E1")
    assert (built.roles, built.role_source) == (("HR Manager",), "key")
    assert security.is_hr_admin(built)


class Directory:
    rows = [{"name": "E5"}]

    def get_list(self, doctype, fields=None, filters=None, order_by=None, limit=20, start=0):
        self.filters = filters
        return list(self.rows)


def test_resolve_context_finds_one_employee_and_their_roles(monkeypatch):
    directory = Directory()
    monkeypatch.setattr(security, "get_service_client", lambda: directory)
    monkeypatch.setattr(security, "resolve_roles",
                        lambda employee_id, credentials=None: RoleResult(("Employee",), "session"))
    ctx = security.resolve_context("ann@x.com")
    assert (ctx.employee_id, ctx.roles, ctx.role_source) == ("E5", ("Employee",), "session")
    assert directory.filters == {"user_id": "ann@x.com", "status": "Active"}


@pytest.mark.parametrize("rows", [[], [{"name": "E5"}, {"name": "E6"}]])
def test_no_employee_or_several_employees_for_a_login_is_refused(monkeypatch, rows):
    directory = Directory()
    directory.rows = rows
    monkeypatch.setattr(security, "get_service_client", lambda: directory)
    with pytest.raises(AccessDenied, match="No active employee"):
        security.resolve_context("ann@x.com")