import datetime
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.answer_language import fallback_answer, localize_answer
from app.core.answer_text import strip_source_references
from app.core.language import HINTS, detect_language
from app.core.llm import get_llm
from app.core.llm.base import ImageData, LLMProvider, Message
from app.core.masking import Vault
from app.auth.security import UserContext
from app.agent.intent_router import localized
from app.core.audit import describe_result
from app.core.turn_trace import current_trace
from app.tools.registry import ToolExecution, get_tool_specs, load_all_tools, run_tool, take_execution

if TYPE_CHECKING:
    from app.agent.retry_handler import ReadRetry
    from app.mutations.service import PreparedMutation

log = logging.getLogger("hr_ai.agent")

MAX_STEPS = 8  # most model <-> tool rounds for one question (room for look up + calculate + policy)
PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.txt"
# Added to the instructions only once placeholders exist in the conversation.
# A model that is told about placeholders when there are none starts inventing them.
PLACEHOLDER_RULES = """\
Placeholders are in use in this conversation. A confidential value may reach you as a placeholder like [[MASK_1]]. Use a placeholder ONLY when a tool result or the person's message actually contains that exact placeholder; copy it exactly, character for character, where the value belongs, for example "Your net pay is [[MASK_1]]." Keep numbers, dates and record identifiers exact. Translate ordinary display text into the reply language. Never invent a placeholder, never guess what one stands for, and never change, split or reformat one. To add, subtract, compare or take a percentage of placeholder values, call calculate with the placeholders inside the expression, for example calculate("[[MASK_1]] - [[MASK_2]]"); the result comes back as a new placeholder that you copy into your reply. For a comparison, report the difference. A placeholder can also stand for something the person typed, such as a phone number or an ID number; treat it as given and do not ask them to repeat it."""


def retry_note(question: str) -> str:
    """Sent once if the model writes a placeholder that does not exist."""
    return (
        "System note: your last answer used a placeholder like [[MASK_1]] that does "
        "not exist. Keep every number, date and record identifier exactly as the tool results "
        "show them. Answer again, in the same language and script as the original "
        f"question, which was: {question}"
    )


@dataclass
class AgentResult:
    answer: str                # real values restored: shown to the person only
    messages: list[Message]    # masked conversation: safe to give to the model again
    tool_log: list[dict] = field(default_factory=list)  # audit trail
    vault: Vault = field(default_factory=Vault)         # keep it with the conversation
    files: list[dict] = field(default_factory=list)     # reports created for the person
    pictures: list[dict] = field(default_factory=list)  # pictures to show the person
    mutation: "PreparedMutation | None" = None  # prepared data stays server-side until the API creates a card


def build_system_prompt(vault: Vault | None = None, hint: str = "") -> str:
    today = datetime.date.today()
    text = PROMPT_FILE.read_text(encoding="utf-8")
    text += ("\n\nSite data policy: Some fields are excluded from chat and reports. "
             "If a requested value is unavailable, say you cannot provide it. "
             "Use only values the permitted tools returned.")
    if vault is not None and vault.by_token:     # placeholder rules only when they apply
        text += "\n\n" + PLACEHOLDER_RULES
    if hint:
        text += "\n\n" + hint
    return text.replace("{today}", f"{today.strftime('%A, %d %B %Y')} ({today.isoformat()})")


def _forget_images(messages: list[Message]) -> None:
    """Pictures are used for the question they came with, then dropped from memory."""
    for m in messages:
        if m.images:
            m.text = f"{m.text} [{len(m.images)} image(s) were attached]".strip()
            m.images = []


def _tidy(messages: list[Message], rejected_at: int | None) -> None:
    """Forget a rejected answer and the correction note, and any attached images."""
    if rejected_at is not None:
        del messages[rejected_at:rejected_at + 2]
    _forget_images(messages)


