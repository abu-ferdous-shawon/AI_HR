from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


class LLMError(Exception):
    """Raised when the AI service fails or is set up wrongly."""

class LLMBusy(LLMError):
    """The AI service is rate-limited right now; trying again shortly usually works."""


@dataclass
class ToolCall:
    """The model asking us to run one tool."""
    name: str
    args: dict[str, Any]


@dataclass
class ImageData:
    """A picture sent by the person (kept only for the question it came with)."""
    mime_type: str
    data: bytes


@dataclass
class Message:
    """One turn of a conversation: from the user, the assistant or a tool."""
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant turn
    tool_name: str | None = None                              # tool turn
    tool_result: dict[str, Any] | None = None                 # tool turn
    raw: Any = None  # provider's original reply, replayed as-is (see Gemini)
    images: list[ImageData] = field(default_factory=list)     # user turn


@dataclass
class LLMResponse:
    """What the model sent back: words, tool requests, or both."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None


class LLMProvider(ABC):
    @abstractmethod
    def generate(self, system_prompt: str, messages: list[Message],
                 tools: list[dict] | None = None) -> LLMResponse:
        """Send instructions, conversation and tool list; get a response."""