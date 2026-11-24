import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main
from app.core import speech
from app.core.security import UserContext

CTX = UserContext("a@x.com", "HR-EMP-00001")


def tone(seconds=1.0, amp=0.2, rate=16000):
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def wav(seconds=1.0, amp=0.2, rate=16000):
    samples = (tone(seconds, amp, rate) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(rate)
        recording.writeframes(samples.tobytes())
    return buffer.getvalue()


class FakeStt:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, language):
        self.calls.append((len(audio), language))
        return " amar  chuti koto? "


@pytest.fixture
def fake_stt(monkeypatch):
    stt = FakeStt()
    monkeypatch.setattr(speech, "get_stt", lambda: stt)
    speech._hits.clear()
    yield stt
    speech._hits.clear()


# ---------- helpers ----------

def test_read_wav_decodes_pcm():
    samples = tone()
    back = speech.read_wav(wav())
    assert back.shape == samples.shape and np.allclose(back, samples, atol=1e-3)


def test_bad_audio_is_refused(monkeypatch):
    for bad in (b"junk", wav(rate=8000)):
        with pytest.raises(speech.SpeechInputError):
            speech.read_wav(bad)
    monkeypatch.setattr(speech, "MAX_SECONDS", 0.5)
    with pytest.raises(speech.SpeechInputError, match="too long"):
        speech.read_wav(wav(1.0))


# ---------- speech to text ----------

def test_transcribe_maps_the_language_and_cleans_text(fake_stt):
    assert speech.transcribe(wav(), "bn") == "amar chuti koto?"
    assert speech.transcribe(wav(), "auto") == "amar chuti koto?"
    assert [call[1] for call in fake_stt.calls] == ["bengali", None]


@pytest.mark.parametrize("data", [wav(amp=0.0), wav(0.1)])
def test_silence_and_tiny_clips_never_reach_the_model(fake_stt, data):
    with pytest.raises(speech.SpeechInputError):
        speech.transcribe(data)
    assert fake_stt.calls == []


def test_rate_limit(monkeypatch):
    speech._hits.clear()
    monkeypatch.setattr(speech.settings, "speech_rate_per_minute", 2)
    speech.check_rate("u")
    speech.check_rate("u")
    with pytest.raises(speech.SpeechRateLimited):
        speech.check_rate("u")
    speech.check_rate("someone-else")
    speech._hits.clear()


# ---------- API ----------

@pytest.fixture
def api(fake_stt, monkeypatch):
    monkeypatch.setattr(main.settings, "speech_enabled", True)
    main.app.dependency_overrides[main.current_user] = lambda: CTX
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def post_audio(client, data, **kw):
    return client.post("/stt", content=data, headers={"Content-Type": "audio/wav"}, **kw)


def test_speech_is_off_by_default(fake_stt, monkeypatch):
    monkeypatch.setattr(main.settings, "speech_enabled", False)
    main.app.dependency_overrides[main.current_user] = lambda: CTX
    try:
        client = TestClient(main.app)
        assert client.get("/speech/config").json() == {"enabled": False}
        assert post_audio(client, wav()).status_code == 404
    finally:
        main.app.dependency_overrides.clear()


def test_login_is_required(fake_stt, monkeypatch):
    monkeypatch.setattr(main.settings, "speech_enabled", True)
    client = TestClient(main.app)
    assert post_audio(client, wav()).status_code == 401


def test_stt_returns_text_and_does_not_log_it(api, caplog):
    caplog.set_level("INFO")
    r = post_audio(api, wav(), params={"lang": "bn"})
    assert r.status_code == 200 and r.json() == {"text": "amar chuti koto?"}
    assert "amar" not in caplog.text


def test_stt_refuses_bad_oversized_and_unknown_language(api, monkeypatch):
    assert post_audio(api, b"junk").status_code == 422
    assert post_audio(api, wav(), params={"lang": "fr"}).status_code == 422
    monkeypatch.setattr(speech, "MAX_AUDIO_BYTES", 100)
    assert post_audio(api, wav()).status_code == 413


def test_stt_rate_limit(api, monkeypatch):
    monkeypatch.setattr(speech.settings, "speech_rate_per_minute", 1)
    assert post_audio(api, wav()).status_code == 200
    assert post_audio(api, wav()).status_code == 429


def test_model_failure_is_a_503_without_details(api, monkeypatch):
    class Broken:
        def transcribe(self, audio, language):
            raise speech.SpeechError("CUDA out of memory at /secret/path")

    monkeypatch.setattr(speech, "get_stt", Broken)
    response = post_audio(api, wav())
    assert response.status_code == 503 and "CUDA" not in response.text
