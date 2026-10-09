"""DAG-based pipeline execution engine for scam detection.

Executes a directed acyclic graph of detection steps (LLM calls, scripts,
classifiers). Phase 1 supports LLM nodes only. The engine handles validation,
topological sorting, parallel layer execution, and full trace recording.
"""

import ast
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from backend.llm.base import ChatMessage, LLMError
from backend.llm.resolve import LLMUnavailable, get_provider_for_model
from backend.scam.channel_ids import TELEGRAM
from backend.scam.client import ScamCheckResult

logger = logging.getLogger(__name__)

# LLM role for pipeline nodes that don't pin a model: an instance named
# "scam" wins, else the orchestrator (see backend.llm.resolve).
SCAM_LLM_ROLE = "scam"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class StepTrace:
    node_id: str
    type: str
    layer: int
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: int = 0
    status: str = "pending"  # pending, completed, failed, skipped, skipped_short_circuit
    error: Optional[str] = None

    # LLM-specific
    model_requested: Optional[str] = None
    model_used: Optional[str] = None
    model_name: Optional[str] = None  # actual model name e.g. "llama3.2:latest"
    provider_name: Optional[str] = None  # e.g. "ollama", "anthropic", "openai"
    model_warning: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    prompt_sent: Optional[str] = None
    raw_response: Optional[str] = None

    # Script-specific
    script: Optional[str] = None

    # Tool use
    tool_calls: list[dict] = field(default_factory=list)  # [{tool, args, result, duration_ms}]

    # All types
    input: Optional[dict] = None
    output: Optional[dict] = None


@dataclass
class PipelineTrace:
    pipeline_version: str
    started_at: str
    completed_at: Optional[str] = None
    duration_ms: int = 0
    status: str = "running"  # completed, failed, short_circuited
    error: Optional[str] = None
    short_circuit: Optional[dict] = None
    input: Optional[str] = None  # the message context fed to the pipeline
    channel: Optional[str] = None  # telegram | gmail | ... of the conversation
    conversation_kind: Optional[str] = None  # direct | group | channel | email_thread
    steps: list[StepTrace] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


@dataclass
class EngineResult:
    scam_result: ScamCheckResult
    trace: PipelineTrace


# ---------------------------------------------------------------------------
# DAG Validation & Layer Computation
# ---------------------------------------------------------------------------

def _build_adjacency(nodes: list, edges: list) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Build predecessor and successor adjacency lists."""
    node_ids = {n.id for n in nodes}
    predecessors: dict[str, list[str]] = {n.id: [] for n in nodes}
    successors: dict[str, list[str]] = {n.id: [] for n in nodes}

    for edge in edges:
        if edge.from_node == "$input":
            # $input is a virtual source, not a real node
            continue
        if edge.from_node not in node_ids:
            raise ValueError(f"Edge references unknown source node: {edge.from_node}")
        if edge.to_node not in node_ids:
            raise ValueError(f"Edge references unknown target node: {edge.to_node}")
        predecessors[edge.to_node].append(edge.from_node)
        successors[edge.from_node].append(edge.to_node)

    return predecessors, successors


def _topological_sort(nodes: list, predecessors: dict[str, list[str]]) -> list[str]:
    """Kahn's algorithm for topological sort. Raises on cycles."""
    in_degree = {n.id: len(predecessors[n.id]) for n in nodes}
    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    order = []

    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for n in nodes:
            if nid in predecessors.get(n.id, []):
                in_degree[n.id] -= 1
                if in_degree[n.id] == 0:
                    queue.append(n.id)

    if len(order) != len(nodes):
        raise ValueError("Pipeline contains a cycle")

    return order


