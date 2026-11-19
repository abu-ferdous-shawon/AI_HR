import re
from pathlib import Path

import pytest

from app.core import security
from app.core.frappe_client import (
    ServicePurposeRequired, enforce_user_login, get_service_client,
)
from app.core.roles import RoleResult
from app.core.security import UserContext
from app.core.service_purpose import active_purpose, for_purpose, service_purpose


@pytest.fixture
def strict():
    enforce_user_login(True)
    yield
    enforce_user_login(False)


def test_the_service_account_is_refused_without_a_purpose(strict):
    with pytest.raises(ServicePurposeRequired):
        get_service_client()


def test_a_declared_purpose_allows_it_only_inside_the_block(strict):
    with service_purpose("identity"):
        assert get_service_client() is not None
    with pytest.raises(ServicePurposeRequired):
        get_service_client()


def test_unknown_purposes_are_rejected_early():
    with pytest.raises(ValueError):
        with service_purpose("whatever"):
            pass
    with pytest.raises(ValueError):
        for_purpose("whatever")


def test_the_decorator_declares_and_then_clears_the_purpose():
    @for_purpose("team")
    def inside():
        return active_purpose()

    assert inside() == "team" and active_purpose() is None


def test_the_inner_purpose_wins_when_nested():
    with service_purpose("identity"):
        with service_purpose("roles"):
            assert active_purpose() == "roles"
        assert active_purpose() == "identity"


def test_scripts_and_tests_are_not_affected_when_not_strict():
    assert get_service_client() is not None


def test_the_real_lookups_declare_their_purpose(monkeypatch):
    seen = []

    class Directory:
        def get_list(self, *args, **kwargs):
            seen.append(active_purpose())
            return [{"name": "E5"}]

    monkeypatch.setattr(security, "get_service_client", lambda: Directory())
    monkeypatch.setattr(security, "resolve_roles",
                        lambda employee_id, credentials=None: RoleResult(("Employee",), "key"))
    security.resolve_context("a@x.com")
    security.get_team(UserContext("u", "E1"))
    assert seen == ["identity", "team"]


# Files allowed to use the service account. Adding a new file here is a security
# decision: it means that file only asks questions about structure.
REVIEWED = {
    "frappe_client/client.py", "auth/security.py", "auth/roles.py",
    "records/scope.py", "registry/probe.py", "registry/draft.py",
}


def test_only_reviewed_files_use_the_service_client():
    root = Path(__file__).resolve().parents[1] / "app"
    users = {p.relative_to(root).as_posix() for p in root.rglob("*.py")
             if re.search(r"\bget_service_client\b", p.read_text(encoding="utf-8"))}
    assert users <= REVIEWED, f"New files use the service account: {sorted(users - REVIEWED)}"