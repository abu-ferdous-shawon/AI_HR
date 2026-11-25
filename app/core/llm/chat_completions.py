"""Translate the existing agent protocol to GroqCloud Chat Completions."""
import base64
import json

import httpx

from app.core.llm.base import LLMBusy, LLMError, LLMProvider, LLMResponse, Message, ToolCall


def _messages(system_prompt: str, messages: list[Message]) -> list[dict]:
    encoded = [{"role": "system", "content": system_prompt}]
    pending = iter(())
    for index, message in enumerate(messages):
        if message.role == "tool":
            call_id = next(pending, None)
            if call_id is None:
                raise LLMError("Cannot match a tool result to its call")
            encoded.append({
                "role": "tool", "tool_call_id": call_id,
                "content": json.dumps(message.tool_result or {}, ensure_ascii=False, default=str),
            })
            continue

        content = message.text
        entry = {"role": message.role, "content": content}
        if message.images:
            entry["content"] = [{"type": "text", "text": content}] + [
                {"type": "image_url", "image_url": {
                    "url": f"data:{image.mime_type};base64,{base64.b64encode(image.data).decode('ascii')}",
                }}
                for image in message.images
            ]
        if message.tool_calls:
            calls = [
                {"id": f"call_{index}_{offset}", "type": "function", "function": {
                    "name": call.name, "arguments": json.dumps(call.args, ensure_ascii=False),
                }}
                for offset, call in enumerate(message.tool_calls)
            ]
            entry["tool_calls"] = calls
            pending = iter(call["id"] for call in calls)
        encoded.append(entry)
    return encoded


class GroqProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.client = httpx.Client(
            base_url="https://api.groq.com/openai/v1", timeout=20.0,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def generate(self, system_prompt: str, messages: list[Message],
                 tools: list[dict] | None = None) -> LLMResponse:
        payload = {"model": self.model, "messages": _messages(system_prompt, messages)}
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {
                    key: tool[key] for key in ("name", "description", "parameters")
                }}
                for tool in tools
            ]
        try:
            response = self.client.post("chat/completions", json=payload)
        except httpx.RequestError:
            raise LLMBusy("Could not reach GroqCloud") from None
        if response.status_code in (429, 500, 502, 503, 504):
            raise LLMBusy(f"GroqCloud is temporarily unavailable (HTTP {response.status_code})")
        if not response.is_success:
            raise LLMError(f"GroqCloud request failed (HTTP {response.status_code})")
        try:
            reply = response.json()["choices"][0]["message"]
            calls = []
            for call in reply.get("tool_calls") or []:
                function = call["function"]
                args = json.loads(function["arguments"])
                if not isinstance(args, dict):
                    raise ValueError("Expected a tool argument object")
                calls.append(ToolCall(name=function["name"], args=args))
            return LLMResponse(text=reply.get("content") or "", tool_calls=calls)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise LLMError("GroqCloud returned an invalid chat response") from None