def _compute_layers(nodes: list, predecessors: dict[str, list[str]]) -> list[list[str]]:
    """Group nodes into layers for parallel execution."""
    node_layer: dict[str, int] = {}

    # Nodes with no predecessors are layer 0
    for n in nodes:
        if not predecessors[n.id]:
            node_layer[n.id] = 0

    # BFS to assign layers
    changed = True
    while changed:
        changed = False
        for n in nodes:
            if n.id in node_layer:
                continue
            pred_layers = []
            all_resolved = True
            for pred_id in predecessors[n.id]:
                if pred_id in node_layer:
                    pred_layers.append(node_layer[pred_id])
                else:
                    all_resolved = False
                    break
            if all_resolved and pred_layers:
                node_layer[n.id] = max(pred_layers) + 1
                changed = True

    # Group by layer
    max_layer = max(node_layer.values()) if node_layer else 0
    layers: list[list[str]] = [[] for _ in range(max_layer + 1)]
    for nid, layer in node_layer.items():
        layers[layer].append(nid)

    return layers


def validate_pipeline(nodes: list, edges: list, output: str) -> tuple[dict[str, list[str]], list[list[str]]]:
    """Validate pipeline DAG. Returns (predecessors, layers). Raises on error."""
    node_ids = {n.id for n in nodes}

    if output not in node_ids:
        raise ValueError(f"Output node '{output}' not found in pipeline nodes")

    predecessors, _ = _build_adjacency(nodes, edges)
    _topological_sort(nodes, predecessors)  # validates no cycles
    layers = _compute_layers(nodes, predecessors)

    # Verify output node is reachable (it must be in some layer)
    if output not in {nid for layer in layers for nid in layer}:
        raise ValueError(f"Output node '{output}' is not reachable from $input")

    return predecessors, layers


# ---------------------------------------------------------------------------
# Template Substitution
# ---------------------------------------------------------------------------

def substitute_placeholders(template: str, inputs: dict[str, Any]) -> str:
    """Replace {{node_id}} and {{node_id.field}} placeholders in a template string."""
    import re as _re

    def _replace(match):
        key = match.group(1)
        if "." in key:
            parts = key.split(".", 1)
            base = parts[0]
            field_path = parts[1]
            val = inputs.get(base)
            if isinstance(val, dict):
                # Navigate nested path
                for part in field_path.split("."):
                    if isinstance(val, dict):
                        val = val.get(part)
                    else:
                        val = None
                        break
            return str(val) if val is not None else ""
        val = inputs.get(key)
        if isinstance(val, dict):
            return json.dumps(val, indent=2, default=str)
        return str(val) if val is not None else ""

    return _re.sub(r"\{\{(\$?[\w.]+)\}\}", _replace, template)


# ---------------------------------------------------------------------------
# LLM Response Parsing
# ---------------------------------------------------------------------------

def parse_llm_response(raw: str) -> dict:
    """Parse the final JSON object out of an LLM response.

    Models often write reasoning (which may itself contain braces) before the
    answer, so take the *last* complete JSON object in the text rather than
    slicing from the first '{' to the last '}'.
    """
    content = raw.strip()
    if not content:
        raise ValueError("LLM returned empty response")

    # Fix common LLM JSON mistakes: Python-style True/False/None → JSON true/false/null
    content = content.replace(": True", ": true").replace(": False", ": false").replace(": None", ": null")

    decoder = json.JSONDecoder()
    last: Optional[dict] = None
    first_error: Optional[json.JSONDecodeError] = None
    idx = content.find("{")
    while idx != -1:
        try:
            obj, end = decoder.raw_decode(content, idx)
        except json.JSONDecodeError as e:
            first_error = first_error or e
            idx = content.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            last = obj
        idx = content.find("{", end)
    if last is not None:
        return last
    if first_error is not None:
        raise first_error
    return json.loads(content)  # raises a JSONDecodeError describing the text


# ---------------------------------------------------------------------------
# Tool Use Helpers
# ---------------------------------------------------------------------------

import re as _re

_TOOL_CALL_RE = _re.compile(r'TOOL_CALL:\s*(\w+)\((.+?)\)', _re.DOTALL)


