import logging
import re
import time

from google import genai
from google.genai import types

from app.config import settings
from app.core.llm.base import (
    LLMBusy, LLMError, LLMProvider, LLMResponse, Message, ToolCall,
)

log = logging.getLogger("hr_ai.gemini")

MAX_RETRIES = 2    # extra attempts after a rate-limit error
MAX_WAIT = 30.0    # never wait longer than this in total (seconds)


def _retry_delay(exc: Exception) -> float | None:
    """Seconds Gemini asks us to wait after a rate-limit (429) error, else None."""
    text = str(exc)
    if getattr(exc, "code", None) != 429 and not text.startswith("429"):
        return None
    match = (re.search(r"retry in ([0-9.]+)s", text)
             or re.search(r"'retryDelay': '([0-9.]+)s'", text))
    return float(match.group(1)) if match else 10.0


class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        key = api_key or settings.gemini_api_key.get_secret_value()
        if not key:
            raise LLMError("GEMINI_API_KEY is empty. Add it to your .env file.")
        self.model = model or settings.gemini_model
        self.client = genai.Client(api_key=key)

    # ---- our format -> Gemini format ----

    @staticmethod
    def _to_contents(messages: list[Message]) -> list[types.Content]:
        contents: list[types.Content] = []
        pending: list[types.Part] = []  # tool results waiting to be grouped

        def flush():
            nonlocal pending
            if pending:
                # Several tool results from one round go in ONE user turn.
                contents.append(types.Content(role="user", parts=pending))
                pending = []

        for m in messages:
            if m.role == "tool":
                pending.append(types.Part.from_function_response(
                    name=m.tool_name, response=m.tool_result or {}))
                continue
            flush()
            if m.role == "user":
                parts = [types.Part.from_bytes(data=i.data, mime_type=i.mime_type)
                         for i in m.images]
                parts.append(types.Part(text=m.text))
                contents.append(types.Content(role="user", parts=parts))
            elif m.raw is not None:
                contents.append(m.raw)  # replay Gemini's own reply unchanged
            else:
                contents.append(types.Content(
                    role="model", parts=[types.Part(text=m.text)]))
        flush()
        return contents

    @staticmethod
    def _to_tools(tools: list[dict]) -> list[types.Tool]:
        declarations = []
        for t in tools:
            spec = {"name": t["name"], "description": t["description"]}
            # Gemini dislikes an empty parameter list, so leave it out then.
            if t["parameters"].get("properties"):
                spec["parameters"] = t["parameters"]
            declarations.append(types.FunctionDeclaration(**spec))
        return [types.Tool(function_declarations=declarations)]

    # ---- the call ----

    def generate(self, system_prompt: str, messages: list[Message],
                 tools: list[dict] | None = None) -> LLMResponse:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt, temperature=0.2,
            tools=self._to_tools(tools) if tools else None,
        )
        contents = self._to_contents(messages)

        waited, attempt = 0.0, 0
        while True:
            try:
                response = self.client.models.generate_content(
                    model=self.model, contents=contents, config=config)
                break
            except Exception as exc:
                delay = _retry_delay(exc)
                if delay is None:                      # not a rate limit: do not retry
                    raise LLMError(f"Gemini request failed ({self.model}): {exc}") from exc
                pause = delay + 1
                if attempt >= MAX_RETRIES or waited + pause > MAX_WAIT:
                    raise LLMBusy(f"Gemini is rate-limited ({self.model}): {exc}") from exc
                log.warning("Gemini rate limit: waiting %.0f s (retry %d of %d)",
                            pause, attempt + 1, MAX_RETRIES)
                time.sleep(pause)
                waited += pause
                attempt += 1

        # ---- Gemini format -> our format ----
        candidate = response.candidates[0] if response.candidates else None
        content = candidate.content if candidate else None
        parts = (content.parts if content else None) or []

        text = "".join(
            p.text for p in parts
            if p.text and not getattr(p, "thought", False)
        )
        calls = [
            ToolCall(name=p.function_call.name, args=dict(p.function_call.args or {}))
            for p in parts if p.function_call
        ]
        return LLMResponse(text=text, tool_calls=calls, raw=content)