def run_agent(question: str, ctx: UserContext,
              history: list[Message] | None = None,
              llm: LLMProvider | None = None,
              vault: Vault | None = None,
              images: list[ImageData] | None = None, *,
              guidance: str = "", allowed_tools: frozenset[str] | None = None,
              contextual_tools: frozenset[str] = frozenset(),
              read_retry: "ReadRetry | None" = None) -> AgentResult:
    llm = llm or get_llm()
    vault = vault if vault is not None else Vault()
    if not get_tool_specs():
        load_all_tools()

    language = detect_language(question)
    hint, fallback = HINTS.get(language, ""), fallback_answer(language)
    tools = get_tool_specs(include_contextual=True) if contextual_tools else get_tool_specs()
    if allowed_tools is not None:
        tools = [spec for spec in tools if spec["name"] in allowed_tools]
    exposed = {spec["name"]: spec["parameters"].get("properties", {}) for spec in tools}
    trace = current_trace()
    # Phone numbers, emails and long ID numbers the person typed are masked too.
    # The conversation keeps the masked text, so they never reach the model later either.
    typed = vault.mask_typed(question)
    messages = list(history or []) + [Message("user", typed, images=list(images or []))]
    tool_log: list[dict] = []
    files: list[dict] = []
    pictures: list[dict] = []
    retried = False
    rejected_at: int | None = None   # where a rejected answer and its note sit in messages
    for _ in range(MAX_STEPS):
        correction = read_retry.begin() if read_retry else None
        if correction and trace:
            trace.retry_count = read_retry.count
        system = build_system_prompt(vault, hint)   # rebuilt each round: placeholders may exist now
        if guidance:
            system += "\n\n" + guidance
        reply = llm.generate(system, messages, tools)

        if not reply.tool_calls:  # the model answered in words
            original_answer = reply.text.strip()
            masked_answer = strip_source_references(original_answer) or fallback
            if correction and not masked_answer.endswith("?"):
                masked_answer, reply.raw = localized("retry", question), None
            invented = vault.unknown_placeholders(masked_answer)

            if invented and not retried:
                # The model made up a placeholder. Ask once for a corrected answer.
                log.warning("user=%s model invented placeholders %s", ctx.user_id, invented)
                retried = True
                rejected_at = len(messages)
                messages.append(Message("assistant", text=masked_answer, raw=reply.raw))
                messages.append(Message("user", retry_note(typed)))
                continue

            if invented:   # still wrong after the correction: never show a garbled answer
                masked_answer, reply.raw = fallback, None

            masked_answer = strip_source_references(localize_answer(masked_answer, language, llm)) or fallback
            if masked_answer != original_answer:
                reply.raw = None

            # The conversation keeps the MASKED answer. Only the person gets real values.
            messages.append(Message("assistant", text=masked_answer, raw=reply.raw))
            _tidy(messages, rejected_at)
            return AgentResult(vault.unmask(masked_answer), messages, tool_log,
                               vault, files, pictures)

        messages.append(Message("assistant", text=reply.text,
                                tool_calls=reply.tool_calls, raw=reply.raw))
        stop = "retry" if correction and not correction.accepts(reply.tool_calls) else None
        if stop and trace:
            trace.error("correction", "unexpected_tool", retry=trace.retry_count)
        for call in reply.tool_calls:
            started = time.monotonic()
            args, secret, result = call.args, False, None
            execution = ToolExecution()

            if stop or (read_retry and read_retry.pending):
                result = {"error": "This tool call was skipped."}
                execution = ToolExecution("skipped")
            elif allowed_tools is not None and call.name not in exposed:
                result = {"error": "This tool is unavailable for the current request."}
                execution = ToolExecution("denied")

            # A calculation may contain placeholders. The real numbers are filled in
            # HERE, on the server, and the answer is hidden behind a new placeholder.
            if result is None and call.name == "calculate" and isinstance((call.args or {}).get("expression"), str):
                try:
                    expression, secret = vault.reveal_numbers(call.args["expression"])
                    args = {**call.args, "expression": expression}
                except ValueError as exc:
                    result = {"error": str(exc)}
                    execution = ToolExecution("invalid")
            if result is None:
                take_execution()                         # drop any stale label
                result = run_tool(call.name, args, ctx)  # security lives here
                execution = take_execution()

            if call.name in contextual_tools:
                if execution.kind == "denied":
                    stop = "scope"
                elif read_retry and execution.kind in {"invalid", "failed"}:
                    stop = read_retry.handle_failure(call, execution, result)
            ok = "error" not in result
            kind = execution.kind or ("ok" if ok else "failed")
            payload = result.get("result")
            summary = describe_result(payload)
            # Audit data: who, what, outcome, row counts. No results, no real numbers.
            tool_log.append({"tool": call.name, "args": call.args, "ok": ok,
                             "error": result.get("error"), "kind": kind,
                             **summary})
            tool_name = call.name if call.name in exposed else "unavailable_tool"
            latency_ms = round((time.monotonic() - started) * 1000, 2)
            if trace:
                trace.record_tool(tool_name, trace.arguments(call.args, exposed.get(call.name, {})),
                                  kind, latency_ms, execution.failure, summary=summary)
            log.info("user=%s tool=%s ok=%s kind=%s latency_ms=%s", ctx.user_id, tool_name, ok, kind, latency_ms)
            if ok and isinstance(payload, dict):
                if isinstance(payload.get("file"), dict):
                    files.append(payload["file"])        # a report: a download button
                if isinstance(payload.get("image"), dict):
                    pictures.append(payload["image"])    # a picture: shown in the chat
                if secret and "value" in payload:        # derived from secrets: keep it secret
                    result = {"result": {"value": vault.hide(payload["value"])}}

            # Masking happens HERE, for every tool, before the model sees anything.
            messages.append(Message("tool", tool_name=call.name,
                                    tool_result=vault.mask(result)))
        if stop:
            answer = localized(stop, question)
            messages.append(Message("assistant", text=answer))
            _tidy(messages, rejected_at)
            return AgentResult(answer, messages, tool_log, vault, files, pictures)

    _tidy(messages, rejected_at)
    return AgentResult(fallback, messages, tool_log, vault, files, pictures)
