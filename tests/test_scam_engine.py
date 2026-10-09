"""Scam engine: tool-call loop (incl. the Ollama 'classify' 400 reproduction),
JSON parsing, the eval-free short-circuit evaluator, target-only signatures,
and the target-preserving prompt builder."""

import json

import pytest

from backend.llm.base import CompletionResponse, LLMError, LLMUnavailable, ToolCall
from backend.llm.ollama import OllamaProvider
from backend.scam import engine
from backend.scam.client import ScanRequest, build_message_context
from backend.scam.engine import _evaluate_short_circuit, parse_llm_response
from backend.scam.pipeline import _parse_pipeline


# ---------------------------------------------------------------------------
# Fake Ollama: real OllamaProvider serialization + Ollama's server-side check
# ---------------------------------------------------------------------------

class FakeOllama(OllamaProvider):
    """Serializes exactly like the real provider, then applies the check the
    Ollama server does: assistant tool_call arguments must be a JSON object.
    A string there is what produced the 2,231 production failures."""

    def __init__(self, responses):
        super().__init__(model="qwen3.5:4b")
        self.instance_name = "fake-ollama"
        self.responses = list(responses)
        self.bodies = []

    async def complete(self, messages, *, max_tokens=1024, temperature=0.3, tools=None):
        wire = [self._serialize_message(m) for m in messages]
        self.bodies.append(wire)
        for msg in wire:
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc["function"]["arguments"], dict):
                    raise LLMError(
                        '{"error":"Value looks like object, but can\'t find closing \'}\' symbol"}',
                        provider="ollama", status_code=400,
                    )
        return self.responses.pop(0)


def _resp(content="", tool_calls=None):
    return CompletionResponse(content=content, model="m", provider="ollama", instance_name="x", tool_calls=tool_calls)


@pytest.fixture
def tools_registered():
    from backend.scam.tools import register_tools_from_pipeline
    raw = json.load(open("backend/scam/default_pipeline.json"))
    register_tools_from_pipeline(raw)
    return raw


def _use_provider(monkeypatch, provider):
    def _resolve(model, role):
        if provider is None:
            raise LLMUnavailable("No LLM is configured")
        assert role == engine.SCAM_LLM_ROLE
        return provider, None

    monkeypatch.setattr(engine, "get_provider_for_model", _resolve)


FINAL = '{"classification": "normal_conversation", "confidence": 0.9, "reasoning": "chat", "matched_scenario": null, "matched_safe_pattern": null}'


@pytest.mark.asyncio
async def test_classify_tool_round_trip_reaches_final_answer(monkeypatch, tools_registered):
    """Regression for `Node 'classify' failed: Value looks like object...`:
    the second request (echoing the tool call) must carry object arguments."""
    provider = FakeOllama([
        _resp("STEP 1: summary", [ToolCall(id="c1", name="search_scam_scenarios", arguments={})]),
        _resp("STEP 3", [ToolCall(id="c2", name="search_safe_patterns", arguments={})]),
        _resp("STEP 6: done\n" + FINAL),
    ])
    _use_provider(monkeypatch, provider)
    node = next(n for n in tools_registered["nodes"] if n["id"] == "classify")["config"]

    step = await engine._execute_llm_node(node, {}, "Messages to analyze:\n  [A]: hi", set())

    assert step.status == "completed", step.error
    assert step.output["classification"] == "normal_conversation"
    assert [c["tool"] for c in step.tool_calls] == ["search_scam_scenarios", "search_safe_patterns"]
    echoed = provider.bodies[1][2]["tool_calls"][0]["function"]["arguments"]
    assert echoed == {}


@pytest.mark.asyncio
async def test_old_string_arguments_reproduce_the_production_error(monkeypatch):
    """The exact production failure: string arguments on the wire → 400."""
    provider = FakeOllama([])
    from backend.llm.base import ChatMessage
    bad = ChatMessage(role="assistant", content="", tool_calls=[{"id": "x", "type": "function", "function": {"name": "t", "arguments": "{}"}}])
    # Bypass the provider fix to show what the server sees with a raw string
    monkeypatch.setattr(FakeOllama, "_serialize_message", lambda self, m: {
        "role": m.role, "content": m.content, "tool_calls": m.tool_calls})
    with pytest.raises(LLMError, match="Value looks like object"):
        await provider.complete([bad])


@pytest.mark.asyncio
async def test_tool_calls_recorded_even_when_a_later_call_fails(monkeypatch, tools_registered):
    class Failing(FakeOllama):
        async def complete(self, messages, **kw):
            if self.responses:
                return await super().complete(messages, **kw)
            raise LLMError("boom")

    provider = Failing([_resp("", [ToolCall(id="c1", name="search_scam_scenarios", arguments={})])])
    _use_provider(monkeypatch, provider)
    node = next(n for n in tools_registered["nodes"] if n["id"] == "classify")["config"]
    step = await engine._execute_llm_node(node, {}, "x", set())
    assert step.status == "failed"
    assert len(step.tool_calls) == 1


