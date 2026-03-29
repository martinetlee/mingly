import json
from collections.abc import AsyncIterator

import httpx

from typing import Optional

from backend.llm.base import BaseLLMProvider, ChatMessage, CompletionResponse, LLMError, ToolCall


class OllamaProvider(BaseLLMProvider):
    provider_name = "ollama"
    supports_tools = True  # Ollama's OpenAI-compatible API supports tools

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        model: str = "qwen2.5:7b",
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.instance_name = ""  # set by registry
        # Native Ollama API base (strip /v1 suffix)
        self._ollama_base = self.base_url.removesuffix("/v1")

    def _serialize_message(self, m: ChatMessage) -> dict:
        """Serialize a ChatMessage to the OpenAI API format."""
        msg: dict = {"role": m.role, "content": m.content}
        if m.tool_calls:
            msg["tool_calls"] = m.tool_calls
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        return msg

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        tools: Optional[list] = None,
    ) -> CompletionResponse:
        request_body: dict = {
            "model": self.model,
            "messages": [self._serialize_message(m) for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            request_body["tools"] = tools

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=request_body,
            )
        if resp.status_code != 200:
            raise LLMError(resp.text, provider=self.provider_name, status_code=resp.status_code)
        data = resp.json()
        usage = data.get("usage", {})

        # Parse tool calls from response
        message_data = data["choices"][0]["message"]
        parsed_tool_calls = None
        raw_tool_calls = message_data.get("tool_calls")
        if raw_tool_calls:
            parsed_tool_calls = []
            for tc in raw_tool_calls:
                func = tc.get("function", {})
                args = func.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                parsed_tool_calls.append(ToolCall(
                    id=tc.get("id", ""),
                    name=func.get("name", ""),
                    arguments=args,
                ))

        return CompletionResponse(
            content=message_data.get("content") or "",
            model=self.model,
            provider=self.provider_name,
            instance_name=self.instance_name,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            tool_calls=parsed_tool_calls,
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": [self._serialize_message(m) for m in messages],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": True,
                },
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise LLMError(body.decode(), provider=self.provider_name, status_code=resp.status_code)
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        chunk = json.loads(line[6:])
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta

    async def health_check(self) -> bool:
        """Check if Ollama process is listening — fast, no model load."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self._ollama_base}/api/tags")
                return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[dict]:
        """Return list of locally available Ollama models."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self._ollama_base}/api/tags")
        if resp.status_code != 200:
            raise LLMError(resp.text, provider=self.provider_name, status_code=resp.status_code)
        return resp.json().get("models", [])

    async def pull_model_stream(self, model_name: str) -> AsyncIterator[dict]:
        """
        Stream pull progress dicts: {"status": str, "completed": int, "total": int}.
        Raises LLMError if Ollama is unreachable.
        """
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{self._ollama_base}/api/pull",
                json={"name": model_name, "stream": True},
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise LLMError(body.decode(), provider=self.provider_name, status_code=resp.status_code)
                async for line in resp.aiter_lines():
                    if line:
                        yield json.loads(line)
