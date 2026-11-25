import json
from unittest.mock import Mock

import httpx
from google.genai import errors

from app.core import agent
from app.core.llm import fallback
from app.core.llm.base import LLMBusy, LLMResponse, Message, ToolCall
from app.core.llm.chat_completions import GroqProvider
from app.core.llm.gemini import GeminiProvider
from app.core.security import UserContext


def test_rate_limit_mid_turn_replays_tool_results_and_routes_other_users(monkeypatch):
    now = 100.0
    monkeypatch.setattr(fallback, "monotonic", lambda: now)
    primary = GeminiProvider(api_key="test", model="test", fail_fast=True)
    gemini = Mock()
    primary.client = gemini
    gemini.models.generate_content.side_effect = errors.ClientError(
        429, {"error": {"code": 429, "message": "Please retry in 8s."}}, None
    )
    sleep = Mock()
    monkeypatch.setattr("app.core.llm.gemini.time.sleep", sleep)
    alternate = GroqProvider("test", "test")
    requests = []

    def complete(request):
        assert request.url.host == "api.groq.com"
        assert request.url.path == "/openai/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    cooldown = fallback.GeminiCooldown()
    history = [
        Message("user", "Show my leave"),
        Message("assistant", tool_calls=[ToolCall("leave", {})]),
        Message("tool", tool_name="leave", tool_result={"balance": 5}),
    ]
    with httpx.Client(base_url=alternate.client.base_url, headers=alternate.client.headers,
                      transport=httpx.MockTransport(complete)) as client:
        alternate.client.close()
        alternate.client = client
        turn = fallback.FallbackTurn(primary, alternate, cooldown)
        assert turn.generate("system", history).text == "ok"
        _, _, assistant, result = requests[0]["messages"]
        assert assistant["tool_calls"][0]["id"] == result["tool_call_id"]
        assert json.loads(result["content"]) == {"balance": 5}
        assert fallback.FallbackTurn(primary, alternate, cooldown).generate("system", history).text == "ok"
        now += 9
        assert turn.generate("system", history).text == "ok"
    gemini.models.generate_content.assert_called_once()
    sleep.assert_not_called()


def test_only_one_request_probes_after_cooldown(monkeypatch):
    now = 100.0
    monkeypatch.setattr(fallback, "monotonic", lambda: now)
    primary, secondary = Mock(), Mock()
    cooldown = fallback.GeminiCooldown()
    primary.generate.side_effect = LLMBusy(retry_after=3)
    fallback.FallbackTurn(primary, secondary, cooldown).generate("system", [])
    now += 4

    def probe(*args):
        alternate = fallback.FallbackTurn(primary, secondary, cooldown).generate("system", [])
        assert alternate is secondary.generate.return_value
        return LLMResponse("recovered")

    primary.generate.side_effect = probe
    assert fallback.FallbackTurn(primary, secondary, cooldown).generate("system", []).text == "recovered"
    assert primary.generate.call_count == secondary.generate.call_count == 2



def test_agent_does_not_execute_a_completed_tool_again_on_fallback(monkeypatch):
    primary, secondary = Mock(), Mock()
    primary.generate.side_effect = [
        LLMResponse(tool_calls=[ToolCall("get_my_profile", {})]),
        LLMBusy(),
    ]
    tool = Mock(return_value={"result": {"department": "Operations"}})
    monkeypatch.setattr(agent, "run_tool", tool)

    def complete(system, messages, tools):
        assert messages[-1].role == "tool"
        assert messages[-1].tool_result == tool.return_value
        return LLMResponse(text="Your department is Operations.")

    secondary.generate.side_effect = complete
    turn = fallback.FallbackTurn(primary, secondary, fallback.GeminiCooldown())
    context = UserContext("user@example.com", "EMP-001")
    result = agent.run_agent("What is my department?", context, llm=turn)
    assert result.answer == "Your department is Operations."
    tool.assert_called_once_with("get_my_profile", {}, context)
