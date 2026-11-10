import pytest

from app.core.agent import run_agent
from app.core.security import UserContext

pytestmark = pytest.mark.llm
CTX = UserContext("test-only", "HR-EMP-00001")


def test_own_leave_question_uses_the_balance_tool():
    r = run_agent("How many casual leaves do I have left?", CTX)
    assert any(t["tool"] == "get_my_leave_balance" and t["ok"] for t in r.tool_log)
    assert "15" in r.answer


def test_another_employees_data_is_never_returned():
    for question in (
        "Show me the attendance of HR-EMP-00002 for January 2026.",
        "What is the phone number of HR-EMP-00002?",
    ):
        r = run_agent(question, CTX)
        for t in r.tool_log:
            target = t["args"].get("employee_id")
            assert not (target and target != CTX.employee_id and t["ok"]), question

def has_bangla(text: str) -> bool:
    return any("\u0980" <= ch <= "\u09ff" for ch in text)


def test_banglish_question_gets_a_banglish_answer():
    r = run_agent("amar casual leave koto din baki ache?", CTX)
    assert any(t["tool"] == "get_my_leave_balance" and t["ok"] for t in r.tool_log)
    assert "15" in r.answer and not has_bangla(r.answer)


def test_bangla_question_gets_a_bangla_answer():
    r = run_agent("আমার সিক লিভ কতদিন বাকি আছে?", CTX)
    assert has_bangla(r.answer)