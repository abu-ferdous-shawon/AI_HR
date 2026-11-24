import json

import pytest

from app.core import agent
from app.auth.security import UserContext
from app.core.answer_language import fallback_answer
from app.core.language import detect_language, language_hint
from app.core.llm.base import LLMResponse, ToolCall
from app.core.masking import Vault

USER = UserContext("employee@example.com", "HR-EMP-00001")


@pytest.mark.parametrize("text, expected", [
    ("আমার সিক লিভ কতদিন বাকি আছে?", "bangla"),
    ("আমার leave balance কত?", "bangla"),
    ("Latest Sales Invoice দেখান", "bangla"),
    ("কি?", "bangla"),
    ("amar casual leave koto din baki ache?", "banglish"),
    ("Sales Invoice dekhao", "banglish"),
    ("HR-EMP-00002 er attendance dekhao", "banglish"),
    ("how many casual leaves do I have left?", "other"),
    ("show my salary slip for March", "other"),
    ("", "other"),
])
def test_language_detection(text, expected):
    assert detect_language(text) == expected


def test_english_questions_get_no_hint():
    assert language_hint("what is my date of birth?") == ""


def test_the_hint_is_added_to_the_prompt():
    prompt = agent.build_system_prompt(Vault(), language_hint("আমার ছুটি কত?"))
    assert "Bangla script" in prompt


def test_placeholder_rules_appear_only_when_placeholders_exist():
    plain = agent.build_system_prompt(Vault())
    assert "[[MASK_" not in plain and "placeholder" not in plain.lower()
    used = Vault()
    used.mask({"cell_number": "111"})
    assert "[[MASK_1]]" in agent.build_system_prompt(used)


def test_bangla_leave_reply_translates_labels_and_values_without_changing_tool_data(monkeypatch):
    applications = [{"leave_type": "Casual Leave", "status": "Approved", "from_date": "2026-11-09"},
                    {"leave_type": "Annual Leave", "status": "Approved", "from_date": "2026-02-26"}]

    def read_tool(name, args, context):
        assert name == "get_my_leave_history" and args["status"] == "Approved"
        assert context == USER
        return {"result": {"applications": applications}}

    monkeypatch.setattr(agent, "run_tool", read_tool)

    class Model:
        calls = 0

        def generate(self, prompt, messages, tools):
            self.calls += 1
            assert "নৈমিত্তিক ছুটি" in prompt and "original Frappe names and values" in prompt
            if self.calls == 1:
                return LLMResponse(tool_calls=[ToolCall("get_my_leave_history", {"status": "Approved"})])
            assert messages[-1].tool_result["result"]["applications"] == applications
            return LLMResponse(text=("আপনার ২টি ছুটির আবেদন রয়েছে।\n\n1. **Casual Leave**\n"
                                     "- স্ট্যাটাস: Approved\n- তারিখ: 2026-11-09\n\n2. **Annual Leave**\n"
                                     "- Status: Approved\n- তারিখ: 2026-02-26"), raw=object())

    model = Model()
    result = agent.run_agent("আমার অনুমোদিত ছুটির আবেদন দেখান", USER, llm=model)
    assert model.calls == 2
    assert "1. **নৈমিত্তিক ছুটি**" in result.answer and "2. **বার্ষিক ছুটি**" in result.answer
    assert result.answer.count("অবস্থা: অনুমোদিত") == 2
    assert "2026-11-09" in result.answer and "2026-02-26" in result.answer
    assert not any(char.isascii() and char.isalpha() for char in result.answer)
    assert result.messages[-1].text == result.answer and result.messages[-1].raw is None


@pytest.mark.parametrize("question,first,translated", [
    ("আমার বিক্রয় চালান দেখান", "বিক্রয় চালান SINV-00025: Balance -120", "বিক্রয় চালান SINV-00025: বকেয়া -120"),
    ("Show my invoice", "বিক্রয় চালান SINV-00025: বকেয়া -120", "Sales invoice SINV-00025: balance -120"),
    ("Amar invoice dekhao", "বিক্রয় চালান SINV-00025: বকেয়া -120", "Apnar bikroy chalan SINV-00025 er bokeya -120"),
])
def test_mixed_answer_gets_one_read_only_translation(question, first, translated):
    class Model:
        calls = 0

        def generate(self, system, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(text=first)
            assert tools is None and "You are a translator" in system
            assert json.loads(messages[0].text) == {"answer": first}
            return LLMResponse(text=translated)

    model = Model()
    assert agent.run_agent(question, USER, llm=model).answer == translated
    assert model.calls == 2


@pytest.mark.parametrize("correction", [
    "বকেয়া 120। যোগাযোগ [[MASK_1]]।",
    "বকেয়া -120। যোগাযোগ [[MASK_8]]।",
    "বকেয়া -120। Contact [[MASK_1]]।",
])
def test_rewrite_rejects_changed_amounts_identifiers_or_remaining_english(correction):
    vault = Vault()
    secret = "private@example.com"
    masked = vault.mask({"email": secret})["email"]

    class Model:
        def generate(self, _system, messages, tools):
            assert secret not in repr(messages)
            if tools is None:
                return LLMResponse(text=correction)
            return LLMResponse(text=f"বকেয়া -120। Contact {masked}।")

    result = agent.run_agent("আমার তথ্য দেখান", USER, llm=Model(), vault=vault)
    assert result.answer == fallback_answer("bangla")


def test_translation_keeps_private_values_masked_until_return():
    vault = Vault()
    secret = "private@example.com"
    masked = vault.mask({"email": secret})["email"]

    class Model:
        def generate(self, _system, messages, tools):
            assert secret not in repr(messages)
            text = f"যোগাযোগের ঠিকানা {masked}।" if tools is None else f"Contact {masked}।"
            return LLMResponse(text=text)

    result = agent.run_agent("আমার যোগাযোগের ঠিকানা দেখান", USER, llm=Model(), vault=vault)
    assert result.answer == f"যোগাযোগের ঠিকানা {secret}।"
    assert secret not in repr(result.messages)
