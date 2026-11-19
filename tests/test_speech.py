import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main
from app.core import speech
from app.core.security import UserContext

CTX = UserContext("a@x.com", "HR-EMP-00001")
BN = "আপনার ১৫ দিন ছুটি বাকি আছে।"


def tone(seconds=1.0, amp=0.2, rate=16000):
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def wav(seconds=1.0, amp=0.2, rate=16000):
    return speech.write_wav(tone(seconds, amp, rate), rate)


class FakeStt:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, language):
        self.calls.append((len(audio), language))
        return " amar  chuti koto? "


class FakeTts:
    def __init__(self):
        self.calls = []

    def synthesize(self, text, language):
        self.calls.append((text, language))
        return np.full(2400, 0.1, dtype=np.float32), 24000


@pytest.fixture
def fakes(monkeypatch):
    stt, tts = FakeStt(), FakeTts()
    monkeypatch.setattr(speech, "get_stt", lambda: stt)
    monkeypatch.setattr(speech, "get_tts", lambda: tts)
    speech._hits.clear()
    yield stt, tts
    speech._hits.clear()


# ---------- helpers ----------

def test_wav_round_trip():
    samples = tone()
    back = speech.read_wav(speech.write_wav(samples, 16000))
    assert back.shape == samples.shape and np.allclose(back, samples, atol=1e-3)


def test_bad_audio_is_refused(monkeypatch):
    for bad in (b"junk", wav(rate=8000)):
        with pytest.raises(speech.SpeechInputError):
            speech.read_wav(bad)
    monkeypatch.setattr(speech, "MAX_SECONDS", 0.5)
    with pytest.raises(speech.SpeechInputError, match="too long"):
        speech.read_wav(wav(1.0))


def test_plain_text_drops_markdown_and_links():
    assert speech.plain_text("**Hi** see https://x.io/a `code` # now") == "Hi see code now"


def test_sentences_are_split_and_capped():
    text = "One. Two! তিন। Four?"
    assert speech.split_sentences(text, 100) == ["One.", "Two!", "তিন।", "Four?"]
    assert speech.split_sentences(text, 8) == ["One.", "Two!"]
    long = "word " * 100
    assert all(len(p) <= 220 for p in speech.split_sentences(long, 1000))


# ---------- speech to text ----------

def test_transcribe_maps_the_language_and_cleans_text(fakes):
    stt, _ = fakes
    assert speech.transcribe(wav(), "bn") == "amar chuti koto?"
    assert speech.transcribe(wav(), "auto") == "amar chuti koto?"
    assert [c[1] for c in stt.calls] == ["bengali", None]


@pytest.mark.parametrize("data", [wav(amp=0.0), wav(0.1)])
def test_silence_and_tiny_clips_never_reach_the_model(fakes, data):
    with pytest.raises(speech.SpeechInputError):
        speech.transcribe(data)
    assert fakes[0].calls == []


# ---------- text to speech ----------

def decode(audio):
    with wave.open(io.BytesIO(audio)) as w:
        return w.getframerate(), w.getnframes()


def test_bangla_text_uses_the_bangla_voice(fakes):
    _, tts = fakes
    assert decode(speech.synthesize(BN))[0] == 24000
    assert tts.calls[0][1] == "bn"


def test_english_text_uses_the_english_voice(fakes):
    _, tts = fakes
    speech.synthesize("You have **15** days left.")
    assert tts.calls == [("You have 15 days left.", "en")]


def test_banglish_is_transliterated_when_possible(fakes, monkeypatch):
    _, tts = fakes
    monkeypatch.setattr(speech, "to_bangla_script", lambda s: BN)
    speech.synthesize("tomar chuti koto din baki ache")
    assert tts.calls[0] == (BN, "bn")


def test_banglish_falls_back_to_english_voice(fakes, monkeypatch):
    _, tts = fakes
    monkeypatch.setattr(speech, "_xlit_engine", lambda: (_ for _ in ()).throw(ImportError()))
    speech.synthesize("tomar chuti koto din baki ache")
    assert tts.calls[0][1] == "en"


def test_long_answers_are_cut_and_empty_text_refused(fakes, monkeypatch):
    _, tts = fakes
    monkeypatch.setattr(speech.settings, "tts_max_chars", 20)
    speech.synthesize("Aaaa bbbb. Cccc dddd. Eeee ffff. Gggg hhhh.")
    assert len(tts.calls) == 2
    with pytest.raises(speech.SpeechInputError):
        speech.synthesize("*** ")


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
def api(fakes, monkeypatch):
    monkeypatch.setattr(main.settings, "speech_enabled", True)
    main.app.dependency_overrides[main.current_user] = lambda: CTX
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def post_audio(client, data, **kw):
    return client.post("/stt", content=data, headers={"Content-Type": "audio/wav"}, **kw)


def test_speech_is_off_by_default(fakes, monkeypatch):
    monkeypatch.setattr(main.settings, "speech_enabled", False)
    main.app.dependency_overrides[main.current_user] = lambda: CTX
    try:
        client = TestClient(main.app)
        assert client.get("/speech/config").json() == {"enabled": False}
        assert post_audio(client, wav()).status_code == 404
        assert client.post("/tts", json={"text": "hi"}).status_code == 404
    finally:
        main.app.dependency_overrides.clear()


def test_login_is_required(fakes, monkeypatch):
    monkeypatch.setattr(main.settings, "speech_enabled", True)
    client = TestClient(main.app)
    assert post_audio(client, wav()).status_code == 401
    assert client.post("/tts", json={"text": "hi"}).status_code == 401


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


def test_tts_returns_private_wav(api, caplog):
    caplog.set_level("INFO")
    r = api.post("/tts", json={"text": "Your phone is 01711000000."})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.headers["cache-control"] == "private, no-store"
    assert decode(r.content)[0] == 24000
    assert "01711000000" not in caplog.text


def test_tts_input_limits_and_rate_limit(api, monkeypatch):
    assert api.post("/tts", json={"text": "x" * 2001}).status_code == 422
    assert api.post("/tts", json={"text": "***"}).status_code == 422
    monkeypatch.setattr(speech.settings, "speech_rate_per_minute", 1)
    speech._hits.clear()
    assert api.post("/tts", json={"text": "Hello."}).status_code == 200
    assert api.post("/tts", json={"text": "Hello."}).status_code == 429


def test_model_failure_is_a_503_without_details(api, fakes):
    class Broken:
        def synthesize(self, text, language):
            raise speech.SpeechError("CUDA out of memory at /secret/path")
    speech.get_tts = lambda: Broken()
    r = api.post("/tts", json={"text": "Hello."})
    assert r.status_code == 503 and "CUDA" not in r.text

def test_split_parler_routes_by_language(monkeypatch):
    calls = []

    class Fake:
        def __init__(self, name):
            self.name = name

        def synthesize(self, text, language):
            calls.append(self.name)
            return np.zeros(10, dtype=np.float32), 24000

    monkeypatch.setattr(speech, "IndicParlerTTS", Fake)
    tts = speech.SplitParlerTTS()
    tts.synthesize("x", "bn")
    tts.synthesize("y", "en")
    assert calls == [speech.settings.tts_model_bn, speech.settings.tts_model_en]