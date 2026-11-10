import base64

import pytest
from fastapi.testclient import TestClient

from app import main
from app.core.agent import AgentResult
from app.core.security import UserContext

CTX = UserContext("a@x.com", "HR-EMP-00001")
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
JPG = b"\xff\xd8\xff" + b"0" * 20


@pytest.fixture
def api(monkeypatch):
    seen = []

    def fake_agent(question, ctx, history=None, llm=None, vault=None, images=None):
        seen.append(images or [])
        return AgentResult("ok", [], [])

    monkeypatch.setattr(main, "run_agent", fake_agent)
    main.app.dependency_overrides[main.current_user] = lambda: CTX
    main._store.clear()
    yield TestClient(main.app), seen
    main.app.dependency_overrides.clear()
    main._store.clear()


def body(*images):
    return {"message": "look",
            "images": [{"mime_type": m, "data": base64.b64encode(d).decode()} for m, d in images]}


def test_a_valid_image_reaches_the_agent(api):
    client, seen = api
    assert client.post("/chat", json=body(("image/png", PNG))).status_code == 200
    assert seen[0][0].data == PNG and seen[0][0].mime_type == "image/png"


def test_a_mislabelled_file_is_refused(api):
    client, _ = api
    assert client.post("/chat", json=body(("image/png", JPG))).status_code == 422


def test_text_that_is_not_base64_is_refused(api):
    client, _ = api
    r = client.post("/chat", json={"message": "x", "images": [{"mime_type": "image/png", "data": "@@@"}]})
    assert r.status_code == 422


def test_too_many_images_are_refused(api):
    client, _ = api
    assert client.post("/chat", json=body(*[("image/png", PNG)] * 4)).status_code == 422


def test_an_oversized_image_is_refused(api):
    client, _ = api
    big = PNG + b"0" * (4 * 1024 * 1024)
    assert client.post("/chat", json=body(("image/png", big))).status_code == 413


def test_unsupported_types_are_refused(api):
    client, _ = api
    r = client.post("/chat", json={"message": "x", "images": [{"mime_type": "image/svg+xml", "data": "AAAA"}]})
    assert r.status_code == 422