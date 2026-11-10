from fastapi.testclient import TestClient

from app import main

client = TestClient(main.app)  # TestClient calls come from "testclient", not localhost
HEADER = {"X-Dev-Employee": "HR-EMP-00001"}


def test_home_page_is_served():
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "HR Assistant" in r.text


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