def _build_native_tool_defs(tools: list) -> list[dict]:
    """Build OpenAI-format tool definitions for native function calling."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
        }
        for t in tools
    ]


def _build_tool_prompt(tools: list) -> str:
    """Build prompt injection for text-based tool use fallback."""
    lines = ["You have access to the following tools:"]
    for i, t in enumerate(tools, 1):
        params = ", ".join(f'{k}: {v.get("type", "string")}' for k, v in t.parameters.get("properties", {}).items())
        lines.append(f"\n{i}. {t.name}({params}) - {t.description}")
    lines.append("\nTo call a tool, respond with EXACTLY this format on its own line:")
    lines.append('TOOL_CALL: tool_name("argument")')
    lines.append("\nWait for the tool result before giving your final JSON answer.")
    lines.append("If you don't need any tools, respond directly with your JSON answer.")
    return "\n".join(lines)


def _parse_text_tool_calls(text: str) -> list:
    """Parse TOOL_CALL: lines from LLM text response."""
    from backend.llm.base import ToolCall
    calls = []
    for match in _TOOL_CALL_RE.finditer(text):
        name = match.group(1)
        args_str = match.group(2).strip().strip('"').strip("'")
        calls.append(ToolCall(id=f"text_{name}_{len(calls)}", name=name, arguments={"query": args_str}))
    return calls


# ---------------------------------------------------------------------------
# Short-Circuit Evaluator
# ---------------------------------------------------------------------------

_SC_LITERALS = {"true": True, "false": False, "null": None, "True": True, "False": False, "None": None}
_SC_COMPARE = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.Gt: lambda a, b: a > b,
    ast.LtE: lambda a, b: a <= b,
    ast.GtE: lambda a, b: a >= b,
}


def _sc_normalize(condition: str) -> str:
    """Translate &&/|| to and/or outside string literals."""
    out = []
    quote = None
    i = 0
    while i < len(condition):
        ch = condition[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < len(condition):
                out.append(condition[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif condition.startswith("&&", i):
            out.append(" and ")
            i += 1
        elif condition.startswith("||", i):
            out.append(" or ")
            i += 1
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _sc_eval(node: ast.AST, output: dict) -> Any:
    if isinstance(node, ast.Expression):
        return _sc_eval(node.body, output)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_sc_eval(v, output) for v in node.values)
        return any(_sc_eval(v, output) for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _sc_eval(node.operand, output)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        val = _sc_eval(node.operand, output)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return -val
        raise ValueError("unary minus on non-number")
    if isinstance(node, ast.Compare):
        left = _sc_eval(node.left, output)
        for op, comp in zip(node.ops, node.comparators):
            fn = _SC_COMPARE.get(type(op))
            if fn is None:
                raise ValueError(f"operator {type(op).__name__} not allowed")
            right = _sc_eval(comp, output)
            try:
                if not fn(left, right):
                    return False
            except TypeError:
                return False  # e.g. null < 0.5
            left = right
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _SC_LITERALS:
            return _SC_LITERALS[node.id]
        raise ValueError(f"unknown name '{node.id}'")
    if isinstance(node, ast.Attribute):
        path = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            path.insert(0, cur.attr)
            cur = cur.value
        if not (isinstance(cur, ast.Name) and cur.id == "output"):
            raise ValueError("only output.<field> paths are allowed")
        resolved: Any = output
        for part in path:
            resolved = resolved.get(part) if isinstance(resolved, dict) else None
        return resolved
    raise ValueError(f"{type(node).__name__} not allowed in short-circuit condition")


def _evaluate_short_circuit(condition: str, output: dict) -> bool:
    """Evaluate a short-circuit condition against a node's output (AST walk, no Python eval).

    Supports: output.field(.sub), ==, !=, >, <, >=, <=, &&/and, ||/or, !/not via
    'not', true/false/null, numbers and quoted strings. Anything else is
    rejected (treated as not triggered).
    """
    if not condition or not condition.strip():
        return False
    try:
        tree = ast.parse(_sc_normalize(condition.strip()), mode="eval")
        return bool(_sc_eval(tree, output))
    except Exception as e:
        logger.warning("Short-circuit evaluation failed for '%s': %s", condition, e)
        return False


# ---------------------------------------------------------------------------
# Node Executors
# ---------------------------------------------------------------------------

async def _execute_llm_node(
    node_config: dict,
    inputs: dict[str, Any],
    message_context: str,
    categories: set[str],
) -> StepTrace:
    """Execute an LLM node with optional tool use and return its trace."""
    trace = StepTrace(node_id="", type="llm", layer=0)
    t0 = time.monotonic()
    trace.started_at = datetime.now(timezone.utc).isoformat()

    # Get model
    model_requested = node_config.get("model")
    trace.model_requested = model_requested

    try:
        provider, trace.model_warning = get_provider_for_model(model_requested, SCAM_LLM_ROLE)
    except LLMUnavailable as e:
        trace.status = "failed"
        trace.error = str(e) or "No LLM provider available"
        trace.duration_ms = int((time.monotonic() - t0) * 1000)
        trace.completed_at = datetime.now(timezone.utc).isoformat()
        return trace

    trace.model_used = provider.instance_name
    trace.model_name = getattr(provider, 'model', None) or provider.instance_name
    trace.provider_name = getattr(provider, 'provider_name', None)

    # Build prompt from template
    prompt_template = node_config.get("prompt", "")
    sub_inputs = {**inputs, "$message": message_context}
    prompt = substitute_placeholders(prompt_template, sub_inputs)

    # Build LLM messages
    max_tokens = node_config.get("max_tokens", 200)
    temperature = node_config.get("temperature", 0.1)
    trace.max_tokens = max_tokens
    trace.temperature = temperature

    llm_messages: list[ChatMessage] = [
        ChatMessage(role="system", content=prompt),
    ]

    # Add few-shot examples
    few_shot = node_config.get("few_shot_examples", [])
    for example in few_shot[:2]:
        if isinstance(example, dict):
            ex_input = example.get("input", "")
            ex_output = example.get("output", "")
            llm_messages.append(ChatMessage(role="user", content=ex_input))
            llm_messages.append(ChatMessage(role="assistant", content=ex_output))

    # Add actual message
    llm_messages.append(ChatMessage(role="user", content=message_context))

    trace.prompt_sent = prompt
    trace.input = dict(sub_inputs)

    # --- Tool setup ---
    from backend.scam.tools import get_tool
    tool_names = node_config.get("tools", [])
    max_tool_iterations = node_config.get("max_tool_iterations", 3)
    tools = [get_tool(name) for name in tool_names if get_tool(name)]
    use_native = getattr(provider, 'supports_tools', False) and bool(tools)

    # Inject tool prompt for text-based fallback
    if tools and not use_native:
        llm_messages[0] = ChatMessage(
            role="system",
            content=llm_messages[0].content + "\n\n" + _build_tool_prompt(tools),
        )

    # Bound to the trace up front so tool calls are recorded even if a later
    # LLM call fails.
    tool_calls_made: list[dict] = []
    trace.tool_calls = tool_calls_made
    response = None
    try:
        exhausted_with_calls = False

        for iteration in range(max_tool_iterations + 1):
            # Call LLM
            if use_native and tools:
                response = await provider.complete(
                    llm_messages, max_tokens=max_tokens, temperature=temperature,
                    tools=_build_native_tool_defs(tools),
                )
            else:
                response = await provider.complete(
                    llm_messages, max_tokens=max_tokens, temperature=temperature,
                )

            # Check for tool calls
            pending_calls = []
            if use_native and response.tool_calls:
                pending_calls = response.tool_calls
            elif tools and not use_native and response.content:
                pending_calls = _parse_text_tool_calls(response.content)

            if not pending_calls:
                exhausted_with_calls = False
                break  # Final answer
            exhausted_with_calls = True

            # One assistant turn carrying every tool call, then one result per
            # call (the shape chat APIs expect for parallel tool calls).
            if use_native:
                llm_messages.append(ChatMessage(
                    role="assistant", content=response.content or "",
                    tool_calls=[
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                        for tc in pending_calls
                    ],
                ))
            else:
                llm_messages.append(ChatMessage(role="assistant", content=response.content or ""))

            text_results = []
            for tc in pending_calls:
                tool = get_tool(tc.name)
                tc_t0 = time.monotonic()
                if not tool:
                    logger.warning("Tool '%s' not found in registry", tc.name)
                    result = {"error": f"Unknown tool '{tc.name}'"}
                else:
                    try:
                        result = await tool.execute(tc.arguments)
                    except Exception as e:
                        result = {"error": str(e)}
                tool_calls_made.append({
                    "iteration": iteration,
                    "tool": tc.name,
                    "args": tc.arguments,
                    "result": result,
                    "duration_ms": int((time.monotonic() - tc_t0) * 1000),
                })
                if use_native:
                    llm_messages.append(ChatMessage(
                        role="tool", content=json.dumps(result, default=str), tool_call_id=tc.id,
                    ))
                else:
                    text_results.append(f"Tool result for {tc.name}:\n{json.dumps(result, indent=2, default=str)}")

            if not use_native:
                llm_messages.append(ChatMessage(
                    role="user", content="\n\n".join(text_results) + "\n\nNow provide your final JSON answer.",
                ))

        # Parse final response
        raw_content = (response.content if response else "") or ""

        # The model is still calling tools after the last allowed iteration, or
        # answered with nothing after tool use: ask once more, without tools.
        if tool_calls_made and (exhausted_with_calls or not raw_content.strip()):
            llm_messages.append(ChatMessage(role="user", content="Now provide your final JSON answer based on the tool results above."))
            final_response = await provider.complete(llm_messages, max_tokens=max_tokens, temperature=temperature)
            raw_content = final_response.content or ""

        trace.raw_response = raw_content

        parsed = parse_llm_response(raw_content)

        # Validate category
        category = parsed.get("category")
        if category and categories and category not in categories:
            category = "other"
            parsed["category"] = "other"

        trace.output = parsed
        trace.status = "completed"

    except (json.JSONDecodeError, ValueError) as e:
        trace.status = "failed"
        trace.error = f"LLM returned invalid JSON: {e}"
        if trace.raw_response is None and response:
            trace.raw_response = response.content
    except (LLMError, Exception) as e:
        trace.status = "failed"
        trace.error = str(e) or f"{type(e).__name__} (no message)"

    trace.duration_ms = int((time.monotonic() - t0) * 1000)
    trace.completed_at = datetime.now(timezone.utc).isoformat()
    return trace


# ---------------------------------------------------------------------------
# Script Node Executor
# ---------------------------------------------------------------------------

async def _execute_script_node(
    node_config: dict,
    inputs: dict[str, Any],
    message_context: str,
    target_text: str = "",
) -> StepTrace:
    """Execute a script node by calling a registered tool/script function."""
    trace = StepTrace(node_id="", type="script", layer=0)
    t0 = time.monotonic()
    trace.started_at = datetime.now(timezone.utc).isoformat()

    script_name = node_config.get("script", "")
    params = node_config.get("params", {})
    trace.script = script_name

    from backend.scam.tools import get_tool  # importing registers the tools
    tool = get_tool(script_name)
    if not tool:
        # If the script isn't available, return a neutral result instead of failing
        # This allows the pipeline to continue without the ML prefilter
        logger.warning("Script '%s' not found in registry — skipping with neutral result", script_name)
        trace.status = "completed"
        trace.output = {"verdict": "unavailable", "risk_score": 0.0, "reason": f"Script '{script_name}' not available"}
        trace.duration_ms = int((time.monotonic() - t0) * 1000)
        trace.completed_at = datetime.now(timezone.utc).isoformat()
        return trace

    # Build args: merge params with predecessor inputs + $message. Scripts
    # that judge text (ML prefilter) use $target_text: the targets only.
    args = {**params, **inputs, "$message": message_context, "$target_text": target_text}
    trace.input = args

    try:
        result = await tool.execute(args)
        trace.output = result
        trace.status = "completed"
    except Exception as e:
        trace.status = "failed"
        trace.error = str(e)

    trace.duration_ms = int((time.monotonic() - t0) * 1000)
    trace.completed_at = datetime.now(timezone.utc).isoformat()
    return trace


# ---------------------------------------------------------------------------
# Pipeline Execution
# ---------------------------------------------------------------------------

def _finish(trace: PipelineTrace, t0: float, status: str, error: Optional[str] = None) -> None:
    trace.status = status
    trace.error = error
    trace.completed_at = datetime.now(timezone.utc).isoformat()
    trace.duration_ms = int((time.monotonic() - t0) * 1000)


def _failed_result(trace: PipelineTrace, t0: float, error: str) -> EngineResult:
    _finish(trace, t0, "failed", error)
    return EngineResult(
        scam_result=ScamCheckResult(is_scam=False, confidence=0.0, reason="", error=error, trace_json=trace.to_json()),
        trace=trace,
    )


def _signature_match(sigs, targets: list[dict], channel: Optional[str] = None) -> Optional[dict]:
    """First signature hit among the *target* messages (context never matches).
    Known-scammer ids are Telegram sender ids (as before the multi-channel
    refactor: the user, or a chat posting as itself), so they only match
    Telegram senders."""
    from backend.scam.client import _extract_domains

    known_ids = set(sigs.telegram_ids) if channel == TELEGRAM else set()
    for msg in targets:
        text = msg.get("text") or ""
        user_id = msg["telegram_sender_id"] if "telegram_sender_id" in msg else msg.get("telegram_user_id")
        if user_id and user_id in known_ids:
            return {"match_type": "telegram_id", "matched_value": str(user_id), "confidence": 0.95,
                    "category": "impersonation", "reason": "Sender matches known scammer ID"}
        if not text:
            continue
        if sigs.domains:
            for domain in _extract_domains(text):
                if domain in sigs.domains:
                    return {"match_type": "domain", "matched_value": domain, "confidence": 0.95,
                            "category": "malware_link", "reason": f"Known malicious domain: {domain}"}
        lowered = text.lower()
        for wallet in sigs.wallets:
            if wallet.lower() in lowered:
                return {"match_type": "wallet", "matched_value": wallet, "confidence": 0.95,
                        "category": "investment_scam", "reason": "Known scam wallet address"}
        for pattern in sigs._compiled_patterns:
            if pattern.search(text):
                return {"match_type": "pattern", "matched_value": pattern.pattern, "confidence": 0.85,
                        "category": "other", "reason": "Matches scam pattern"}
    return None


async def execute_pipeline(
    pipeline_config,
    message_context: str,
    targets: list[dict],
    *,
    channel: Optional[str] = None,
    conversation_kind: Optional[str] = None,
) -> EngineResult:
    """Execute a pipeline DAG and return the result with full trace.

    Args:
        pipeline_config: PipelineConfig with nodes, edges, output, signatures, categories
        message_context: Rendered prompt input (conversation info + profiles + context + targets)
        targets: The message dicts being judged. Signature checks and the ML
            prefilter look only at these.
        channel: The conversation's channel (telegram, gmail, ...); nodes see
            it as ``{{$channel}}``.
        conversation_kind: direct | group | channel | email_thread; nodes see
            it as ``{{$conversation_kind}}``.
    """
    t0 = time.monotonic()
    trace = PipelineTrace(
        pipeline_version=pipeline_config.version,
        started_at=datetime.now(timezone.utc).isoformat(),
        input=message_context,
        channel=channel,
        conversation_kind=conversation_kind,
    )
    target_text = "\n".join((m.get("text") or "") for m in targets if m.get("text"))
    scan_inputs = {"$channel": channel or "", "$conversation_kind": conversation_kind or ""}

    # --- Step 1: Signature check (targets only) ---
    hit = _signature_match(pipeline_config.signatures, targets, channel)
    if hit:
        reason = hit.pop("reason")
        trace.short_circuit = hit
        _finish(trace, t0, "short_circuited")
        return EngineResult(
            scam_result=ScamCheckResult(is_scam=True, confidence=hit["confidence"], reason=reason,
                                        category=hit["category"], trace_json=trace.to_json()),
            trace=trace,
        )

    # --- Step 2: Validate & compute layers ---
    nodes = pipeline_config.nodes
    edges = pipeline_config.edges
    output_node_id = pipeline_config.output

    if not nodes:
        return _failed_result(trace, t0, "Pipeline has no nodes")

    try:
        predecessors, layers = validate_pipeline(nodes, edges, output_node_id)
    except ValueError as e:
        return _failed_result(trace, t0, f"Pipeline validation failed: {e}")

    # --- Step 3: Execute layers ---
    node_map = {n.id: n for n in nodes}
    results: dict[str, dict] = {}
    category_ids = pipeline_config.get_category_ids()

    def _skip_rest(layer_idx: int, status: str) -> None:
        for later_idx, later in enumerate(layers[layer_idx + 1:], start=layer_idx + 1):
            for rn in later:
                trace.steps.append(StepTrace(node_id=rn, type=node_map[rn].type, layer=later_idx, status=status))

    for layer_idx, layer_node_ids in enumerate(layers):
        tasks = []
        for nid in layer_node_ids:
            node = node_map[nid]
            node_inputs: dict[str, Any] = {
                pred_id: results[pred_id] for pred_id in predecessors.get(nid, []) if pred_id in results
            }
            node_inputs.update(scan_inputs)
            if node.type == "llm":
                tasks.append((nid, _execute_llm_node(node.config, node_inputs, message_context, category_ids)))
            elif node.type == "script":
                tasks.append((nid, _execute_script_node(node.config, node_inputs, message_context, target_text)))
            else:
                step = StepTrace(node_id=nid, type=node.type, layer=layer_idx, status="failed",
                                 error=f"Node type '{node.type}' not supported yet")

                async def _return_step(s=step):
                    return s

                tasks.append((nid, _return_step()))

        if not tasks:
            continue
        step_results = await asyncio.gather(*(t for _, t in tasks), return_exceptions=True)

        # Record every step of the layer before deciding to abort, so a failed
        # node's siblings still show up in the trace.
        layer_steps: list[StepTrace] = []
        for (nid, _), step_result in zip(tasks, step_results):
            if isinstance(step_result, BaseException):
                step = StepTrace(node_id=nid, type=node_map[nid].type, layer=layer_idx,
                                 status="failed", error=str(step_result) or type(step_result).__name__)
            else:
                step = step_result
                step.node_id = nid
                step.layer = layer_idx
            trace.steps.append(step)
            layer_steps.append(step)

        failed = next((st for st in layer_steps if st.status == "failed"), None)
        if failed is not None:
            _skip_rest(layer_idx, "skipped")
            return _failed_result(trace, t0, f"Node '{failed.node_id}' failed: {failed.error}")

        for step in layer_steps:
            nid = step.node_id
            node_def = node_map[nid]
            # Store output for downstream nodes (filtered by output_forward/output_trace_only)
            if step.output:
                if node_def.output_forward is not None:
                    results[nid] = {k: v for k, v in step.output.items() if k in node_def.output_forward}
                elif node_def.output_trace_only is not None:
                    results[nid] = {k: v for k, v in step.output.items() if k not in node_def.output_trace_only}
                else:
                    results[nid] = step.output

            node_sc = node_def.short_circuit
            if node_sc and step.output and _evaluate_short_circuit(node_sc.get("condition", ""), step.output):
                sc_result_spec = node_sc.get("result", "output")
                sc_output = dict(sc_result_spec) if isinstance(sc_result_spec, dict) else step.output
                trace.short_circuit = {
                    "node_id": nid,
                    "condition": node_sc.get("condition"),
                    "evaluated_output": step.output,
                    "result": sc_output,
                }
                _skip_rest(layer_idx, "skipped_short_circuit")
                _finish(trace, t0, "short_circuited")
                return EngineResult(
                    scam_result=ScamCheckResult(
                        is_scam=bool(sc_output.get("is_scam", False)),
                        confidence=_clamp01(sc_output.get("confidence", 0.0)),
                        reason=str(sc_output.get("reason", "")),
                        category=sc_output.get("category"),
                        trace_json=trace.to_json(),
                    ),
                    trace=trace,
                )

    # --- Step 4: Extract result from output node ---
    output = results.get(output_node_id)
    if not output:
        return _failed_result(trace, t0, f"Output node '{output_node_id}' produced no output")
    _finish(trace, t0, "completed")
    return EngineResult(
        scam_result=ScamCheckResult(
            is_scam=bool(output.get("is_scam", False)),
            confidence=_clamp01(output.get("confidence", 0.0)),
            reason=str(output.get("reason", "")),
            category=output.get("category"),
            raw_response=output,
            trace_json=trace.to_json(),
        ),
        trace=trace,
    )


def _clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
