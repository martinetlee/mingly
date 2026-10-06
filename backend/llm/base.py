import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Optional

import httpx


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_calls: Optional[list] = None  # OpenAI-shaped: [{"id", "type": "function", "function": {"name", "arguments"}}]
    tool_call_id: Optional[str] = None  # For tool result messages
    # Provider-native assistant content (e.g. Anthropic content blocks incl.
    # thinking) that must be echoed back unchanged in a tool loop.
    provider_content: Optional[Any] = None


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict

    def to_openai(self) -> dict:
        import json
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments)},
        }


@dataclass
class CompletionResponse:
    content: str
    model: str
    provider: str
    instance_name: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    tool_calls: Optional[list] = None
    reasoning: Optional[str] = None
    provider_content: Optional[Any] = None


@dataclass
class StreamDelta:
    """One streamed chunk: answer text and/or reasoning ("thinking") text."""
    content: str = ""
    reasoning: str = ""


class LLMError(Exception):
    def __init__(self, message: str, provider: str = "", status_code: Optional[int] = None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class LLMUnavailable(LLMError):
    """No usable provider: none configured, server unreachable, or missing/invalid credentials."""


class LLMModelMissing(LLMError):
    """The provider is reachable but the configured model is not installed / does not exist."""


class LLMTimeout(LLMError):
    """The provider did not answer in time."""


class BaseLLMProvider(ABC):
    """Async-first LLM provider interface.

    Each provider keeps one long-lived ``httpx.AsyncClient`` (re-created if the
    running event loop changed, e.g. across test loops).
    """

    provider_name: str  # class-level constant, e.g. "ollama"
    instance_name: str  # set by registry when adding
    model: str = ""
    supports_tools: bool = False  # Whether this provider supports native function calling
    # Context window in tokens used to size prompts (None = provider default / large).
    context_tokens: Optional[int] = None
    request_timeout: float = 180.0

    _http_client: Optional[httpx.AsyncClient] = None
    _http_loop: Optional[asyncio.AbstractEventLoop] = None

    def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        client = self._http_client
        if client is None or client.is_closed or self._http_loop is not loop:
            client = httpx.AsyncClient(timeout=httpx.Timeout(self.request_timeout, connect=10.0))
            self._http_client = client
            self._http_loop = loop
        return client

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
        self._http_client = None

    def _raise_transport(self, exc: Exception) -> None:
        """Map an httpx transport exception to the typed LLM errors."""
        if isinstance(exc, httpx.TimeoutException):
            raise LLMTimeout(f"{self.provider_name} timed out: {exc}", provider=self.provider_name) from exc
        if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError, httpx.NetworkError)):
            raise LLMUnavailable(f"{self.provider_name} unreachable: {exc}", provider=self.provider_name) from exc
        raise LLMError(f"{self.provider_name} request failed: {exc}", provider=self.provider_name) from exc

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        tools: Optional[list] = None,
    ) -> CompletionResponse:
        """Non-streaming completion. ``tools`` use the OpenAI function schema.
        Raises LLMError (or a typed subclass) on failure."""
        ...

    @abstractmethod
    def stream(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> AsyncIterator[StreamDelta]:
        """Yields StreamDelta chunks (answer text and reasoning separately).
        Raises LLMError (or a typed subclass) on failure."""
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
