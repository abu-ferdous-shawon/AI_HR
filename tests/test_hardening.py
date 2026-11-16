
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.core.hardening import (
    OriginGuard, assert_safe_config, docs_settings, parse_origins,
)


ROOT = Path(__file__).resolve().parents[1]




# ---------- origin guard ----------

def client(allowed=()):
    app = FastAPI()
    app.add_middleware(OriginGuard, allowed=list(allowed))

    @app.post("/x")
    def post():
        return {"ok": True}

    @app.get("/x")
    def get():
        return {"ok": True}

    return TestClient(app)


@pytest.mark.parametrize("headers, status", [
    ({"Origin": "http://testserver"}, 200),
    ({"Origin": "http://TESTSERVER"}, 200),
    ({"Origin": "https://evil.example"}, 403),
    ({"Origin": "null"}, 403),
    ({}, 200),
    ({"Cookie": "sid=abc"}, 403),
    ({"Cookie": "sid=abc", "Sec-Fetch-Site": "same-origin"}, 200),
    ({"Sec-Fetch-Site": "cross-site"}, 403),
    ({"Sec-Fetch-Site": "same-site"}, 403),
    ({"Authorization": "token a:b", "Cookie": "sid=x"}, 200),
])
def test_post_requests_are_checked(headers, status):
    assert client().post("/x", headers=headers).status_code == status


def test_reads_are_never_blocked():
    assert client().get("/x", headers={"Origin": "https://evil.example"}).status_code == 200


def test_an_allowed_origin_passes():
    c = client(["https://hrm.example.com"])
    assert c.post("/x", headers={"Origin": "https://hrm.example.com"}).status_code == 200
    assert c.post("/x", headers={"Origin": "https://other.example.com"}).status_code == 403


def test_the_real_app_blocks_a_foreign_origin_before_login():
    assert any(m.cls is OriginGuard for m in main.app.user_middleware)
    r = TestClient(main.app).post("/chat", json={"message": "hi"},
                                  headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "blocked" in r.json()["detail"]


# ---------- startup checks ----------

def test_dev_mode_is_refused_in_production():
    with pytest.raises(RuntimeError, match="DEV_MODE"):
        assert_safe_config("production", True, [])
    assert_safe_config("production", False, [])
    assert_safe_config("development", True, [])


@pytest.mark.parametrize("bad", ["*", "hrm.example.com", "https://x.com/path", "https://*.x.com"])
def test_bad_origins_are_refused(bad):
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        assert_safe_config("production", False, [bad])


def test_origins_are_parsed_and_docs_hidden_in_production():
    assert parse_origins(" https://A.com/ , ,http://b.com ") == ["https://a.com", "http://b.com"]
    assert docs_settings("Production")["openapi_url"] is None
    assert docs_settings("development") == {}


def test_the_new_settings_exist_with_safe_defaults():
    fields = Settings.model_fields
    for name in ("environment", "allowed_origins", "log_level", "log_file", "log_json",
                 "log_max_mb", "log_backups"):
        assert name in fields, name
    assert fields["environment"].default == "development"
    assert fields["log_file"].default == ""


# ---------- deployment files ----------

UNIT = (ROOT / "deploy" / "hr-ai.service").read_text(encoding="utf-8")
ENV = (ROOT / "deploy" / "hr-ai.env.example").read_text(encoding="utf-8")


def test_the_service_binds_to_localhost_with_one_worker():
    assert "--host 127.0.0.1" in UNIT and "--port 8100" in UNIT
    assert "0.0.0.0" not in UNIT and "--workers" not in UNIT
    assert "--proxy-headers" in UNIT and "--forwarded-allow-ips=127.0.0.1" in UNIT
    assert "EnvironmentFile=/etc/hr-ai/hr-ai.env" in UNIT


def test_the_service_is_sandboxed():
    for line in ("User=hrai", "NoNewPrivileges=true", "ProtectSystem=strict",
                 "ProtectHome=true", "PrivateTmp=true", "UMask=0077",
                 "LogsDirectory=hr-ai", "Restart=on-failure"):
        assert line in UNIT, line


def test_the_env_example_is_production_safe_and_holds_no_secrets():
    values = dict(line.split("=", 1) for line in ENV.splitlines()
                  if "=" in line and not line.lstrip().startswith("#"))
    assert values["ENVIRONMENT"] == "production" and values["DEV_MODE"] == "false"
    assert values["FRAPPE_AUTH_MODE"] == "user_session" and values["LOG_JSON"] == "true"
    for secret in ("FRAPPE_API_KEY", "FRAPPE_API_SECRET", "GEMINI_API_KEY"):
        assert values[secret] == "", secret
    assert not any(p in ENV for p in ("AIza", "gsk_", "sk-"))