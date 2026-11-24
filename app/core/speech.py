"""Transcribe voice input with a local speech recognition model."""
import io
import logging
import threading
import time
import wave
from abc import ABC, abstractmethod
from collections import deque
from functools import lru_cache

import numpy as np

from app.config import settings

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
    """The submitted audio cannot be used."""

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

def warm_up() -> None:
    """Load the speech recognition model before the first voice request."""
    get_stt().load()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    warm_up()
    print("Speech recognition is ready.")
