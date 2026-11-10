import pytest
from google.genai import errors

from app.core.llm import gemini
from app.core.llm.base import LLMBusy, LLMError, Message
from app.core.llm.gemini import GeminiProvider


class _Part:
    text, function_call, thought = "ok", None, False


class _Content:
    parts = [_Part()]


class _Candidate:
    content = _Content()


class _Response:
    candidates = [_Candidate()]


class FakeModels:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return _Response()


class FakeClient:
    def __init__(self, failures):
        self.models = FakeModels(failures)


def rate_limit(seconds):
    return errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": f"Quota exceeded. Please retry in {seconds}s."}}, None)


def make(failures, monkeypatch):
    provider = GeminiProvider(api_key="test", model="m")
    provider.client = FakeClient(failures)
    slept = []
    monkeypatch.setattr(gemini.time, "sleep", lambda seconds: slept.append(seconds))
    return provider, slept


def ask(provider):
    return provider.generate("system", [Message("user", "hi")])


def test_a_short_rate_limit_is_waited_out(monkeypatch):
    provider, slept = make([rate_limit(1.5)], monkeypatch)
    assert ask(provider).text == "ok"
    assert provider.client.models.calls == 2 and slept == [2.5]


def test_a_long_wait_is_not_attempted(monkeypatch):
    provider, slept = make([rate_limit(120)], monkeypatch)
    with pytest.raises(LLMBusy):
        ask(provider)
    assert provider.client.models.calls == 1 and slept == []


def test_it_gives_up_after_the_allowed_retries(monkeypatch):
    provider, slept = make([rate_limit(1)] * 5, monkeypatch)
    with pytest.raises(LLMBusy):
        ask(provider)
    assert provider.client.models.calls == gemini.MAX_RETRIES + 1


def test_other_errors_are_not_retried(monkeypatch):
    bad = errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                             "message": "bad request"}}, None)
    provider, slept = make([bad], monkeypatch)
    with pytest.raises(LLMError) as info:
        ask(provider)
    assert not isinstance(info.value, LLMBusy)
    assert provider.client.models.calls == 1 and slept == []