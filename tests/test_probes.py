import pytest

from app.core import security
from app.core.frappe_client import FrappeError
from app.core.roles import RoleResult
from app.core.security import UserContext
from app.registry import build_profile
from tests.manual import probe_roles, probe_session
from tests.manual._login import credentials_from_env


def test_the_login_is_read_from_the_environment():
    assert credentials_from_env({}) is None
    assert credentials_from_env({"HR_TEST_TOKEN": "  "}) is None
    token = credentials_from_env({"HR_TEST_TOKEN": "token a:b"})
    assert token.headers() == {"Authorization": "token a:b"}
    sid = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    assert credentials_from_env({"HR_TEST_SID": sid}).headers() == {"Cookie": f"sid={sid}"}
    both = credentials_from_env({"HR_TEST_TOKEN": "token a:b", "HR_TEST_SID": sid})
    assert both.headers() == {"Authorization": "token a:b"}          # the token wins
    assert credentials_from_env({"HR_TEST_SID": "Guest"}) is None


class Erp:
    def __init__(self, rows=None, error=None):
        self.rows, self.error = rows or [], error

    def get_all(self, doctype, fields=None, filters=None, order_by=None, max_rows=2000):
        if self.error:
            raise self.error
        return self.rows[:max_rows]


def test_counts_are_labelled_and_capped():
    assert probe_session.count_of(Erp([{}] * 3), "X") == (3, "3")
    assert probe_session.count_of(Erp([{}] * 500), "X") == (201, "200+")
    assert probe_session.count_of(Erp(error=FrappeError(403, "x")), "X") == (None, "refused (403)")
    assert probe_session.count_of(Erp(error=FrappeError(500, "x")), "X") == (None, "failed (500)")


@pytest.mark.parametrize("key, user, expected", [
    ((10, "10"), (3, "3"), "RESTRICTED (fewer records)"),
    ((10, "10"), (None, "refused (403)"), "RESTRICTED (refused for the person)"),
    ((10, "10"), (10, "10"), "same view"),
    ((3, "3"), (10, "10"), "person sees more than the key"),
    ((None, "refused (403)"), (4, "4"), "person sees more than the key"),
    ((None, "refused (403)"), (None, "refused (403)"), "no access either way"),
])
def test_the_comparison_verdicts(key, user, expected):
    assert probe_session.compare(key, user) == expected


def test_the_conclusion_follows_the_verdicts():
    assert "user_session" in probe_session.conclusion(["same view", "RESTRICTED (fewer records)"])
    assert "integration key cannot" in probe_session.conclusion(["person sees more than the key"])
    assert "exactly what the key sees" in probe_session.conclusion(["same view"])


def make_profile(**access):
    return build_profile({"name": "t", "access": access, "record_types": {"thing": {
        "doctype": "Thing", "label": "Things", "description": "t",
        "aliases": {"en": ["thing"]}, "employee_field": "employee",
        "fields": ["name", "employee"]}}})


def test_scope_lines_show_what_a_person_could_read(monkeypatch):
    monkeypatch.setattr(security.settings, "hr_admin_employees", "")
    monkeypatch.setattr(probe_roles.settings, "hr_admin_employees", "")
    profile = make_profile()
    plain = UserContext("u", "E1", ("Employee",))
    assert probe_roles.scope_lines(plain, profile, 0)[1:] == [
        "team     no  (no direct reports)", "company  no  (not HR)"]
    manager = UserContext("u", "E1", ("HR Manager",))
    assert probe_roles.scope_lines(manager, profile, 4)[1:] == [
        "team     yes (4 direct reports)", "company  yes (HR role)"]
    strict = make_profile(manager_roles=["Line Manager"])
    assert "requires a manager role" in probe_roles.scope_lines(plain, strict, 4)[1]


def test_an_explicit_hr_grant_is_labelled(monkeypatch):
    monkeypatch.setattr(probe_roles.settings, "hr_admin_employees", "E1, E9")
    lines = probe_roles.scope_lines(UserContext("u", "E1"), make_profile(), 0)
    assert lines[2] == "company  yes (listed in HR_ADMIN_EMPLOYEES)"


def test_advice_says_what_to_do_when_no_roles_are_found():
    assert "found by 'session'" in probe_roles.advice(RoleResult(("HR Manager",), "session"))
    assert "role_map" in probe_roles.advice(RoleResult((), "none"))
    assert "HR_ADMIN_EMPLOYEES" in probe_roles.advice(RoleResult((), "none"))