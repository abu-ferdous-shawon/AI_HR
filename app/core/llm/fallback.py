import logging
from threading import Lock
from time import monotonic

from app.core.llm.base import LLMBusy, LLMProvider, LLMResponse, Message

log = logging.getLogger(__name__)


class GeminiCooldown:
    """Share quota state among concurrent requests using the same Gemini credentials/model."""

    def __init__(self):
        self.lock = Lock()
        self.until = 0.0
        self.probing = False

    def select_primary(self) -> tuple[bool, bool]:
        with self.lock:
            if self.probing or monotonic() < self.until:
                return False, False
            self.probing = bool(self.until)
            return True, self.probing

    def rate_limited(self, delay: float) -> None:
        with self.lock:
            self.until = max(self.until, monotonic() + delay)

    def finish_probe(self, recovered: bool) -> None:
        with self.lock:
            if recovered and self.until <= monotonic():
                self.until = 0.0
            self.probing = False


class FallbackTurn(LLMProvider):
    """Stay on the fallback provider for the rest of a turn once it takes over."""

    def __init__(self, primary: LLMProvider, fallback: LLMProvider, cooldown: GeminiCooldown):
        self.primary = primary
        self.fallback = fallback
        self.cooldown = cooldown
        self.using_fallback = False

    def generate(self, system_prompt: str, messages: list[Message],
                 tools: list[dict] | None = None) -> LLMResponse:
        if not self.using_fallback:
            use_primary, probe = self.cooldown.select_primary()
            if use_primary:
                recovered = False
                try:
                    response = self.primary.generate(system_prompt, messages, tools)
                    recovered = True
                    return response
                except LLMBusy as exc:
                    self.cooldown.rate_limited(exc.retry_after)
                    log.info("Routing chat to GroqCloud after Gemini became unavailable")
                finally:
                    if probe:
                        self.cooldown.finish_probe(recovered)
            self.using_fallback = True
        return self.fallback.generate(system_prompt, messages, tools)
