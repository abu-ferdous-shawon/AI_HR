import pytest
from fastapi.testclient import TestClient

from app import main
from app.core.agent import AgentResult
from app.core.llm.base import Message
from app.core.security import UserContext

A = UserContext("a@x.com", "HR-EMP-00001")
B = UserContext("b@x.com", "HR-EMP-00002")


@pytest.fixture
def api(monkeypatch):
    who = {"ctx": A}
    seen = []  # history length the "agent" received on each call

    def fake_agent(question, ctx, history=None, llm=None, vault=None, images=None):
        seen.append(len(history or []))
        msgs = list(history or []) + [Message("user", question),
                                      Message("assistant", text="ok")]
        return AgentResult("ok", msgs, [])

    monkeypatch.setattr(main, "run_agent", fake_agent)
    main.app.dependency_overrides[main.current_user] = lambda: who["ctx"]
    main._store.clear()
    yield TestClient(main.app), who, seen
    main.app.dependency_overrides.clear()
    main._store.clear()


def test_health_needs_no_login():
    assert TestClient(main.app).get("/health").status_code == 200


def test_chat_without_credentials_is_401():
    assert TestClient(main.app).post("/chat", json={"message": "hi"}).status_code == 401


def test_follow_up_remembers_the_conversation(api):
    client, _, seen = api
    cid = client.post("/chat", json={"message": "one"}).json()["conversation_id"]
    client.post("/chat", json={"message": "two", "conversation_id": cid})
    assert seen == [0, 2]  # second call received the first question and answer


def test_another_user_cannot_borrow_a_conversation_id(api):
    client, who, seen = api
    cid = client.post("/chat", json={"message": "one"}).json()["conversation_id"]
    who["ctx"] = B
    client.post("/chat", json={"message": "two", "conversation_id": cid})
    assert seen == [0, 0]  # B started with an empty history


@pytest.mark.parametrize("body", [
    {"message": ""},
    {"message": "x" * 1001},
    {"message": "hi", "conversation_id": "../../etc"},
])
def test_bad_input_is_rejected(api, body):
    client, _, _ = api
    assert client.post("/chat", json=body).status_code == 422


def test_trim_cuts_only_between_whole_turns():
    msgs = []
    for i in range(9):
        msgs += [Message("user", f"q{i}"), Message("tool", tool_name="t", tool_result={}),
                 Message("assistant", text="a")]
    kept = main.trim(msgs)
    assert kept[0].role == "user" and sum(m.role == "user" for m in kept) == 6