"""Decide the language and script of a human-facing reply."""
import re
from typing import Literal

Language = Literal["bangla", "banglish", "other"]

# Common Banglish (Bangla in English letters) words. Add words your colleagues use.
BANGLISH_WORDS = {
    "amar", "ami", "tomar", "apnar", "apni", "koto", "kotha", "kon", "kobe", "kemon",
    "keno", "ki", "ache", "achhe", "nai", "nei", "baki", "din", "dekhao", "dekhan",
    "dao", "den", "bolo", "bolen", "chai", "chuti", "chhuti", "jonno", "theke",
    "porjonto", "hobe", "hoy", "thakbe", "niyechi", "nibo", "nite", "niye", "korbo",
    "korte", "lagbe", "pabo", "pai", "jodi", "tahole", "ebong", "sob", "mot", "hishab",
    "beton", "maina", "thikana", "chobi", "tarikh", "er", "chhutir", "thik",
}
_BANGLISH_COMMANDS = {"dekhao", "dekhan", "bolen", "janan"}
_BANGLA_QUERY_WORDS = frozenset({
    "আমার", "আমাকে", "আমি", "দেখাও", "দেখান", "বলুন", "বলো", "জানান", "কত", "কয়টি", "কয়টি",
    "কেমন", "কেন", "কবে", "কোন", "কী", "কি", "আছে", "করুন", "করো", "চাই", "দিন", "দাও",
})

HINTS = {
    "bangla": ("Write the entire human-facing reply in natural Bangla script. Translate "
               "DocType names, labels and readable field values, including leave types and statuses. "
               "For example: Casual Leave = নৈমিত্তিক ছুটি, Annual Leave = বার্ষিক ছুটি, "
               "Approved = অনুমোদিত. Transcribe people's and organizations' display names in Bangla script."),
    "banglish": ("Write the human-facing reply in natural Banglish (Bangla in Latin letters). "
                 "Translate DocType names, labels and readable field values into that style. "
                 "For example: Casual Leave = Noimittik chhuti, Approved = Onumodito. Use Latin letters."),
}


def detect_language(text: str) -> Language:
    """Returns 'bangla', 'banglish' or 'other'."""
    # Bangla letters and vowel signs, not the Bangla digits ০-৯
    bangla = sum(1 for ch in text if "\u0980" <= ch <= "\u09ff" and not "০" <= ch <= "৯")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    bangla_words = re.findall(r"[\u0980-\u09e5]+", text)
    if bangla and (not latin or bangla * 2 >= latin or _BANGLA_QUERY_WORDS.intersection(bangla_words)):
        return "bangla"
    words = re.findall(r"[a-z]+", text.lower())
    if _BANGLISH_COMMANDS.intersection(words) or sum(1 for word in words if word in BANGLISH_WORDS) >= 2:
        return "banglish"
    return "other"


def language_hint(text: str) -> str:
    return HINTS.get(detect_language(text), "")