@pytest.mark.asyncio
async def test_exhausted_tool_iterations_force_a_final_answer(monkeypatch, tools_registered):
    calls = [_resp("still searching", [ToolCall(id=f"c{i}", name="search_scam_scenarios", arguments={})]) for i in range(2)]
    provider = FakeOllama(calls + [_resp(FINAL)])
    _use_provider(monkeypatch, provider)
    node = dict(next(n for n in tools_registered["nodes"] if n["id"] == "classify")["config"], max_tool_iterations=1)
    step = await engine._execute_llm_node(node, {}, "x", set())
    assert step.status == "completed", step.error
    assert len(provider.bodies) == 3  # 2 tool turns + 1 forced final answer (no tools)


@pytest.mark.asyncio
async def test_parallel_tool_calls_share_one_assistant_turn(monkeypatch, tools_registered):
    provider = FakeOllama([
        _resp("", [ToolCall(id="a", name="search_scam_scenarios", arguments={}),
                   ToolCall(id="b", name="search_safe_patterns", arguments={})]),
        _resp(FINAL),
    ])
    _use_provider(monkeypatch, provider)
    node = next(n for n in tools_registered["nodes"] if n["id"] == "classify")["config"]
    step = await engine._execute_llm_node(node, {}, "x", set())
    assert step.status == "completed"
    second = provider.bodies[1]
    assistant_turns = [m for m in second if m["role"] == "assistant" and m.get("tool_calls")]
    assert len(assistant_turns) == 1 and len(assistant_turns[0]["tool_calls"]) == 2
    assert [m["role"] for m in second[-2:]] == ["tool", "tool"]


# ---------------------------------------------------------------------------
# Parsing / short-circuit
# ---------------------------------------------------------------------------

def test_parse_llm_response_takes_last_object_after_reasoning():
    raw = 'I considered {not json} and {"a": 1}.\nFinal:\n```json\n{"is_scam": True, "confidence": 0.7}\n```'
    assert parse_llm_response(raw) == {"is_scam": True, "confidence": 0.7}


def test_parse_llm_response_rejects_garbage():
    with pytest.raises(ValueError):
        parse_llm_response("no json here")
    with pytest.raises(ValueError):
        parse_llm_response("   ")


@pytest.mark.parametrize("cond,out,expected", [
    ('output.verdict == "safe"', {"verdict": "safe"}, True),
    ('output.verdict == "safe"', {"verdict": "borderline"}, False),
    ("output.risk_score < 0.2 && output.verdict != null", {"risk_score": 0.1, "verdict": "x"}, True),
    ("output.risk_score >= 0.9 || output.flag == true", {"risk_score": 0.1, "flag": True}, True),
    ("output.a.b == 3", {"a": {"b": 3}}, True),
    ("output.missing < 0.5", {}, False),  # null < number → not triggered
    ('output.s == "a&&b"', {"s": "a&&b"}, True),  # && inside a string is data
    ("output.x == -1", {"x": -1}, True),
])
def test_short_circuit_evaluator(cond, out, expected):
    assert _evaluate_short_circuit(cond, out) is expected


@pytest.mark.parametrize("cond", [
    "__import__('os').system('echo pwned')",
    "output.__class__ == 1",
    "(lambda: 1)() == 1",
    "output.verdict.upper() == 'SAFE'",
    "[1][0] == 1",
    "open('x') == 1",
])
def test_short_circuit_rejects_code(cond):
    assert _evaluate_short_circuit(cond, {"verdict": "safe"}) is False


def test_short_circuit_has_no_eval():
    import inspect
    import re
    src = inspect.getsource(engine)
    assert not re.search(r"(?<![\w.])eval\(", src)


# ---------------------------------------------------------------------------
# Targets vs context
# ---------------------------------------------------------------------------

def _req(target_text="TARGET", n_context=5, ctx_len=400):
    ctx = [{"message_id": i, "identity_id": 7, "sender_name": "S", "text": f"ctx{i} " + "x" * ctx_len}
           for i in range(n_context)]
    return ScanRequest(conversation_id=1, channel="telegram",
                       targets=[{"message_id": 99, "identity_id": 7, "sender_name": "S", "text": target_text}],
                       context=ctx, metadata={"title": "G", "kind": "group", "channel": "telegram"})


def test_prompt_always_ends_with_untruncated_target():
    long_target = "pay me " * 2000  # far beyond the budget
    text = build_message_context(_req(long_target, n_context=50), budget=3000)
    assert text.rstrip().endswith(long_target.strip())
    assert "Messages to analyze:" in text
    assert "ctx" not in text.split("Messages to analyze:")[1]
    assert text.startswith("Conversation: G (group on telegram)")


