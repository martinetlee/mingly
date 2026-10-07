import json
from collections.abc import AsyncIterator
from typing import Optional

import httpx

from backend.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    CompletionResponse,
    LLMError,
    LLMModelMissing,
    LLMUnavailable,
    StreamDelta,
    ToolCall,
)

# Vault key holding the API key (see backend.secrets). Set via PUT /api/llm/keys/openai.
API_KEY_SECRET = "openai_api_key"


class OpenAIProvider(BaseLLMProvider):
    """
    OpenAI and OpenAI-compatible providers (Grok, LM Studio, vLLM, ...), via the
    chat completions API. The API key comes from the vault; it is required for
    api.openai.com and optional for other (e.g. local) compatible servers.
    """
    provider_name = "openai"
    supports_tools = True

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
    ):
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.model = model
        self.instance_name = ""  # set by registry

    @property
    def _requires_key(self) -> bool:
        return "api.openai.com" in self.base_url

    def _headers(self) -> dict:
        from backend.secrets import get_secret
        key = get_secret(API_KEY_SECRET)
        if not key:
            if self._requires_key:
                raise LLMUnavailable(
                    "OpenAI API key is not set (or the vault is locked)", provider=self.provider_name
                )
            return {}
        return {"Authorization": f"Bearer {key}"}

    @staticmethod
    def _serialize_message(m: ChatMessage) -> dict:
        msg: dict = {"role": m.role, "content": m.content}
        if m.tool_calls:
            calls = []
            for tc in m.tool_calls:
                func = dict(tc.get("function", {}))
                if not isinstance(func.get("arguments"), str):
                    func["arguments"] = json.dumps(func.get("arguments") or {})
                calls.append({"id": tc.get("id", ""), "type": "function", "function": func})
            msg["tool_calls"] = calls
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        return msg

    def _body(self, messages, max_tokens, temperature, stream) -> dict:
        return {
            "model": self.model,
            "messages": [self._serialize_message(m) for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }

    def _raise_status(self, status: int, body: str) -> None:
        low = body.lower()
        if status in (401, 403):
            raise LLMUnavailable(
                f"{self.provider_name} rejected the API key ({status})", provider=self.provider_name, status_code=status
            )
        if status == 404 or "model_not_found" in low or ("model" in low and "does not exist" in low):
            raise LLMModelMissing(
                f"Model '{self.model}' is not available: {body[:200]}", provider=self.provider_name, status_code=status
            )
        raise LLMError(body[:500], provider=self.provider_name, status_code=status)

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        tools: Optional[list] = None,
    ) -> CompletionResponse:
        body = self._body(messages, max_tokens, temperature, stream=False)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            resp = await self._http().post(f"{self.base_url}/chat/completions", json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            self._raise_transport(exc)
        if resp.status_code != 200:
            self._raise_status(resp.status_code, resp.text)
        data = resp.json()
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"Malformed response: {str(data)[:200]}", provider=self.provider_name)

        parsed = None
        if msg.get("tool_calls"):
            parsed = []
            for i, tc in enumerate(msg["tool_calls"]):
                func = tc.get("function", {})
                args = func.get("arguments") or "{}"
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                parsed.append(ToolCall(id=tc.get("id") or f"call_{i}", name=func.get("name", ""), arguments=args))

        usage = data.get("usage") or {}
        return CompletionResponse(
            content=msg.get("content") or "",
            model=self.model,
            provider=self.provider_name,
            instance_name=self.instance_name,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            tool_calls=parsed,
            reasoning=msg.get("reasoning") or msg.get("reasoning_content") or None,
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> AsyncIterator[StreamDelta]:
        body = self._body(messages, max_tokens, temperature, stream=True)
        headers = self._headers()
        try:
            async with self._http().stream(
                "POST", f"{self.base_url}/chat/completions", json=body, headers=headers
            ) as resp:
                if resp.status_code != 200:
                    raw = await resp.aread()
                    self._raise_status(resp.status_code, raw.decode(errors="replace"))
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    if data.get("error"):
                        raise LLMError(str(data["error"])[:500], provider=self.provider_name)
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content") or ""
                    reasoning = delta.get("reasoning") or delta.get("reasoning_content") or ""
                    if content or reasoning:
                        yield StreamDelta(content=content, reasoning=reasoning)
        except httpx.HTTPError as exc:
            self._raise_transport(exc)

    async def health_check(self) -> bool:
        """Reachable and authorised (GET /models spends no tokens)."""
        try:
            resp = await self._http().get(f"{self.base_url}/models", headers=self._headers(), timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False
