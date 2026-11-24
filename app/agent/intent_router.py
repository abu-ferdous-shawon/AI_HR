import re
from dataclasses import dataclass
from typing import Literal

from app.core.language import detect_language

_INTENT_WORDS = re.compile(r"\b(apply (?:for )?(?:leave|pto)|take action|create|add|insert|update|edit|change|set|submit|cancel|delete|remove|approve|reject|amend|make)\b", re.I)
_READ_QUALIFIER = re.compile(
    r"^(?:please\s+)?(?:(?:can|could) you\s+)?(?:"
    r"(?:how|where|when|what|who|why|is|are|list|show|find|which|count|explain|summarize|check|tell)\b)",
    re.I,
)
_OUTPUT = re.compile(r"\b(?:create|make|generate)\s+(?:(?:me|a|an|my|the|an? excel|a csv)\s+)*(?:report|csv|excel|spreadsheet|file)\b", re.I)
_REPORT_OUTPUT = re.compile(r"^(?:please\s+)?(?:create|make|export|generate)\b.*\b(?:csv|excel|spreadsheet|report|file)\s*[.!]?$", re.I)
_POLICY = re.compile(r"\b(?:policy|policies|handbook|entitlement|procedure|rules)\b|নীতিমালা|নীতি", re.I)
_SHORTCUTS = {
    "hello": {"hi", "hello", "hey", "good morning", "good afternoon", "good evening", "হ্যালো", "নমস্কার", "আসসালামু আলাইকুম", "assalamu alaikum"},
    "thanks": {"thanks", "thank you", "thanks a lot", "ধন্যবাদ", "dhonnobad"},
    "help": {"what can you do", "how can you help", "what can i ask you", "help", "what do you do"},
}
_REPLIES = {
    "hello": ("Hi! What can I help you with?", "হ্যালো! কী নিয়ে সাহায্য করতে পারি?", "Hello! Ki niye shahajjo korte pari?"),
    "thanks": ("You’re welcome!", "আপনাকে স্বাগতম!", "Apnake shagotom!"),
    "help": (
        "I can answer policy questions and look up Frappe records and reports your account can access.",
        "আমি নীতিমালা সম্পর্কে প্রশ্নের উত্তর দিতে এবং আপনার অনুমতিপ্রাপ্ত রেকর্ড ও রিপোর্ট খুঁজতে পারি।",
        "Ami policy niye proshner uttor dite pari ebong apnar access ache emon record o report khuje dite pari.",
    ),
    "write": (
        "I can look up records and explain the process. Making changes through chat requires the confirmation flow.",
        "আমি রেকর্ড খুঁজতে এবং প্রক্রিয়া বুঝিয়ে দিতে পারি। চ্যাট থেকে পরিবর্তন করতে নিশ্চিতকরণের ধাপ প্রয়োজন।",
        "Ami record khujte ebong process bujhiye dite pari. Chat theke poriborton korte confirmation lagbe.",
    ),
    "scope": (
        "Frappe denied access to that record or report for your account.",
        "আপনার অ্যাকাউন্ট দিয়ে এই রেকর্ড বা রিপোর্ট দেখার অনুমতি ফ্রাপে দেয়নি।",
        "Apnar account diye ei record ba report dekhar permission Frappe deyni.",
    ),
    "retry": (
        "I couldn’t complete that query. Please check the record ID, report filters, or date range and try again.",
        "অনুরোধটি সম্পন্ন করতে পারিনি। রেকর্ড আইডি, রিপোর্ট ফিল্টার বা তারিখের সীমা দেখে আবার চেষ্টা করুন।",
        "Query ta shesh korte parini. Record ID, report filter ba tarikh dekhe abar try korun.",
    ),
    "read_failed": (
        "Frappe could not complete that read. Please try again shortly.",
        "ফ্রাপে রেকর্ডটি পড়তে পারেনি। কিছুক্ষণ পর আবার চেষ্টা করুন।",
        "Frappe record ta porte pareni. Kichukhon por abar try korun.",
    ),
}


@dataclass(frozen=True)
class Intent:
    kind: Literal["conversational", "erp_read", "erp_write"]
    reply: str | None = None


def localized(key: str, question: str) -> str:
    language = detect_language(question)
    index = {"bangla": 1, "banglish": 2}.get(language, 0)
    if question.strip(" !?.").casefold() in {"dhonnobad", "assalamu alaikum"}:
        index = 2
    return _REPLIES[key][index]


def route_intent(question: str, *, has_image: bool = False) -> Intent:
    text = " ".join(question.casefold().strip(" !?.").split())
    if not has_image:
        for key, phrases in _SHORTCUTS.items():
            if text in phrases:
                return Intent("conversational", localized(key, question))
    if _INTENT_WORDS.search(text) and not _READ_QUALIFIER.search(text) and not (
        _OUTPUT.search(text) or _REPORT_OUTPUT.search(text)
    ):
        return Intent("erp_write", localized("write", question))
    if _POLICY.search(text) and not has_image:
        return Intent("conversational")
    if re.match(r"^(?:how|where) (?:do|can|should) i\b", text) and _INTENT_WORDS.search(text):
        return Intent("conversational")
    return Intent("erp_read")
