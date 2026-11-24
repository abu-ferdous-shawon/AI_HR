from fastapi.testclient import TestClient

from app import main
from app.auth.security import UserContext

client = TestClient(main.app)  # TestClient calls come from "testclient", not localhost
HEADER = {"X-Dev-Employee": "HR-EMP-00001"}


def test_home_page_is_served():
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "HR Assistant" in r.text


def test_welcome_requires_a_login_and_returns_only_the_display_name(monkeypatch):
    assert client.get("/chat/welcome").status_code == 401
    user = UserContext("worker@example.com", "EMP-1", display_name="Test Employee")
    monkeypatch.setitem(main.app.dependency_overrides, main.current_user, lambda: user)
    response = client.get("/chat/welcome")
    assert response.status_code == 200 and response.json() == {"display_name": "Test Employee"}
    assert response.headers["cache-control"] == "no-store"


def test_dev_header_is_ignored_when_dev_mode_is_off(monkeypatch):
    monkeypatch.setattr(main.settings, "dev_mode", False)
    assert client.post("/chat", json={"message": "hi"}, headers=HEADER).status_code == 401


def test_dev_header_is_ignored_from_non_local_clients(monkeypatch):
    monkeypatch.setattr(main.settings, "dev_mode", True)
    assert client.post("/chat", json={"message": "hi"}, headers=HEADER).status_code == 401


def test_dev_employee_list_is_hidden_from_non_local_clients(monkeypatch):
    monkeypatch.setattr(main.settings, "dev_mode", True)
    assert client.get("/dev/employees").status_code == 404
    assert client.get("/config").json() == {"dev": False}
