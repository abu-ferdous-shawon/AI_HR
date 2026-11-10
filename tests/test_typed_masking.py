import re

import pytest

from app.core.agent import run_agent
from app.core.llm.base import LLMProvider, LLMResponse
from app.core.masking import Vault
from app.core.security import UserContext

CTX = UserContext("u@x.com", "HR-EMP-00001")


@pytest.mark.parametrize("typed", [
    "০১৭১১২৩৪৫৬৭৮",           # Bangla digits
    "01711234567",
    "+8801711234567",
    "01711-234567",
    "01711 234 567",
    "rahim@example.com",
    "1234567890",               # 10 digits, NID-like
    "19901234567890123",        # 17 digits
])
def test_typed_secrets_are_masked_and_restored_exactly(typed):
    vault = Vault()
    text = f"amar number {typed} thik ache?"
    masked = vault.mask_typed(text)
    assert typed not in masked and "[[MASK_" in masked
    assert vault.unmask(masked) == text


@pytest.mark.parametrize("text", [
    "Show attendance from 2026-01-01 to 2026-01-31",
    "HR-EMP-00002 er attendance dekhao",
    "আমার ১৫ দিন ছুটি বাকি আছে ২০২৬ সালে",
    "salary slip for 20260125",              # 8 digits: not an ID
    "leave 2026-03-01 2026-03-02",
])
def test_ordinary_text_is_left_alone(text):
    assert Vault().mask_typed(text) == text


def test_a_longer_number_is_masked_whole():
    masked = Vault().mask_typed("number 017112345678901 ache")
    assert "0171" not in masked


def test_bangla_values_from_the_system_are_masked_and_restored():
    vault = Vault()
    token = vault.mask({"current_address": "মিরপুর ১০, ঢাকা"})["current_address"]
    assert "মিরপুর" not in token
    assert vault.unmask(f"আপনার ঠিকানা {token}") == "আপনার ঠিকানা মিরপুর ১০, ঢাকা"


class EchoPlaceholder(LLMProvider):
    """Repeats the placeholder it was given, like a model that cannot see the number."""
    def __init__(self):
        self.seen = []

    def generate(self, system_prompt, messages, tools=None):
        self.seen.append(repr(messages))
        token = re.search(r"\[\[MASK_\d+\]\]", messages[-1].text).group(0)
        return LLMResponse(text=f"Apni {token} number-ti diyechhen.")


def test_the_model_never_sees_a_number_typed_in_bangla():
    typed = "০১৭১১২৩৪৫৬৭৮"
    llm = EchoPlaceholder()
    result = run_agent(f"আমার ফোন নম্বর {typed}, এটা কি ঠিক?", CTX, llm=llm)
    assert typed not in "".join(llm.seen)          # the model never saw it
    assert typed not in repr(result.messages)      # nor does the stored history
    assert typed in result.answer                  # the person gets it back