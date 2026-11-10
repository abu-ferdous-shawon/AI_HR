import datetime
import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.core.language import language_hint
from app.core.llm import get_llm
from app.core.llm.base import ImageData, LLMProvider, Message
from app.core.masking import Vault
from app.core.security import UserContext
from app.tools.registry import get_tool_specs, load_all_tools, run_tool

log = logging.getLogger("hr_ai.agent")

MAX_STEPS = 8  # most model <-> tool rounds for one question (room for look up + calculate + policy)
PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.txt"
FALLBACK = ("Sorry, I could not complete that. Please try again or contact HR.\n"
            "দুঃখিত, এটি সম্পন্ন করা যায়নি। আবার চেষ্টা করুন অথবা এইচআর-এর সাথে যোগাযোগ করুন।")

# Added to the instructions only once placeholders exist in the conversation.
# A model that is told about placeholders when there are none starts inventing them.
PLACEHOLDER_RULES = """\
Placeholders are in use in this conversation. A confidential value may reach you as a placeholder like [[MASK_1]]. Use a placeholder ONLY when a tool result or the person's message actually contains that exact placeholder; copy it exactly, character for character, where the value belongs, for example "Your net pay is [[MASK_1]]." Everything else is plain: leave days, dates, counts, names and any number that appears as an ordinary number in a tool result must be written out as it is. Never invent a placeholder, never guess what one stands for, and never change, split or reformat one. To add, subtract, compare or take a percentage of placeholder values, call calculate with the placeholders inside the expression, for example calculate("[[MASK_1]] - [[MASK_2]]"); the result comes back as a new placeholder that you copy into your reply. For a comparison, report the difference. A placeholder can also stand for something the person typed, such as a phone number or an ID number; treat it as given and do not ask them to repeat it."""


def retry_note(question: str) -> str:
    """Sent once if the model writes a placeholder that does not exist."""
    return (
        "System note: your last answer used a placeholder like [[MASK_1]] that does "
        "not exist. Write every number, date and name exactly as the tool results "
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


def build_system_prompt(vault: Vault | None = None, hint: str = "") -> str:
    today = datetime.date.today()
    text = PROMPT_FILE.read_text(encoding="utf-8")
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
              images: list[ImageData] | None = None) -> AgentResult:
    llm = llm or get_llm()
    vault = vault if vault is not None else Vault()
    if not get_tool_specs():
        load_all_tools()

    hint = language_hint(question)   # decided from the letters, not left to the model
    tools = get_tool_specs()
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
        system = build_system_prompt(vault, hint)   # rebuilt each round: placeholders may exist now
        reply = llm.generate(system, messages, tools)

        if not reply.tool_calls:  # the model answered in words
            masked_answer = reply.text.strip() or FALLBACK
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
                masked_answer, reply.raw = FALLBACK, None

            # The conversation keeps the MASKED answer. Only the person gets real values.
            messages.append(Message("assistant", text=masked_answer, raw=reply.raw))
            _tidy(messages, rejected_at)
            return AgentResult(vault.unmask(masked_answer), messages, tool_log,
                               vault, files, pictures)

        messages.append(Message("assistant", text=reply.text,
                                tool_calls=reply.tool_calls, raw=reply.raw))
        for call in reply.tool_calls:
            args, secret, result = call.args, False, None

            # A calculation may contain placeholders. The real numbers are filled in
            # HERE, on the server, and the answer is hidden behind a new placeholder.
            if call.name == "calculate" and isinstance((call.args or {}).get("expression"), str):
                try:
                    expression, secret = vault.reveal_numbers(call.args["expression"])
                    args = {**call.args, "expression": expression}
                except ValueError as exc:
                    result = {"error": str(exc)}
            if result is None:
                result = run_tool(call.name, args, ctx)  # security lives here

            ok = "error" not in result
            # Audit line: who, what, outcome. Results and real numbers are not logged.
            tool_log.append({"tool": call.name, "args": call.args, "ok": ok,
                             "error": result.get("error")})
            log.info("user=%s tool=%s args=%s ok=%s",
                     ctx.user_id, call.name, call.args, ok)

            payload = result.get("result")
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

    _tidy(messages, rejected_at)
    return AgentResult(FALLBACK, messages, tool_log, vault, files, pictures)