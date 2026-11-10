import re

from app.core import agent
from app.core.agent import run_agent
from app.core.llm.base import LLMProvider, LLMResponse, ToolCall
from app.core.security import UserContext

CTX = UserContext("u@x.com", "HR-EMP-00001")
PHONE = "01711000000"
PROFILE = {"result": {"details": {"cell_number": PHONE, "department": "IT"},
                      "no_information_on_record": ["blood_group"]}}


class AskThenAnswer(LLMProvider):
    """Calls the profile tool, then repeats whatever it was given."""
    def __init__(self):
        self.seen = []

    def generate(self, system_prompt, messages, tools=None):
        self.seen.append(repr(messages))          # everything the model is shown
        if messages[-1].role != "tool":
            return LLMResponse(tool_calls=[ToolCall("get_my_profile", {})])
        token = messages[-1].tool_result["result"]["details"]["cell_number"]
        return LLMResponse(text=f"Your phone number is {token}.")


class Echo(LLMProvider):
    """Answers a follow-up using a placeholder found in the history."""
    def generate(self, system_prompt, messages, tools=None):
        blob = " ".join(m.text for m in messages)
        token = re.search(r"\[\[MASK_\d+\]\]", blob).group(0)
        return LLMResponse(text=f"As I said: {token}")


def test_model_never_sees_confidential_values(monkeypatch):
    monkeypatch.setattr(agent, "run_tool", lambda name, args, ctx: PROFILE)
    llm = AskThenAnswer()
    r = run_agent("What is my phone number?", CTX, llm=llm)
    assert r.answer == f"Your phone number is {PHONE}."   # the person gets the real value
    assert PHONE not in "".join(llm.seen)                  # the model never did
    assert PHONE not in repr(r.messages)                   # nor does the stored history


def test_follow_up_turn_resolves_earlier_placeholders(monkeypatch):
    monkeypatch.setattr(agent, "run_tool", lambda name, args, ctx: PROFILE)
    r1 = run_agent("What is my phone number?", CTX, llm=AskThenAnswer())
    r2 = run_agent("Say it again", CTX, history=r1.messages, vault=r1.vault, llm=Echo())
    assert r2.answer == f"As I said: {PHONE}"
    assert PHONE not in repr(r2.messages)