import logging

import pytest

from app.core import roles
from app.core.credentials import extract_credentials
from app.core.frappe_client import FrappeError
from app.registry import build_profile

CREDS = extract_credentials("token abc:def", None)
EMPLOYEE = {"name": "E1", "user_id": "ann@x.com", "designation": "Clerk", "department": "Sales"}


def profile(**access):
    return build_profile({
        "name": "t", "access": access,
        "record_types": {"thing": {
            "doctype": "Thing", "label": "Things", "description": "t",
            "aliases": {"en": ["thing"]}, "employee_field": "employee",
            "fields": ["name", "employee"]}}})


class Client:
    """One fake ERP connection."""

    def __init__(self, user_roles=None, method_roles=None, user_error=None,
                 method_error=None, employee=None):
        self.user_roles, self.method_roles = user_roles, method_roles
        self.user_error, self.method_error = user_error, method_error
        self.employee = employee or dict(EMPLOYEE)
        self.calls = []
        self.session = type("Session", (), {"close": lambda self: None})()

    def get_list(self, doctype, fields=None, filters=None, order_by=None, limit=20, start=0):
        self.calls.append("employee")
        return [self.employee]

    def get_doc(self, doctype, name):
        self.calls.append("user record")
        if self.user_error:
            raise self.user_error
        return {"roles": [{"role": r} for r in self.user_roles or []]}

    def call_method(self, method, params=None):
        self.calls.append("method")
        if self.method_error:
            raise self.method_error
        return self.method_roles


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    roles.clear_role_cache()
    monkeypatch.setattr(roles.settings, "role_cache_seconds", 0)
    yield
    roles.clear_role_cache()


def run(monkeypatch, session, key, prof=None, creds=CREDS):
    monkeypatch.setattr(roles, "_session_client", lambda credentials: session)
    monkeypatch.setattr(roles, "get_service_client", lambda: key)
    return roles.resolve_roles("E1", creds,
                               profile=prof or profile(role_methods=["pkg.get_roles"]))


def test_the_users_own_session_wins_and_the_key_is_not_asked(monkeypatch):
    session, key = Client(user_roles=["Employee", "HR Manager"]), Client()
    result = run(monkeypatch, session, key)
    assert (result.source, result.roles) == ("session", ("Employee", "HR Manager"))
    assert key.calls == ["employee"]                    # only the lookup of who the person is


def test_when_the_session_cannot_tell_the_key_is_tried(monkeypatch):
    session = Client(user_roles=[], method_error=FrappeError(404, "no"))
    key = Client(method_roles=["HR User", "All", "Guest"])
    result = run(monkeypatch, session, key)
    assert (result.source, result.roles) == ("key", ("HR User",))     # automatic roles dropped
    assert result.notes == (
        "session: user record: no roles returned",
        "session: method pkg.get_roles: failed (404)",
        "key: user record: no roles returned",
        "key: method pkg.get_roles: ok")


def test_roles_that_only_the_automatic_ones_are_inconclusive(monkeypatch):
    result = run(monkeypatch, Client(user_roles=["All", "Guest", "Desk User"]), Client())
    assert result.source == "none" and result.roles == ()


def test_a_method_that_returns_something_else_is_ignored(monkeypatch):
    result = run(monkeypatch, Client(method_roles={"roles": ["X"]}), Client(method_roles="X"))
    assert result.source == "none"


def test_denials_are_recorded_without_leaking_credentials(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    session = Client(user_error=FrappeError(403, "x"), method_error=FrappeError(403, "x"))
    key = Client(user_error=FrappeError(403, "x"), method_error=FrappeError(500, "x"))
    result = run(monkeypatch, session, key)
    text = " | ".join(result.notes)
    assert result.source == "none" and result.roles == ()
    assert "denied (403)" in text and "failed (500)" in text
    assert "abc:def" not in text and "abc:def" not in caplog.text


@pytest.mark.parametrize("rule, expected", [
    ({"role": "HR Manager", "designations": ["clerk"]}, ("HR Manager",)),     # case-insensitive
    ({"role": "HR Manager", "departments": ["Sales"]}, ("HR Manager",)),
    ({"role": "HR Manager", "employees": ["E1"]}, ("HR Manager",)),
    ({"role": "HR Manager", "users": ["ANN@x.com"]}, ("HR Manager",)),
    ({"role": "HR Manager", "designations": ["Director"]}, ()),
])
def test_the_fallback_mapping(monkeypatch, rule, expected):
    result = run(monkeypatch, Client(), Client(), profile(role_map=[rule]))
    assert result.roles == expected
    assert result.source == ("mapping" if expected else "none")


def test_mapping_is_used_only_when_the_erp_cannot_tell(monkeypatch):
    prof = profile(role_map=[{"role": "HR Manager", "employees": ["E1"]}])
    result = run(monkeypatch, Client(user_roles=["Employee"]), Client(), prof)
    assert (result.source, result.roles) == ("session", ("Employee",))


def test_an_employee_without_a_login_can_still_be_mapped(monkeypatch):
    employee = {"name": "E1", "user_id": None, "designation": "HR Manager", "department": None}
    prof = profile(role_map=[{"role": "HR Manager", "designations": ["HR Manager"]}])
    session = Client()
    result = run(monkeypatch, session, Client(employee=employee), prof)
    assert result.source == "mapping" and "no linked login" in " ".join(result.notes)
    assert session.calls == []


def test_without_user_credentials_only_the_key_is_used(monkeypatch):
    session, key = Client(user_roles=["X"]), Client(user_roles=["Employee"])
    result = run(monkeypatch, session, key, creds=None)
    assert (result.source, result.roles) == ("key", ("Employee",))
    assert session.calls == []
    assert "session: skipped (no user credentials)" in result.notes


def test_a_failed_employee_lookup_fails_closed(monkeypatch):
    class Broken(Client):
        def get_list(self, *args, **kwargs):
            raise FrappeError(500, "boom")

    result = run(monkeypatch, Client(user_roles=["HR Manager"]), Broken())
    assert result.source == "none" and result.roles == ()
    assert "employee lookup failed (500)" in result.notes


def test_roles_are_cached_for_a_short_time(monkeypatch):
    monkeypatch.setattr(roles.settings, "role_cache_seconds", 60)
    session, key = Client(user_roles=["Employee"]), Client()
    run(monkeypatch, session, key)
    made = len(session.calls) + len(key.calls)
    run(monkeypatch, session, key)
    assert len(session.calls) + len(key.calls) == made          # served from the cache
    roles.clear_role_cache()
    run(monkeypatch, session, key)
    assert len(session.calls) + len(key.calls) > made


def test_the_source_is_logged(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    run(monkeypatch, Client(user_roles=["Employee"]), Client())
    assert "roles of E1 found by session (1 roles)" in caplog.text