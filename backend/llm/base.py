from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_calls: Optional[list] = None  # For assistant messages with tool calls
    tool_call_id: Optional[str] = None  # For tool result messages


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class CompletionResponse:
    content: str
    model: str
    provider: str
    instance_name: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    tool_calls: Optional[list] = None


class LLMError(Exception):
    def __init__(self, message: str, provider: str = "", status_code: Optional[int] = None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class BaseLLMProvider(ABC):
    """Async-first LLM provider interface."""

    provider_name: str  # class-level constant, e.g. "ollama"
    instance_name: str  # set by registry when adding
    supports_tools: bool = False  # Whether this provider supports native function calling

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        tools: Optional[list] = None,
    ) -> CompletionResponse:
        """Non-streaming completion. Raises LLMError on failure."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        """Yields text delta chunks. Raises LLMError on failure."""
        ...

    async def health_check(self) -> bool:
        """Return True if provider is reachable."""
        try:
            await self.complete(
                [ChatMessage(role="user", content="hi")],
                max_tokens=1,
            )
            return True
        except Exception:
            return False
