import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import main

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "frappe_app" / "hr_ai_widget"
PKG = APP / "hr_ai_widget"
LOADER = PKG / "public" / "js" / "hr_ai_loader.js"
LOADER_TEXT = LOADER.read_text(encoding="utf-8")


def hooks() -> dict:
    out = {}
    for node in ast.parse((PKG / "hooks.py").read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return out


def test_required_app_files_exist():
    for path in (APP / "pyproject.toml", PKG / "__init__.py", PKG / "hooks.py",
                 PKG / "modules.txt", PKG / "patches.txt", LOADER):
        assert path.is_file(), path


def test_every_module_in_modules_txt_has_a_folder():
    for line in (PKG / "modules.txt").read_text(encoding="utf-8").splitlines():
        if line.strip():
            folder = re.sub(r"[^a-z0-9]+", "_", line.strip().lower())
            assert (PKG / folder / "__init__.py").is_file(), line


def test_hooks_point_at_a_real_file():
    h = hooks()
    assert h["app_name"] == "hr_ai_widget"
    assert h["app_include_js"]
    for url in h["app_include_js"]:
        prefix = "/assets/hr_ai_widget/"
        assert url.startswith(prefix), url
        assert (PKG / "public" / url[len(prefix):]).is_file(), url


def test_pyproject_matches_the_app():
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads((APP / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == hooks()["app_name"]
    assert "frappe" in data["tool"]["bench"]["frappe-dependencies"]


def test_the_loader_loads_the_backends_widget_script():
    assert "'/ai'" in LOADER_TEXT and "/ai_chat.js" in LOADER_TEXT
    assert any(r.path == "/ai_chat.js" for r in main.app.routes)


def test_the_loader_is_safe_and_idempotent():
    assert "'Guest'" in LOADER_TEXT
    assert "__hrAiLoader" in LOADER_TEXT and "hr-ai-chat-script" in LOADER_TEXT
    for banned in ("innerHTML", "eval(", "document.write", "localStorage", "sessionStorage",
                   "Authorization", "document.cookie"):
        assert banned not in LOADER_TEXT, banned


def test_the_nginx_snippets_route_and_inject():
    text = (ROOT / "deploy" / "nginx_frappe_ai.conf").read_text(encoding="utf-8")
    assert "location /ai/" in text and "proxy_pass http://127.0.0.1:8100/;" in text
    assert "client_max_body_size" in text
    assert "sub_filter" in text and 'Accept-Encoding ""' in text
    assert "/ai/ai_chat.js" in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_loader_is_valid_javascript():
    done = subprocess.run(["node", "--check", str(LOADER)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr