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
    StreamDelta,
    ToolCall,
)

# Context window sent with every request. Ollama's own default (2-4k tokens)
# silently truncates briefing transcripts from the front. Keep it constant per
# instance: changing num_ctx between calls forces Ollama to reload the model.
DEFAULT_NUM_CTX = 8192


class OllamaProvider(BaseLLMProvider):
    provider_name = "ollama"
    supports_tools = True

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        model: str = "qwen3.5:4b",
        num_ctx: Optional[int] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.instance_name = ""  # set by registry
        self.context_tokens = num_ctx or DEFAULT_NUM_CTX
        # Native Ollama API base (strip /v1 suffix)
        self._ollama_base = self.base_url.removesuffix("/v1")

    def _serialize_message(self, m: ChatMessage) -> dict:
        """Serialize a ChatMessage to Ollama's native /api/chat format."""
        msg: dict = {"role": m.role, "content": m.content}
        if m.tool_calls:
            # Native API wants arguments as an object; OpenAI-shaped callers
            # pass a JSON string, which Ollama rejects.
            calls = []
            for tc in m.tool_calls:
                func = dict(tc.get("function", {}))
                args = func.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except json.JSONDecodeError:
                        args = {"raw": args}
                func["arguments"] = args or {}
                calls.append({"function": func})
            msg["tool_calls"] = calls
        if m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        return msg

    def _body(self, messages: list[ChatMessage], max_tokens: int, temperature: float, stream: bool) -> dict:
        # think:false keeps thinking-mode models (e.g. Qwen 3.5) from spending
        # the whole budget on hidden reasoning and returning empty content.
        return {
            "model": self.model,
            "messages": [self._serialize_message(m) for m in messages],
            "stream": stream,
            "think": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
                "num_ctx": self.context_tokens,
            },
        }

    def _raise_status(self, status: int, body: str) -> None:
        low = body.lower()
        if status == 404 or ("model" in low and "not found" in low):
            raise LLMModelMissing(
                f"Model '{self.model}' is not installed in Ollama", provider=self.provider_name, status_code=status
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
        try:
            resp = await self._http().post(f"{self._ollama_base}/api/chat", json=body)
        except httpx.HTTPError as exc:
            self._raise_transport(exc)
        if resp.status_code != 200:
            self._raise_status(resp.status_code, resp.text)
        data = resp.json()
        if data.get("error"):
            self._raise_status(500, str(data["error"]))

        message_data = data.get("message", {})
        parsed_tool_calls = None
        raw_tool_calls = message_data.get("tool_calls")
        if raw_tool_calls:
            parsed_tool_calls = []
            for i, tc in enumerate(raw_tool_calls):
                func = tc.get("function", {})
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                parsed_tool_calls.append(ToolCall(
                    id=tc.get("id") or f"call_{i}",
                    name=func.get("name", ""),
                    arguments=args if isinstance(args, dict) else {"value": args},
                ))

        return CompletionResponse(
            content=message_data.get("content") or "",
            model=self.model,
            provider=self.provider_name,
            instance_name=self.instance_name,
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            tool_calls=parsed_tool_calls,
            reasoning=message_data.get("thinking") or None,
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> AsyncIterator[StreamDelta]:
        body = self._body(messages, max_tokens, temperature, stream=True)
        try:
            async with self._http().stream("POST", f"{self._ollama_base}/api/chat", json=body) as resp:
                if resp.status_code != 200:
                    raw = await resp.aread()
                    self._raise_status(resp.status_code, raw.decode(errors="replace"))
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        self._raise_status(500, str(chunk["error"]))
                    msg = chunk.get("message", {})
                    content = msg.get("content") or ""
                    reasoning = msg.get("thinking") or ""
                    if content or reasoning:
                        yield StreamDelta(content=content, reasoning=reasoning)
        except httpx.HTTPError as exc:
            self._raise_transport(exc)

    async def health_check(self) -> bool:
        """Check that Ollama is listening and this instance's model is installed — fast, no model load."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self._ollama_base}/api/tags")
                if resp.status_code != 200:
                    return False
                if not self.model:
                    # Admin provider (no model bound) — server reachability is enough
                    return True
                names = [m.get("name", "") for m in resp.json().get("models", [])]
                return self._model_installed(self.model, names)
        except Exception:
            return False

    @staticmethod
    def _model_installed(model: str, installed_names: list[str]) -> bool:
        return any(n == model or n.startswith(f"{model}:") for n in installed_names)

    async def list_models(self) -> list[dict]:
        """Return list of locally available Ollama models."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self._ollama_base}/api/tags")
        except httpx.HTTPError as exc:
            self._raise_transport(exc)
        if resp.status_code != 200:
            raise LLMError(resp.text, provider=self.provider_name, status_code=resp.status_code)
        return resp.json().get("models", [])

    async def pull_model_stream(self, model_name: str) -> AsyncIterator[dict]:
        """
        Stream pull progress dicts: {"status": str, "completed": int, "total": int}.
        Raises LLMError if Ollama is unreachable.
        """
        try:
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
        except httpx.HTTPError as exc:
            self._raise_transport(exc)