def test_context_fills_remaining_budget_newest_first():
    text = build_message_context(_req(n_context=10, ctx_len=400), budget=2000)
    assert "ctx9" in text and "ctx0" not in text
    assert text.index("ctx9") < text.index("TARGET")


@pytest.mark.asyncio
async def test_signatures_only_match_targets(monkeypatch):
    cfg = _parse_pipeline({"version": "t", "system_prompt": "x",
                           "signatures": {"domains": ["evil.xyz"], "wallets": [], "patterns": [], "telegram_ids": []}})
    cfg.signatures.domains = ["evil.xyz"]
    req = ScanRequest(conversation_id=1, channel="telegram",
                      targets=[{"message_id": 2, "identity_id": 1, "sender_name": "A", "text": "hello"}],
                      context=[{"message_id": 1, "identity_id": 1, "sender_name": "A", "text": "go to evil.xyz now"}])

    _use_provider(monkeypatch, None)
    res = await engine.execute_pipeline(cfg, build_message_context(req), req.targets)
    assert res.trace.status == "failed"  # no short-circuit from context; LLM unavailable
    req.targets[0]["text"] = "see evil.xyz"
    res = await engine.execute_pipeline(cfg, build_message_context(req), req.targets)
    assert res.trace.status == "short_circuited" and res.scam_result.is_scam


@pytest.mark.asyncio
async def test_known_telegram_ids_match_only_telegram_senders(monkeypatch):
    cfg = _parse_pipeline({"version": "t", "system_prompt": "x",
                           "signatures": {"domains": [], "wallets": [], "patterns": [], "telegram_ids": [555]}})
    cfg.signatures.telegram_ids = [555]
    _use_provider(monkeypatch, None)
    target = {"message_id": 3, "identity_id": 9, "telegram_user_id": 555, "sender_name": "M", "text": "hi"}
    res = await engine.execute_pipeline(cfg, "ctx", [target], channel="telegram", conversation_kind="direct")
    assert res.trace.status == "short_circuited" and res.trace.short_circuit["matched_value"] == "555"
    assert res.trace.channel == "telegram" and res.trace.conversation_kind == "direct"
    # The same number on another channel (or an internal identity id) is not a Telegram user
    res = await engine.execute_pipeline(cfg, "ctx", [target], channel="gmail")
    assert res.trace.status == "failed"
    res = await engine.execute_pipeline(cfg, "ctx", [{"message_id": 3, "identity_id": 555, "text": "hi"}],
                                        channel="telegram")
    assert res.trace.status == "failed"


@pytest.mark.asyncio
async def test_local_client_passes_channel_and_kind_to_nodes(monkeypatch):
    from backend.scam.client import LocalPipelineClient

    seen = {}

    async def fake_execute(cfg, ctx, targets, *, channel=None, conversation_kind=None):
        seen.update(channel=channel, kind=conversation_kind)
        return engine.EngineResult(scam_result=engine.ScamCheckResult(is_scam=False, confidence=0.1, reason=""),
                                   trace=None)

    monkeypatch.setattr(engine, "execute_pipeline", fake_execute)
    cfg = _parse_pipeline({"version": "t", "system_prompt": "x"})
    await LocalPipelineClient(cfg).is_this_scam("r", _req())
    assert seen == {"channel": "telegram", "kind": "group"}
    assert engine.substitute_placeholders("{{$channel}}/{{$conversation_kind}}",
                                          {"$channel": "telegram", "$conversation_kind": "group"}) == "telegram/group"


@pytest.mark.asyncio
async def test_failed_pipeline_duration_is_elapsed_not_clock(monkeypatch):
    cfg = _parse_pipeline({"version": "t", "system_prompt": "x"})

    _use_provider(monkeypatch, None)
    res = await engine.execute_pipeline(cfg, "ctx", [{"message_id": 1, "text": "hi"}])
    assert res.scam_result.error
    assert 0 <= res.trace.duration_ms < 10_000


@pytest.mark.asyncio
async def test_pinned_model_resolution_goes_through_resolver(monkeypatch):
    """A node pinning a model gets that instance; otherwise the 'scam' role
    provider with a warning (backend.llm.resolve.get_provider_for_model)."""
    from backend.llm import resolve

    pinned, fallback = FakeOllama([_resp(FINAL)]), FakeOllama([_resp(FINAL)])
    fallback.instance_name = "orchestrator"

    class _Reg:
        def get_by_model(self, m):
            return pinned if m == "qwen3.5:4b" else None

    monkeypatch.setattr(resolve, "get_registry", lambda: _Reg())
    monkeypatch.setattr(resolve, "get_provider", lambda role: fallback)
    step = await engine._execute_llm_node({"model": "qwen3.5:4b", "prompt": "p"}, {}, "x", set())
    assert step.model_used == "fake-ollama" and step.model_warning is None
    step = await engine._execute_llm_node({"model": "missing:1b", "prompt": "p"}, {}, "x", set())
    assert step.model_used == "orchestrator" and "missing:1b" in step.model_warning
