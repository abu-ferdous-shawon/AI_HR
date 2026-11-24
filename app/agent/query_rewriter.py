"""Resolve topic and date references without sending the user profile to a model."""
import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.agent.intent_router import route_intent
from app.core.llm.base import Message

_FOLLOW_UP = re.compile(
    r"^(?:and\b|what about\b|how about\b|same\b|(?:for\s+)?(?:this|last|next) (?:month|week|year)\b)"
    r"|\b(?:those records|that report|same report|same period)\b|\b(?:them|it)[.!?]*$", re.I,
)
_PERIOD = re.compile(r"\b(?:(this|last|next) (month|week|year)|(today|yesterday|tomorrow))\b", re.I)


@dataclass(frozen=True)
class RewrittenQuery:
    search_text: str
    previous_topic: str = ""
    date_range: tuple[str, str] | None = None


def _period(text: str, today: date) -> tuple[str, str] | None:
    match = _PERIOD.search(text)
    if not match:
        return None
    direction, _, day = match.group().lower().partition(" ")
    if not day:
        selected = today + timedelta(days={"today": 0, "yesterday": -1, "tomorrow": 1}[direction])
        return selected.isoformat(), selected.isoformat()
    step = {"last": -1, "this": 0, "next": 1}[direction]
    if day == "week":
        start = today - timedelta(days=today.weekday()) + timedelta(weeks=step)
        end = start + timedelta(days=6)
    elif day == "month":
        year, month = divmod(today.year * 12 + today.month - 1 + step, 12)
        start = date(year, month + 1, 1)
        end = date(year, month + 1, calendar.monthrange(year, month + 1)[1])
    else:
        start, end = date(today.year + step, 1, 1), date(today.year + step, 12, 31)
    return start.isoformat(), end.isoformat()


def rewrite_query(question: str, history: list[Message], today: date) -> RewrittenQuery:
    previous = ""
    if _FOLLOW_UP.search(question.strip()):
        for message in reversed(history):
            if (message.role == "user" and message.text and not _FOLLOW_UP.search(message.text.strip())
                    and route_intent(message.text).kind == "erp_read"):
                previous = message.text[:500]
                break
    return RewrittenQuery(
        search_text=f"{previous}\n{question}".strip(),
        previous_topic=previous,
        date_range=_period(question, today) or (_period(previous, today) if previous else None),
    )
