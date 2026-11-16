import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.core import audit, gateway
from app.core.agent import AgentResult
from app.core.llm.base import LLMBusy, LLMError, Message
from app.core.ratelimit import RateLimiter
from app.core.security import AccessDenied, UserContext

A = UserContext("a@x.com", "HR-EMP-00001", ("Employee",), "session")
B = UserContext("b@x.com", "HR-EMP-00002")
TOOL_LOG = [{"tool": "get_employee_records",
             "args": {"record_type": "leave", "employee": "Rahim Uddin"}, "ok": False,
             "error": "'Rahim Uddin' is not one of your direct reports", "kind": "denied",
             "rows": 3, "scope": "team", "target": "HR-EMP-00003"}]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def rows(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def api(monkeypatch, tmp_path):
    who, behaviour = {"ctx": A}, {"error": None}

    def fake_agent(question, ctx, history=None, llm=None, vault=None, images=None):
        if behaviour["error"]:
            raise behaviour["error"]
        return AgentResult("ok", [Message("user", question),
                                  Message("assistant", text="ok")], list(TOOL_LOG))

    monkeypatch.setattr(main, "run_agent", fake_agent)
    main.app.dependency_overrides[main.current_user] = lambda: who["ctx"]
    main._store.clear()
    path = tmp_path / "audit.jsonl"
    audit.configure(path)
    yield TestClient(main.app), who, behaviour, path
    audit.close()
    main.app.dependency_overrides.clear()
    main._store.clear()


# ---- rate limiter ----

def test_limiter_blocks_then_recovers():
    clock = Clock()
    limiter = RateLimiter(2, window=60, clock=clock)
    assert limiter.hit("k") == 0 and limiter.hit("k") == 0
    assert limiter.hit("k") == 60
    assert limiter.hit("other") == 0
    clock.now = 159
    assert limiter.hit("k") == 1
    clock.now = 160
    assert limiter.hit("k") == 0


def test_blocked_counts_nothing_and_zero_means_off():
    limiter = RateLimiter(1)
    assert all(limiter.blocked("k") == 0 for _ in range(5))
    assert all(RateLimiter(0).hit("k") == 0 for _ in range(100))


def test_limiter_forgets_the_oldest_keys():
    limiter = RateLimiter(5, max_keys=2)
    for key in "abc":
        limiter.hit(key)
    assert len(limiter._hits) == 2 and "a" not in limiter._hits


# ---- client address ----

def fake_request(host, **headers):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers)


def test_proxy_headers_are_ignored_unless_trusted(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", False)
    assert gateway.client_ip(fake_request("127.0.0.1", **{"x-real-ip": "10.1.2.3"})) == "127.0.0.1"
    monkeypatch.setattr(settings, "trust_proxy", True)
    assert gateway.client_ip(fake_request("127.0.0.1", **{"x-real-ip": "10.1.2.3"})) == "10.1.2.3"
    assert gateway.client_ip(fake_request("127.0.0.1", **{"x-forwarded-for": "6.6.6.6, 10.1.2.3"})) == "10.1.2.3"
    assert gateway.client_ip(fake_request("127.0.0.1", **{"x-real-ip": "garbage"})) == "127.0.0.1"
    assert gateway.client_ip(fake_request("8.8.8.8", **{"x-real-ip": "10.1.2.3"})) == "8.8.8.8"


def test_dev_mode_is_refused_behind_a_proxy(monkeypatch):
    monkeypatch.setattr(main.settings, "dev_mode", True)
    assert main._dev_active(fake_request("127.0.0.1")) is True
    assert main._dev_active(fake_request("127.0.0.1", **{"x-real-ip": "1.2.3.4"})) is False


# ---- the API ----

def test_chat_is_rate_limited_per_user(api, monkeypatch):
    client, who, _, path = api
    monkeypatch.setattr(gateway, "chat_limiter", RateLimiter(2))
    assert client.post("/chat", json={"message": "one"}).status_code == 200
    assert client.post("/chat", json={"message": "two"}).status_code == 200
    r = client.post("/chat", json={"message": "three"})
    assert r.status_code == 429 and "wait" in r.json()["detail"] and "retry-after" in r.headers
    who["ctx"] = B
    assert client.post("/chat", json={"message": "hi"}).status_code == 200
    assert [x["status"] for x in rows(path)] == ["ok", "ok", "rate_limited", "ok"]


def test_a_chat_is_audited_without_personal_values(api):
    client, _, _, path = api
    text = "my phone is 01711234567"
    assert client.post("/chat", json={"message": text}).status_code == 200
    row = rows(path)[0]
    assert (row["event"], row["user"], row["employee"], row["status"]) == (
        "chat", "a@x.com", "HR-EMP-00001", "ok")
    assert row["roles"] == ["Employee"] and row["role_source"] == "session"
    assert row["message_chars"] == len(text) and len(row["request_id"]) == 32
    tool = row["tools"][0]
    assert tool["kind"] == "denied" and tool["rows"] == 3 and tool["target"] == "HR-EMP-00003"
    assert tool["args"]["employee"].startswith("<hidden")
    raw = path.read_text(encoding="utf-8")
    for secret in ("01711234567", "Rahim", "not one of your"):
        assert secret not in raw


def test_llm_failures_and_bad_images_are_audited(api):
    client, _, behaviour, path = api
    behaviour["error"] = LLMBusy("busy")
    assert client.post("/chat", json={"message": "x"}).status_code == 503
    behaviour["error"] = LLMError("down")
    assert client.post("/chat", json={"message": "x"}).status_code == 503
    behaviour["error"] = None
    bad = {"message": "x", "images": [{"mime_type": "image/png", "data": "@@@"}]}
    assert client.post("/chat", json=bad).status_code == 422
    assert [r["status"] for r in rows(path)] == ["llm_busy", "llm_error", "rejected_422"]


def test_repeated_bad_logins_lock_the_address_out(api, monkeypatch):
    client, _, _, path = api
    main.app.dependency_overrides.clear()

    def refuse(authorization, cookie):
        raise AccessDenied("Your sign-in is not valid.")

    monkeypatch.setattr(main, "identify_user", refuse)
    monkeypatch.setattr(gateway, "auth_failures", RateLimiter(2))
    headers = {"Authorization": "token abc:def"}
    codes = [client.post("/chat", json={"message": "x"}, headers=headers).status_code
             for _ in range(3)]
    assert codes == [401, 401, 429]
    assert [r["reason"] for r in rows(path)] == ["bad_login", "bad_login", "locked_out"]
    assert "abc:def" not in path.read_text(encoding="utf-8")


def test_responses_carry_security_headers_and_a_request_id(api):
    client, *_ = api
    r = client.get("/health")
    assert len(r.headers["x-request-id"]) == 32
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert client.post("/chat", json={"message": "x"}).headers["cache-control"] == "no-store"


def test_oversized_requests_are_refused_early(api, monkeypatch):
    client, *_ = api
    monkeypatch.setattr(settings, "max_request_bytes", 50)
    r = client.post("/chat", json={"message": "x" * 100})
    assert r.status_code == 413 and "too large" in r.json()["detail"]


def test_downloads_are_audited(api):
    client, _, _, path = api
    assert client.get("/reports/" + "0" * 32).status_code == 404
    row = rows(path)[0]
    assert (row["event"], row["kind"], row["status"]) == ("download", "report", "not_found")