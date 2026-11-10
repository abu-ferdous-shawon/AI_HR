"""Decides which language and script the reply should use.

A small model sometimes answers a Bangla question in Banglish, or the other way
round. A one-line hint decided from the question's letters removes the guess.
"""
import re

# Common Banglish (Bangla in English letters) words. Add words your colleagues use.
BANGLISH_WORDS = {
    "amar", "ami", "tomar", "apnar", "apni", "koto", "kotha", "kon", "kobe", "kemon",
    "keno", "ki", "ache", "achhe", "nai", "nei", "baki", "din", "dekhao", "dekhan",
    "dao", "den", "bolo", "bolen", "chai", "chuti", "chhuti", "jonno", "theke",
    "porjonto", "hobe", "hoy", "thakbe", "niyechi", "nibo", "nite", "niye", "korbo",
    "korte", "lagbe", "pabo", "pai", "jodi", "tahole", "ebong", "sob", "mot", "hishab",
    "beton", "maina", "thikana", "chobi", "tarikh", "er",
}

HINTS = {
    "bangla": ("LANGUAGE FOR THIS REPLY: the person wrote in Bangla script. Reply in "
               "Bangla script (বাংলা অক্ষরে). Common HR words such as leave, attendance "
               "and salary slip may stay in English. Do not write Bangla words in "
               "English letters."),
    "banglish": ("LANGUAGE FOR THIS REPLY: the person wrote Banglish (Bangla in English "
                 "letters). Reply in Banglish, using English letters only. Do not use "
                 "Bangla script."),
}


def detect_language(text: str) -> str:
    """Returns 'bangla', 'banglish' or 'other'."""
    # Bangla letters and vowel signs, not the Bangla digits ০-৯
    bangla = sum(1 for ch in text if "\u0980" <= ch <= "\u09ff" and not "০" <= ch <= "৯")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if bangla >= 3 and bangla * 2 >= latin:
        return "bangla"
    words = re.findall(r"[a-z]+", text.lower())
    if sum(1 for word in words if word in BANGLISH_WORDS) >= 2:
        return "banglish"
    return "other"


def language_hint(text: str) -> str:
    return HINTS.get(detect_language(text), "")