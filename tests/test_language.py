import pytest

from app.core import agent
from app.core.language import detect_language, language_hint
from app.core.masking import Vault


@pytest.mark.parametrize("text, expected", [
    ("আমার সিক লিভ কতদিন বাকি আছে?", "bangla"),
    ("আমার leave balance কত?", "bangla"),
    ("amar casual leave koto din baki ache?", "banglish"),
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