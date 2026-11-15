import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main
from app.config import Settings
from app.core import frappe_client
from app.core.agent import AgentResult
from app.core.security import UserContext

REAL = UserContext("ann@x.com", "HR-EMP-00001", ("Employee",), "session")
DEV = UserContext("dev:HR-EMP-00001", "HR-EMP-00001")
SID = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


@pytest.fixture
def api(monkeypatch):
    seen = []

    def fake_agent(question, ctx, history=None, llm=None, vault=None, images=None):
        client = frappe_client.get_frappe_client()
        seen.append({"authorization": client.session.headers.get("Authorization"),
                     "cookie": client.session.headers.get("Cookie"),
                     "as_user": client.acts_as_user, "roles": ctx.roles})
        return AgentResult("ok", [], [])

    who = {"ctx": REAL}
    monkeypatch.setattr(main, "run_agent", fake_agent)
    main.app.dependency_overrides[main.current_user] = lambda: who["ctx"]
    main._store.clear()
    yield TestClient(main.app), seen, who
    main.app.dependency_overrides.clear()
    main._store.clear()


def chat(client, **headers):
    return client.post("/chat", json={"message": "hi"}, headers=headers)


def test_the_default_mode_is_user_session_and_bad_values_are_rejected():
    assert Settings.model_fields["frappe_auth_mode"].default == "user_session"
    with pytest.raises(ValidationError):
        Settings(frappe_auth_mode="oops")


def test_user_session_mode_reads_data_as_the_logged_in_user(api, monkeypatch):
    monkeypatch.setattr(main.settings, "frappe_auth_mode", "user_session")
    client, seen, _ = api
    assert chat(client, Authorization="token abc:def").status_code == 200
    assert seen[0]["as_user"] is True and seen[0]["authorization"] == "token abc:def"
    assert seen[0]["roles"] == ("Employee",)


def test_a_cookie_login_forwards_only_the_session_id(api, monkeypatch):
    monkeypatch.setattr(main.settings, "frappe_auth_mode", "user_session")
    client, seen, _ = api
    assert chat(client, Cookie=f"theme=dark; sid={SID}; other=1").status_code == 200
    assert seen[0]["cookie"] == f"sid={SID}" and seen[0]["authorization"] is None


def test_service_account_mode_never_uses_the_users_login(api, monkeypatch):
    monkeypatch.setattr(main.settings, "frappe_auth_mode", "service_account")
    client, seen, _ = api
    assert chat(client, Authorization="token abc:def").status_code == 200
    assert seen[0]["as_user"] is False and "abc:def" not in str(seen[0]["authorization"])


def test_a_real_login_without_usable_credentials_is_refused_not_downgraded(api, monkeypatch):
    monkeypatch.setattr(main.settings, "frappe_auth_mode", "user_session")
    client, seen, _ = api
    for headers in ({}, {"Authorization": "Basic dXNlcjpwYXNz"}, {"Cookie": "sid=Guest"}):
        assert client.post("/chat", json={"message": "hi"}, headers=headers).status_code == 401
    assert seen == []                                    # the agent never ran


def test_dev_requests_use_the_service_account_explicitly(api, monkeypatch):
    monkeypatch.setattr(main.settings, "frappe_auth_mode", "user_session")
    client, seen, who = api
    who["ctx"] = DEV
    assert chat(client).status_code == 200 and seen[0]["as_user"] is False


def test_every_request_uses_its_own_login(api, monkeypatch):
    monkeypatch.setattr(main.settings, "frappe_auth_mode", "user_session")
    client, seen, _ = api
    chat(client, Authorization="token a1:b1")
    chat(client, Authorization="token a2:b2")
    assert [s["authorization"] for s in seen] == ["token a1:b1", "token a2:b2"]


def test_credentials_do_not_leak_into_logs_or_responses(api, monkeypatch, caplog):
    monkeypatch.setattr(main.settings, "frappe_auth_mode", "user_session")
    caplog.set_level(logging.DEBUG)
    client, _, _ = api
    reply = chat(client, Authorization="token abc:def")
    assert "abc:def" not in reply.text and "abc:def" not in caplog.text


def test_the_login_check_hands_the_credentials_to_the_role_lookup(monkeypatch):
    got = {}
    monkeypatch.setattr(main, "identify_user", lambda authorization, cookie: "ann@x.com")

    def fake_resolve(user_id, credentials=None):
        got["credentials"] = credentials
        return REAL

    monkeypatch.setattr(main, "resolve_context", fake_resolve)
    ctx = main.current_user(None, "token abc:def", None, None)
    assert ctx is REAL and got["credentials"].headers() == {"Authorization": "token abc:def"}


def test_the_dev_shortcut_resolves_roles_without_credentials(monkeypatch):
    seen = {}

    class Directory:
        def get_list(self, *args, **kwargs):
            return [{"name": "HR-EMP-00004"}]

    def fake_build(user_id, employee_id, credentials=None):
        seen.update(user=user_id, employee=employee_id, credentials=credentials)
        return DEV

    monkeypatch.setattr(main, "_dev_active", lambda request: True)
    monkeypatch.setattr(main, "get_frappe_client", lambda: Directory())
    monkeypatch.setattr(main, "build_context", fake_build)
    assert main.current_user(None, None, None, "HR-EMP-00004") is DEV
    assert seen == {"user": "dev:HR-EMP-00004", "employee": "HR-EMP-00004", "credentials": None}