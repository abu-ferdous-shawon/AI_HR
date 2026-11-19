"""Speech to text and text to speech with local models.

Audio and answers stay on the VM. Nothing here is sent to the LLM.
"""
import io
import logging
import re
import threading
import time
import wave
from abc import ABC, abstractmethod
from collections import deque
from functools import lru_cache

import numpy as np

from app.config import settings
from app.core.language import detect_language

log = logging.getLogger("hr_ai.speech")

STT_RATE = 16000
MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_SECONDS = 60
MIN_SECONDS = 0.4
MIN_RMS = 0.003
MAX_TRANSCRIPT = 1000
LANG_NAMES = {"bn": "bengali", "en": "english"}


class SpeechError(Exception):
    """Speech is unavailable or failed."""

class SpeechInputError(SpeechError):
    """The audio or text given cannot be used."""

class SpeechRateLimited(SpeechError):
    """Too many voice requests from one person."""


# ---------- audio helpers (standard library only) ----------

def read_wav(data: bytes) -> np.ndarray:
    """16 kHz mono 16-bit WAV -> float32 samples in -1..1. The browser sends this format."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (STT_RATE, 1, 2):
                raise SpeechInputError("The audio must be 16 kHz mono 16-bit WAV.")
            if w.getnframes() / STT_RATE > MAX_SECONDS:
                raise SpeechInputError(f"The recording is too long (limit {MAX_SECONDS} seconds).")
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        raise SpeechInputError("The audio could not be read.") from None
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def write_wav(samples: np.ndarray, rate: int) -> bytes:
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())
    return buffer.getvalue()


# ---------- limits ----------

_slots = threading.BoundedSemaphore(max(1, settings.speech_max_concurrent))
_hits: dict[str, deque] = {}
_hits_lock = threading.Lock()


def check_rate(user_id: str) -> None:
    limit = settings.speech_rate_per_minute
    if limit <= 0:
        return
    now = time.monotonic()
    with _hits_lock:
        q = _hits.setdefault(user_id, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            raise SpeechRateLimited("Too many voice requests.")
        q.append(now)


def _device(torch) -> str:
    wanted = settings.speech_device
    if wanted != "auto":
        return wanted
    return "cuda:0" if torch.cuda.is_available() else "cpu"


# ---------- speech to text ----------

class SpeechToText(ABC):
    @abstractmethod
    def transcribe(self, audio: np.ndarray, language: str | None) -> str: ...


class WhisperSTT(SpeechToText):
    """Whisper through transformers. It covers Bangla and English, also mixed speech."""

    def __init__(self, model: str | None = None):
        self.model = model or settings.stt_model
        self._pipe = None
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self._pipe is None:
                try:
                    import torch
                    from transformers import pipeline
                    device = _device(torch)
                    dtype = torch.float16 if device.startswith("cuda") else torch.float32
                    self._pipe = pipeline("automatic-speech-recognition", model=self.model,
                                          torch_dtype=dtype, device=device, chunk_length_s=30)
                except ImportError as exc:
                    raise SpeechError("Speech packages are missing: install "
                                      "requirements-speech.txt.") from exc
                except Exception as exc:
                    raise SpeechError(f"Could not load {self.model}: {exc}") from exc
            return self._pipe

    def transcribe(self, audio, language):
        pipe = self.load()
        kwargs = {"task": "transcribe"}
        if language:
            kwargs["language"] = language
        try:
            out = pipe({"raw": audio, "sampling_rate": STT_RATE}, generate_kwargs=kwargs)
        except Exception as exc:
            raise SpeechError(f"Transcription failed: {exc}") from exc
        return str(out.get("text", "")).strip()


@lru_cache
def get_stt() -> SpeechToText:
    return WhisperSTT()


def transcribe(data: bytes, lang: str = "auto") -> str:
    audio = read_wav(data)
    if (audio.size < MIN_SECONDS * STT_RATE
            or float(np.sqrt(np.mean(audio ** 2))) < MIN_RMS):
        raise SpeechInputError("I could not hear anything. Please try again.")
    with _slots:
        text = get_stt().transcribe(audio, LANG_NAMES.get(lang))
    return " ".join(text.split())[:MAX_TRANSCRIPT]


# ---------- text to speech ----------

class TextToSpeech(ABC):
    @abstractmethod
    def synthesize(self, text: str, language: str) -> tuple[np.ndarray, int]:
        """language is 'bn' or 'en'. Returns (float32 samples, sample rate)."""


class IndicParlerTTS(TextToSpeech):
    """ai4bharat/indic-parler-tts: Bangla and English, voice set by a text description."""

    def __init__(self, model: str | None = None):
        self.name = model or settings.tts_model
        self._model = self._tok = self._desc_tok = None
        self._device = "cpu"
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self._model is None:
                try:
                    import torch
                    from parler_tts import ParlerTTSForConditionalGeneration
                    from transformers import AutoTokenizer
                    self._device = _device(torch)
                    model = ParlerTTSForConditionalGeneration.from_pretrained(self.name)
                    model = model.to(self._device)
                    self._tok = AutoTokenizer.from_pretrained(self.name)
                    self._desc_tok = AutoTokenizer.from_pretrained(
                        model.config.text_encoder._name_or_path)
                    self._model = model
                except ImportError as exc:
                    raise SpeechError(f"Speech packages are missing ({exc}): install requirements-speech.txt.") from exc
                except Exception as exc:
                    raise SpeechError(f"Could not load {self.name}: {exc}") from exc

    def synthesize(self, text, language):
        self.load()
        import torch
        voice = settings.tts_voice_bn if language == "bn" else settings.tts_voice_en
        try:
            d = self._desc_tok(voice, return_tensors="pt").to(self._device)
            p = self._tok(text, return_tensors="pt").to(self._device)
            torch.manual_seed(settings.tts_seed)     # same voice for every sentence
            with torch.no_grad():
                out = self._model.generate(
                    input_ids=d.input_ids, attention_mask=d.attention_mask,
                    prompt_input_ids=p.input_ids, prompt_attention_mask=p.attention_mask)
        except Exception as exc:
            raise SpeechError(f"Speech synthesis failed: {exc}") from exc
        return (out.cpu().to(torch.float32).numpy().squeeze(),
                int(self._model.config.sampling_rate))


class MmsTTS(TextToSpeech):
    """facebook/mms-tts-ben and -eng: small and CPU friendly. CC-BY-NC licence."""
    MODELS = {"bn": "facebook/mms-tts-ben", "en": "facebook/mms-tts-eng"}

    def __init__(self):
        self._loaded: dict = {}
        self._lock = threading.Lock()

    def _get(self, language):
        with self._lock:
            if language not in self._loaded:
                try:
                    import torch
                    from transformers import AutoTokenizer, VitsModel
                    name = self.MODELS[language]
                    self._loaded[language] = (AutoTokenizer.from_pretrained(name),
                                              VitsModel.from_pretrained(name).to(_device(torch)))
                except ImportError as exc:
                    raise SpeechError("Speech packages are missing: install "
                                      "requirements-speech.txt.") from exc
                except Exception as exc:
                    raise SpeechError(f"Could not load {self.MODELS[language]}: {exc}") from exc
            return self._loaded[language]

    def synthesize(self, text, language):
        import torch
        tok, model = self._get(language)
        try:
            inputs = tok(text, return_tensors="pt").to(model.device)
            with torch.no_grad():
                wave_ = model(**inputs).waveform[0]
        except Exception as exc:
            raise SpeechError(f"Speech synthesis failed: {exc}") from exc
        return wave_.cpu().to(torch.float32).numpy(), int(model.config.sampling_rate)


class SplitParlerTTS(TextToSpeech):
    """Bangla with the Bangladeshi model, English with the base model (each loads on first use)."""

    def __init__(self):
        self.bn = IndicParlerTTS(settings.tts_model_bn)
        self.en = IndicParlerTTS(settings.tts_model_en)

    def synthesize(self, text, language):
        return (self.bn if language == "bn" else self.en).synthesize(text, language)

    def load(self):
        self.bn.load()
        self.en.load()


@lru_cache
def get_tts() -> TextToSpeech:
    name = settings.tts_provider.lower()
    if name == "indic_parler":
        return IndicParlerTTS()
    if name == "parler_split":
        return SplitParlerTTS()
    if name == "mms":
        return MmsTTS()
    raise SpeechError(f"Unknown TTS_PROVIDER '{name}'. Supported: indic_parler, mms")


# ---------- preparing text for the voice ----------

def plain_text(text: str) -> str:
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_`#>|~]+", " ", text)
    return " ".join(text.split())


def split_sentences(text: str, max_total: int, max_piece: int = 220) -> list[str]:
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[.!?।])\s+", text):
        sentence = sentence.strip()
        while len(sentence) > max_piece:
            cut = sentence.rfind(" ", 0, max_piece)
            cut = cut if cut > 0 else max_piece
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    out, total = [], 0
    for piece in pieces:
        if out and total + len(piece) > max_total:
            break
        out.append(piece)
        total += len(piece)
    return out


def _has_bangla(text: str) -> bool:
    return any("\u0980" <= ch <= "\u09ff" for ch in text)


_warned = False


@lru_cache
def _xlit_engine():
    from ai4bharat.transliteration import XlitEngine
    return XlitEngine("bn", beam_width=4, src_script_type="en")


def to_bangla_script(text: str) -> str:
    """Banglish (Bangla in English letters) -> Bangla script. Unchanged if unavailable."""
    global _warned
    if not settings.tts_transliterate_banglish:
        return text
    try:
        out = _xlit_engine().translit_sentence(text)
        return out["bn"] if isinstance(out, dict) else str(out)
    except Exception as exc:
        if not _warned:
            _warned = True
            log.warning("Banglish transliteration unavailable (%s); using the English voice.",
                        type(exc).__name__)
        return text


def synthesize(text: str) -> bytes:
    """WAV bytes for the text. Only counts are logged by callers, never the text."""
    plain = plain_text(text)
    if not plain:
        raise SpeechInputError("There is nothing to read aloud.")
    sentences = split_sentences(plain, settings.tts_max_chars)
    if not _has_bangla(plain) and detect_language(plain) == "banglish":
        sentences = [to_bangla_script(s) for s in sentences]
    language = "bn" if _has_bangla(" ".join(sentences)) else "en"

    parts, rate = [], None
    with _slots:
        tts = get_tts()
        for sentence in sentences:
            samples, rate = tts.synthesize(sentence, language)
            parts += [np.asarray(samples, dtype=np.float32),
                      np.zeros(int(rate * 0.15), dtype=np.float32)]
    return write_wav(np.concatenate(parts), rate)


def warm_up() -> None:
    """Download and load both models now, so the first user does not wait."""
    get_stt().load()
    tts = get_tts()
    if hasattr(tts, "load"):
        tts.load()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    warm_up()
    print("Speech models are ready.")