from app.core import agent
from app.core.agent import run_agent
from app.core.llm.base import LLMProvider, LLMResponse
from app.core.masking import Vault
from app.core.security import UserContext

CTX = UserContext("u@x.com", "HR-EMP-00001")


class InventsThenFixes(LLMProvider):
    def __init__(self):
        self.calls = 0
        self.saw_note = False

    def generate(self, system_prompt, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(text="Tomar [[MASK_7]] din baki ache.")
        self.saw_note = "does not exist" in messages[-1].text
        return LLMResponse(text="Tomar 15 din baki ache.")


class AlwaysInvents(LLMProvider):
    def generate(self, system_prompt, messages, tools=None):
        return LLMResponse(text="Tomar [[MASK_9]] din baki ache.")


def test_unknown_placeholders_are_detected():
    vault = Vault()
    vault.mask({"cell_number": "111"})                      # creates [[MASK_1]]
    assert vault.unknown_placeholders("a [[MASK_1]] b [[ mask_5 ]]") == ["[[ mask_5 ]]"]


def test_an_invented_placeholder_triggers_one_correction():
    llm = InventsThenFixes()
    result = run_agent("amar leave koto din baki?", CTX, llm=llm)
    assert result.answer == "Tomar 15 din baki ache."
    assert llm.calls == 2 and llm.saw_note
    # the rejected answer and the note are not kept in the conversation
    assert [m.role for m in result.messages] == ["user", "assistant"]
    assert "MASK_7" not in repr(result.messages)


def test_a_second_invented_placeholder_gives_the_fallback_not_garbage():
    result = run_agent("amar leave koto din baki?", CTX, llm=AlwaysInvents())
    assert result.answer == agent.FALLBACK
    assert "MASK" not in result.answer and "not available" not in result.answer
    assert [m.role for m in result.messages] == ["user", "assistant"]