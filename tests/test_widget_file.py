import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main

JS = Path(main.STATIC_DIR) / "ai_chat.js"
TEXT = JS.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ["/ai_chat.js", "/chat_message.js", "/voice_input.js"])
def test_the_script_is_served_with_safe_headers(path):
    r = TestClient(main.app).get(path)
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_it_uses_the_frappe_session_cookie_and_stores_no_credentials():
    assert TEXT.count("credentials: 'same-origin'") >= 3     # chat, report, picture
    for banned in ("sessionStorage", "localStorage", "Authorization", "document.cookie"):
        assert banned not in TEXT, banned


def test_it_never_builds_html_from_text():
    renderer = (main.STATIC_DIR / "chat_message.js").read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in TEXT, banned
        assert banned not in renderer, banned


def test_both_interfaces_load_the_same_reply_renderer():
    page = (main.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "import('/chat_message.js')" in page
    assert "import(BASE + '/chat_message.js')" in TEXT


def test_both_interfaces_use_the_same_voice_input():
    page = (main.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "import('/voice_input.js')" in page and "endpoint: '/stt'" in page
    assert "import(BASE + '/voice_input.js')" in TEXT and "endpoint: BASE + '/stt'" in TEXT


def test_it_shows_only_the_answer_never_tool_output():
    assert "data.answer" in TEXT
    for banned in ("tool_log", "tool_result", "tool_calls"):
        assert banned not in TEXT, banned


def test_it_calls_the_expected_endpoints_under_a_configurable_base():
    assert "HR_AI_BASE" in TEXT and "'/ai'" in TEXT
    for path in ("/chat", "/reports/", "/pictures/"):
        assert path in TEXT


@pytest.mark.parametrize("status", ["401", "403", "413", "422", "429", "503"])
def test_every_error_status_has_a_message(status):
    assert f"res.status === {status}" in TEXT


def test_images_and_isolation_are_kept():
    assert "MAX_IMAGES = 3" in TEXT and "toDataURL('image/jpeg'" in TEXT
    assert "attachShadow" in TEXT
    assert "isComposing" in TEXT
    assert "'Guest'" in TEXT                                  # hidden on the login page


def test_the_message_limit_matches_the_backend():
    assert f"MAX_CHARS = {main.ChatRequest.model_fields['message'].metadata[1].max_length}" in TEXT


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_script_is_valid_javascript():
    done = subprocess.run(["node", "--check", str(JS)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
