"""Provider layer: wire formats, streaming/reasoning, typed errors, role resolution."""

import json
from unittest.mock import patch

import httpx
import pytest

from backend.llm.anthropic import convert_messages, convert_tools
from backend.llm.base import ChatMessage, LLMError, LLMModelMissing, LLMTimeout, LLMUnavailable
from backend.llm.config import LLMRegistryConfig
from backend.llm.ollama import OllamaProvider
from backend.llm.openai import OpenAIProvider
from backend.llm.resolve import error_kind, get_provider


def _mock(provider, handler):
    """Route the provider's long-lived client through a MockTransport."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider._http = lambda: client
    return client


def test_ollama_serializes_tool_call_arguments_as_objects():
    p = OllamaProvider(model="m")
    msg = ChatMessage(role="assistant", content="", tool_calls=[
        {"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"q": "x"}'}},
    ])
    out = p._serialize_message(msg)
    assert out["tool_calls"] == [{"function": {"name": "f", "arguments": {"q": "x"}}}]


@pytest.mark.asyncio
async def test_ollama_request_sets_think_false_and_num_ctx_and_parses_tools():
    p = OllamaProvider(model="m", num_ctx=16384)
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "", "tool_calls": [
            {"function": {"name": "search", "arguments": {"keyword": "x"}}}]}})

    _mock(p, handler)
    resp = await p.complete([ChatMessage(role="user", content="hi")], tools=[{"type": "function"}])
    assert seen["think"] is False and seen["options"]["num_ctx"] == 16384
    assert resp.tool_calls[0].name == "search" and resp.tool_calls[0].arguments == {"keyword": "x"}
    assert resp.tool_calls[0].id  # synthesized when Ollama omits it


@pytest.mark.asyncio
async def test_ollama_missing_model_is_typed():
    p = OllamaProvider(model="nope")
    _mock(p, lambda r: httpx.Response(404, json={"error": "model 'nope' not found"}))
    with pytest.raises(LLMModelMissing):
        await p.complete([ChatMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_ollama_stream_yields_content_and_reasoning():
    p = OllamaProvider(model="m")
    body = "\n".join(json.dumps(c) for c in [
        {"message": {"thinking": "hmm"}},
        {"message": {"content": "Hel"}},
        {"message": {"content": "lo"}, "done": True},
    ])
    _mock(p, lambda r: httpx.Response(200, text=body))
    deltas = [d async for d in p.stream([ChatMessage(role="user", content="hi")])]
    assert "".join(d.content for d in deltas) == "Hello"
    assert "".join(d.reasoning for d in deltas) == "hmm"


@pytest.mark.asyncio
async def test_connect_error_is_unavailable_and_timeout_is_typed():
    p = OllamaProvider(model="m")

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    _mock(p, refuse)
    with pytest.raises(LLMUnavailable):
        await p.complete([ChatMessage(role="user", content="hi")])

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    _mock(p, slow)
    with pytest.raises(LLMTimeout):
        await p.complete([ChatMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_openai_requires_key_for_official_api():
    p = OpenAIProvider(model="gpt-4o")
    with patch("backend.secrets.get_secret", return_value=None):
        with pytest.raises(LLMUnavailable):
            await p.complete([ChatMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_openai_sends_bearer_and_parses_stream_with_reasoning():
    p = OpenAIProvider(model="gpt-4o")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        lines = [
            {"choices": [{"delta": {"reasoning_content": "think"}}]},
            {"choices": [{"delta": {"content": "Hi"}}]},
            {"choices": [{"delta": {"content": "!"}}]},
        ]
        text = "".join(f"data: {json.dumps(x)}\n\n" for x in lines) + "data: [DONE]\n\n"
        return httpx.Response(200, text=text)

    _mock(p, handler)
    with patch("backend.secrets.get_secret", return_value="sk-test"):
        deltas = [d async for d in p.stream([ChatMessage(role="user", content="hi")])]
    assert seen["auth"] == "Bearer sk-test"
    assert "".join(d.content for d in deltas) == "Hi!"
    assert "".join(d.reasoning for d in deltas) == "think"


@pytest.mark.asyncio
async def test_openai_compatible_local_server_needs_no_key_and_maps_401():
    p = OpenAIProvider(base_url="http://localhost:1234/v1", model="local")
    _mock(p, lambda r: httpx.Response(401, json={"error": "bad key"}))
    with patch("backend.secrets.get_secret", return_value=None):
        with pytest.raises(LLMUnavailable):
            await p.complete([ChatMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_openai_tool_calls_round_trip():
    p = OpenAIProvider(base_url="http://localhost:1234/v1", model="local")
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": '{"a": 1}'}}]}}]})

    _mock(p, handler)
    with patch("backend.secrets.get_secret", return_value=None):
        resp = await p.complete([ChatMessage(role="user", content="hi")], tools=[{"type": "function"}])
    assert seen["tool_choice"] == "auto"
    assert resp.tool_calls[0].arguments == {"a": 1}
    assert resp.tool_calls[0].to_openai()["function"]["arguments"] == '{"a": 1}'


def test_anthropic_message_conversion():
    msgs = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="q"),
        ChatMessage(role="assistant", content="", tool_calls=[
            {"id": "t1", "function": {"name": "f", "arguments": '{"x": 1}'}},
            {"id": "t2", "function": {"name": "g", "arguments": "{}"}},
        ]),
        ChatMessage(role="tool", content="r1", tool_call_id="t1"),
        ChatMessage(role="tool", content="r2", tool_call_id="t2"),
        ChatMessage(role="assistant", content="ignored", provider_content=[{"type": "thinking", "thinking": "", "signature": "s"}]),
    ]
    system, out = convert_messages(msgs)
    assert system == "sys"
    assert out[1]["content"][0] == {"type": "tool_use", "id": "t1", "name": "f", "input": {"x": 1}}
    assert out[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "r1"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "r2"},
    ]}
    assert out[3]["content"][0]["type"] == "thinking"  # provider blocks echoed unchanged
    tools = convert_tools([{"type": "function", "function": {"name": "f", "description": "d",
                                                             "parameters": {"type": "object"}}}])
    assert tools == [{"name": "f", "description": "d", "input_schema": {"type": "object"}}]


@pytest.mark.asyncio
async def test_anthropic_without_key_is_unavailable():
    from backend.llm.anthropic import AnthropicProvider
    with patch("backend.secrets.get_secret", return_value=None):
        with pytest.raises(LLMUnavailable):
            await AnthropicProvider(model="claude-opus-5-5").complete([ChatMessage(role="user", content="hi")])


def test_role_resolution_prefers_role_then_orchestrator_then_any():
    from backend.llm import registry as reg

    class R:
        def __init__(self, names):
            self.names = names

        def get(self, name):
            if name in self.names:
                return name
            raise KeyError(name)

        def get_any(self):
            return self.names[0] if self.names else None

    cfg = LLMRegistryConfig(orchestrator_name="main")
    with patch("backend.llm.resolve.load_registry_config", return_value=cfg):
        with patch("backend.llm.resolve.get_registry", return_value=R(["x", "main", "briefing"])):
            assert get_provider("briefing") == "briefing"
            assert get_provider("catchup") == "main"
            assert get_provider() == "main"
        with patch("backend.llm.resolve.get_registry", return_value=R(["x"])):
            assert get_provider("catchup") == "x"
        with patch("backend.llm.resolve.get_registry", return_value=R([])):
            with pytest.raises(LLMUnavailable):
                get_provider()
    assert reg  # module importable


def test_error_kinds():
    assert error_kind(LLMUnavailable("x")) == "llm_unavailable"
    assert error_kind(LLMModelMissing("x")) == "model_missing"
    assert error_kind(LLMTimeout("x")) == "timeout"
    assert error_kind(LLMError("x")) == "llm_error"
