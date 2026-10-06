"""Anthropic (Claude) provider via the official ``anthropic`` SDK.

The SDK is imported lazily so a missing package surfaces as a clear
``LLMUnavailable`` instead of breaking app startup. The API key comes from the
vault (``anthropic_api_key``, set via PUT /api/llm/keys/anthropic).

Callers pass OpenAI-shaped messages/tools (the app-wide convention); they are
translated to Messages API content blocks here. ``temperature`` and
``thinking`` are not sent: current Claude models reject sampling parameters
and choose their own thinking depth.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Optional

from backend.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    CompletionResponse,
    LLMError,
    LLMModelMissing,
    LLMTimeout,
    LLMUnavailable,
    StreamDelta,
    ToolCall,
)

API_KEY_SECRET = "anthropic_api_key"


def _block_to_param(block: Any) -> Optional[dict]:
    """Convert a response content block into a request content block (echoed back unchanged)."""
    t = getattr(block, "type", None)
    if t == "text":
        return {"type": "text", "text": block.text}
    if t == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if t == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if t == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    dump = getattr(block, "model_dump", None)
    return dump(exclude_none=True) if dump else None


def convert_tools(tools: list) -> list:
    """OpenAI function-calling schema → Anthropic tool definitions."""
    out = []
    for t in tools:
        fn = t.get("function", t)
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def convert_messages(messages: list[ChatMessage]) -> tuple[str, list[dict]]:
    """Split out the system prompt and convert the rest to Messages API turns.
    Consecutive tool results are merged into one user turn."""
    system_parts: list[str] = []
    out: list[dict] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
            continue
        if m.role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id or "", "content": m.content}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and all(b.get("type") == "tool_result" for b in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if m.role == "assistant":
            if m.provider_content:
                out.append({"role": "assistant", "content": m.provider_content})
                continue
            if m.tool_calls:
                blocks: list[dict] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    fn = tc.get("function", {})
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args) if args.strip() else {}
                        except json.JSONDecodeError:
                            args = {"raw": args}
                    blocks.append({"type": "tool_use", "id": tc.get("id", ""), "name": fn.get("name", ""), "input": args})
                out.append({"role": "assistant", "content": blocks})
                continue
        out.append({"role": m.role, "content": m.content or " "})
    return "\n\n".join(p for p in system_parts if p), out


class AnthropicProvider(BaseLLMProvider):
    provider_name = "anthropic"
    supports_tools = True
    context_tokens = 200_000

    def __init__(self, model: str = "claude-opus-5-5"):
        self.model = model
        self.instance_name = ""  # set by registry
        self._client: Any = None
        self._client_key: Optional[str] = None
        self._client_loop: Optional[asyncio.AbstractEventLoop] = None

    def _sdk(self):
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:
            raise LLMUnavailable(
                "The 'anthropic' package is not installed", provider=self.provider_name
            ) from exc
        return anthropic

    def _get_client(self):
        sdk = self._sdk()
        from backend.secrets import get_secret
        key = get_secret(API_KEY_SECRET)
        if not key:
            raise LLMUnavailable("Anthropic API key is not set (or the vault is locked)", provider=self.provider_name)
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_key != key or self._client_loop is not loop:
            self._client = sdk.AsyncAnthropic(api_key=key, timeout=self.request_timeout, max_retries=2)
            self._client_key = key
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
        self._client = None

    def _map_error(self, exc: Exception) -> LLMError:
        sdk = self._sdk()
        if isinstance(exc, sdk.APITimeoutError):
            return LLMTimeout(f"Anthropic timed out: {exc}", provider=self.provider_name)
        if isinstance(exc, sdk.APIConnectionError):
            return LLMUnavailable(f"Anthropic unreachable: {exc}", provider=self.provider_name)
        if isinstance(exc, (sdk.AuthenticationError, sdk.PermissionDeniedError)):
            return LLMUnavailable("Anthropic rejected the API key", provider=self.provider_name, status_code=exc.status_code)
        if isinstance(exc, sdk.NotFoundError):
            return LLMModelMissing(f"Model '{self.model}' not found", provider=self.provider_name, status_code=404)
        if isinstance(exc, sdk.APIStatusError):
            return LLMError(f"Anthropic error {exc.status_code}: {exc.message}", provider=self.provider_name,
                            status_code=exc.status_code)
        return LLMError(f"Anthropic request failed: {exc}", provider=self.provider_name)

    def _params(self, messages: list[ChatMessage], max_tokens: int, tools: Optional[list]) -> dict:
        system, converted = convert_messages(messages)
        params: dict = {"model": self.model, "max_tokens": max_tokens, "messages": converted}
        if system:
            params["system"] = system
        if tools:
            params["tools"] = convert_tools(tools)
        return params

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        tools: Optional[list] = None,
    ) -> CompletionResponse:
        client = self._get_client()
        sdk = self._sdk()
        try:
            resp = await client.messages.create(**self._params(messages, max_tokens, tools))
        except sdk.APIError as exc:
            raise self._map_error(exc) from exc
        if resp.stop_reason == "refusal":
            raise LLMError("Claude declined this request", provider=self.provider_name)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: list[ToolCall] = []
        for b in resp.content:
            if b.type == "text":
                text_parts.append(b.text)
            elif b.type == "thinking":
                reasoning_parts.append(b.thinking or "")
            elif b.type == "tool_use":
                calls.append(ToolCall(id=b.id, name=b.name, arguments=dict(b.input or {})))
        blocks = [p for p in (_block_to_param(b) for b in resp.content) if p is not None]
        return CompletionResponse(
            content="".join(text_parts),
            model=self.model,
            provider=self.provider_name,
            instance_name=self.instance_name,
            input_tokens=getattr(resp.usage, "input_tokens", None),
            output_tokens=getattr(resp.usage, "output_tokens", None),
            tool_calls=calls or None,
            reasoning="".join(reasoning_parts) or None,
            provider_content=blocks,
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> AsyncIterator[StreamDelta]:
        client = self._get_client()
        sdk = self._sdk()
        try:
            async with client.messages.stream(**self._params(messages, max_tokens, None)) as stream:
                async for event in stream:
                    if event.type != "content_block_delta":
                        continue
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield StreamDelta(content=delta.text)
                    elif delta.type == "thinking_delta":
                        yield StreamDelta(reasoning=delta.thinking)
                final = await stream.get_final_message()
            if final.stop_reason == "refusal":
                raise LLMError("Claude declined this request", provider=self.provider_name)
        except sdk.APIError as exc:
            raise self._map_error(exc) from exc

    async def health_check(self) -> bool:
        """Key present and the model exists (models.retrieve is free; no tokens spent)."""
        try:
            client = self._get_client()
            await client.models.retrieve(self.model)
            return True
        except Exception:
            return False
