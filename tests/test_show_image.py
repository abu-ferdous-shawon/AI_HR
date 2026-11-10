import pytest
from fastapi.testclient import TestClient

from app import main
from app.core import agent, media
from app.core.agent import run_agent
from app.core.frappe_client import FrappeClient
from app.core.llm.base import LLMProvider, LLMResponse, ToolCall
from app.core.security import UserContext
from app.tools.hr import images
from app.tools.registry import run_tool

CTX = UserContext("u@x.com", "HR-EMP-00001")
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


class FakeResponse:
    status_code = 200

    def iter_content(self, size):
        yield PNG

    def close(self):
        pass


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return FakeResponse()


class Fake(FrappeClient):
    """Real file-download code, with fake record lookups."""

    def __init__(self, stored_path):
        super().__init__("http://frappe.test:8001", "k", "s")
        self.stored_path = stored_path
        self.session = FakeSession()
        self.filters = None
        self.doctypes = []

    def get_list(self, doctype, fields=None, filters=None, order_by=None, limit=20, start=0):
        self.filters = filters
        self.doctypes.append(doctype)
        return [{"name": "HR-EMP-00001", "company": "Starkology", "user_id": "u@x.com",
                 "image": self.stored_path, "user_image": self.stored_path}]

    def get_doc(self, doctype, name):
        return {"company_logo": self.stored_path}


@pytest.fixture(autouse=True)
def clean():
    media._pictures.clear()
    yield
    media._pictures.clear()


def use(monkeypatch, fake):
    monkeypatch.setattr(images, "get_frappe_client", lambda: fake)
    return fake


def test_own_photo_is_stored_for_the_owner_only(monkeypatch):
    fake = use(monkeypatch, Fake("/private/files/me.png"))
    picture = run_tool("show_image", {"source": "employee_photo"}, CTX)["result"]["image"]
    assert fake.filters == {"name": "HR-EMP-00001"}
    assert media.get_picture(picture["id"], "u@x.com").data == PNG
    assert media.get_picture(picture["id"], "someone-else") is None


def test_the_model_cannot_pick_another_person(monkeypatch):
    fake = use(monkeypatch, Fake("/files/me.png"))
    run_tool("show_image", {"source": "employee_photo", "employee_id": "HR-EMP-00002"}, CTX)
    assert fake.filters == {"name": "HR-EMP-00001"}


def test_company_logo_works(monkeypatch):
    use(monkeypatch, Fake("/files/logo.png"))
    assert "image" in run_tool("show_image", {"source": "company_logo"}, CTX)["result"]


def test_no_picture_on_record(monkeypatch):
    use(monkeypatch, Fake(None))
    res = run_tool("show_image", {"source": "employee_photo"}, CTX)["result"]
    assert "image" not in res and "no picture" in res["message"]


def test_an_external_address_in_the_record_is_not_fetched(monkeypatch):
    fake = use(monkeypatch, Fake("https://evil.example/x.png"))
    res = run_tool("show_image", {"source": "employee_photo"}, CTX)["result"]
    assert "image" not in res and fake.session.calls == []


def test_unknown_source_is_refused():
    assert "error" in run_tool("show_image", {"source": "salary_slip"}, CTX)


def test_the_agent_hands_the_picture_to_the_person(monkeypatch):
    payload = {"result": {"image": {"id": "abc", "name": "employee_photo.png",
                                    "caption": "Your employee photo"}}}
    monkeypatch.setattr(agent, "run_tool", lambda name, args, ctx: payload)

    class Llm(LLMProvider):
        def generate(self, system_prompt, messages, tools=None):
            if messages[-1].role != "tool":
                return LLMResponse(tool_calls=[ToolCall("show_image", {"source": "employee_photo"})])
            return LLMResponse(text="Here is your photo.")

    result = run_agent("show my photo", CTX, llm=Llm())
    assert result.pictures == [payload["result"]["image"]]


def test_picture_download_is_owner_only():
    info = media.save_picture("a@x.com", "me.png", PNG, "image/png")
    client = TestClient(main.app)
    main.app.dependency_overrides[main.current_user] = lambda: UserContext("a@x.com", "HR-EMP-00001")
    try:
        r = client.get(f"/pictures/{info['id']}")
        assert r.status_code == 200 and r.content == PNG
        assert r.headers["content-type"] == "image/png"
        main.app.dependency_overrides[main.current_user] = lambda: UserContext("b@x.com", "HR-EMP-00002")
        assert client.get(f"/pictures/{info['id']}").status_code == 404
        assert client.get("/pictures/not-an-id").status_code == 422
    finally:
        main.app.dependency_overrides.clear()

def test_profile_picture_uses_the_callers_own_login(monkeypatch):
    fake = use(monkeypatch, Fake("/files/avatar.png"))
    res = run_tool("show_image", {"source": "profile_picture", "user": "admin@x.com"}, CTX)["result"]
    assert "image" in res
    assert fake.doctypes == ["Employee", "User"]     # the login came from the caller's own record
    assert fake.filters == {"name": "u@x.com"}       # the 'user' the model passed was ignored