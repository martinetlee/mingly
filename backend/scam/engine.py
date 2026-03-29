"""DAG-based pipeline execution engine for scam detection.

Executes a directed acyclic graph of detection steps (LLM calls, scripts,
classifiers). Phase 1 supports LLM nodes only. The engine handles validation,
topological sorting, parallel layer execution, and full trace recording.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from backend.llm.base import ChatMessage, LLMError
from backend.llm.registry import get_registry
from backend.scam.client import ScamCheckResult

logger = logging.getLogger(__name__)


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
    model_warning: Optional[str] = None
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
    """Parse LLM response text into a dict. Handles code fences and extra text."""
    content = raw.strip()
    if not content:
        raise ValueError("LLM returned empty response")

    # Strip markdown code fences
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    # Extract JSON object from potentially wrapped text
    json_start = content.find("{")
    json_end = content.rfind("}") + 1
    if json_start >= 0 and json_end > json_start:
        content = content[json_start:json_end]

    # Fix common LLM JSON mistakes: Python-style True/False/None → JSON true/false/null
    content = content.replace(": True", ": true").replace(": False", ": false").replace(": None", ": null")

    return json.loads(content)


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

    registry = get_registry()
    provider = None
    if model_requested:
        provider = registry.get_by_name(model_requested) if hasattr(registry, 'get_by_name') else None
        if not provider:
            provider = registry.get_any()
            if provider:
                trace.model_warning = f"Model '{model_requested}' not available, using '{provider.instance_name}'"
    else:
        provider = registry.get_any()

    if not provider:
        trace.status = "failed"
        trace.error = "No LLM provider available"
        trace.duration_ms = int((time.monotonic() - t0) * 1000)
        trace.completed_at = datetime.now(timezone.utc).isoformat()
        return trace

    trace.model_used = provider.instance_name

    # Build prompt from template
    prompt_template = node_config.get("prompt", "")
    sub_inputs = {**inputs, "$message": message_context}
    prompt = substitute_placeholders(prompt_template, sub_inputs)

    # Build LLM messages
    max_tokens = node_config.get("max_tokens", 200)
    temperature = node_config.get("temperature", 0.1)

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

    try:
        response = None
        tool_calls_made = []

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
                break  # Final answer

            # Execute tool calls
            for tc in pending_calls:
                tool = get_tool(tc.name)
                if not tool:
                    logger.warning("Tool '%s' not found in registry", tc.name)
                    continue

                tc_t0 = time.monotonic()
                try:
                    result = await tool.execute(tc.arguments)
                except Exception as e:
                    result = {"error": str(e)}
                tc_ms = int((time.monotonic() - tc_t0) * 1000)

                tool_calls_made.append({
                    "iteration": iteration,
                    "tool": tc.name,
                    "args": tc.arguments,
                    "result": result,
                    "duration_ms": tc_ms,
                })

                # Append to message history for next iteration
                if use_native:
                    # Native: assistant message with tool_calls + tool result
                    llm_messages.append(ChatMessage(
                        role="assistant", content=response.content or "",
                        tool_calls=[{"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}],
                    ))
                    llm_messages.append(ChatMessage(
                        role="tool", content=json.dumps(result, default=str),
                        tool_call_id=tc.id,
                    ))
                else:
                    # Text-based: append as user message
                    llm_messages.append(ChatMessage(role="assistant", content=response.content or ""))
                    llm_messages.append(ChatMessage(
                        role="user",
                        content=f"Tool result for {tc.name}:\n{json.dumps(result, indent=2, default=str)}\n\nNow provide your final JSON answer.",
                    ))

        trace.tool_calls = tool_calls_made

        # Parse final response
        raw_content = (response.content if response else "") or ""

        # If content is empty after tool use, make one more call without tools to force a final answer
        if not raw_content.strip() and tool_calls_made:
            llm_messages.append(ChatMessage(role="user", content="Now provide your final JSON answer based on the tool results above."))
            try:
                final_response = await provider.complete(llm_messages, max_tokens=max_tokens, temperature=temperature)
                raw_content = final_response.content or ""
            except Exception:
                pass

        trace.raw_response = raw_content

        parsed = parse_llm_response(raw_content)

        # Validate category
        category = parsed.get("category")
        if category and categories and category not in categories:
            category = "other"
            parsed["category"] = "other"

        trace.output = parsed
        trace.status = "completed"

    except json.JSONDecodeError as e:
        trace.status = "failed"
        trace.error = f"LLM returned invalid JSON: {e}"
        if response:
            trace.raw_response = response.content
    except (LLMError, Exception) as e:
        trace.status = "failed"
        trace.error = str(e)

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
) -> StepTrace:
    """Execute a script node by calling a registered tool/script function."""
    trace = StepTrace(node_id="", type="script", layer=0)
    t0 = time.monotonic()
    trace.started_at = datetime.now(timezone.utc).isoformat()

    script_name = node_config.get("script", "")
    params = node_config.get("params", {})
    trace.script = script_name

    import backend.scam.tools  # ensure tools are registered
    from backend.scam.tools import get_tool
    tool = get_tool(script_name)
    if not tool:
        trace.status = "failed"
        trace.error = f"Script '{script_name}' not found in registry — app update may be required"
        trace.duration_ms = int((time.monotonic() - t0) * 1000)
        trace.completed_at = datetime.now(timezone.utc).isoformat()
        return trace

    # Build args: merge params with predecessor inputs + $message
    args = {**params, **inputs, "$message": message_context}
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

async def execute_pipeline(pipeline_config, message_context: str, messages: list[dict]) -> EngineResult:
    """Execute a pipeline DAG and return the result with full trace.

    Args:
        pipeline_config: PipelineConfig with nodes, edges, output, signatures, categories
        message_context: Formatted message context string (chat info + profiles + messages)
        messages: Raw message dicts (for signature checking)
    """
    from backend.scam.client import _extract_domains

    trace = PipelineTrace(
        pipeline_version=pipeline_config.version,
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    # --- Step 1: Signature check ---
    sigs = pipeline_config.signatures
    if sigs.domains or sigs.wallets or sigs._compiled_patterns or sigs.telegram_ids:
        for msg in messages:
            text = msg.get("text") or ""
            sender_id = msg.get("sender_id")

            if sender_id and sender_id in set(sigs.telegram_ids):
                trace.status = "short_circuited"
                trace.short_circuit = {"match_type": "telegram_id", "matched_value": str(sender_id), "confidence": 0.95, "category": "impersonation"}
                trace.completed_at = datetime.now(timezone.utc).isoformat()
                trace.duration_ms = 0
                return EngineResult(
                    scam_result=ScamCheckResult(is_scam=True, confidence=0.95, reason=f"Sender matches known scammer ID", category="impersonation", trace_json=trace.to_json()),
                    trace=trace,
                )

            if text:
                if sigs.domains:
                    for domain in _extract_domains(text):
                        if domain in sigs.domains:
                            trace.status = "short_circuited"
                            trace.short_circuit = {"match_type": "domain", "matched_value": domain, "confidence": 0.95, "category": "malware_link"}
                            trace.completed_at = datetime.now(timezone.utc).isoformat()
                            return EngineResult(
                                scam_result=ScamCheckResult(is_scam=True, confidence=0.95, reason=f"Known malicious domain: {domain}", category="malware_link", trace_json=trace.to_json()),
                                trace=trace,
                            )

                for wallet in sigs.wallets:
                    if wallet.lower() in text.lower():
                        trace.status = "short_circuited"
                        trace.short_circuit = {"match_type": "wallet", "matched_value": wallet, "confidence": 0.95, "category": "investment_scam"}
                        trace.completed_at = datetime.now(timezone.utc).isoformat()
                        return EngineResult(
                            scam_result=ScamCheckResult(is_scam=True, confidence=0.95, reason="Known scam wallet address", category="investment_scam", trace_json=trace.to_json()),
                            trace=trace,
                        )

                for pattern in sigs._compiled_patterns:
                    if pattern.search(text):
                        trace.status = "short_circuited"
                        trace.short_circuit = {"match_type": "pattern", "matched_value": pattern.pattern, "confidence": 0.85, "category": "other"}
                        trace.completed_at = datetime.now(timezone.utc).isoformat()
                        return EngineResult(
                            scam_result=ScamCheckResult(is_scam=True, confidence=0.85, reason=f"Matches scam pattern", category="other", trace_json=trace.to_json()),
                            trace=trace,
                        )

    # --- Step 2: Validate & compute layers ---
    nodes = pipeline_config.nodes
    edges = pipeline_config.edges
    output_node_id = pipeline_config.output

    if not nodes:
        trace.status = "failed"
        trace.error = "Pipeline has no nodes"
        trace.completed_at = datetime.now(timezone.utc).isoformat()
        return EngineResult(
            scam_result=ScamCheckResult(is_scam=False, confidence=0.0, reason="", error="Pipeline has no nodes", trace_json=trace.to_json()),
            trace=trace,
        )

    try:
        predecessors, layers = validate_pipeline(nodes, edges, output_node_id)
    except ValueError as e:
        trace.status = "failed"
        trace.error = str(e)
        trace.completed_at = datetime.now(timezone.utc).isoformat()
        return EngineResult(
            scam_result=ScamCheckResult(is_scam=False, confidence=0.0, reason="", error=f"Pipeline validation failed: {e}", trace_json=trace.to_json()),
            trace=trace,
        )

    # --- Step 3: Execute layers ---
    node_map = {n.id: n for n in nodes}
    results: dict[str, dict] = {}
    category_ids = pipeline_config.get_category_ids()

    for layer_idx, layer_node_ids in enumerate(layers):
        # Build tasks for this layer
        tasks = []
        for nid in layer_node_ids:
            node = node_map[nid]

            # Assemble inputs from predecessors
            node_inputs: dict[str, Any] = {}
            for pred_id in predecessors.get(nid, []):
                if pred_id in results:
                    node_inputs[pred_id] = results[pred_id]

            if node.type == "llm":
                task = _execute_llm_node(node.config, node_inputs, message_context, category_ids)
                tasks.append((nid, task))
            elif node.type == "script":
                task = _execute_script_node(node.config, node_inputs, message_context)
                tasks.append((nid, task))
            else:
                step = StepTrace(node_id=nid, type=node.type, layer=layer_idx, status="failed", error=f"Node type '{node.type}' not supported yet")

                async def _return_step(s=step):
                    return s

                tasks.append((nid, _return_step()))

        # Execute all tasks in this layer in parallel
        if tasks:
            coros = [t for _, t in tasks]
            step_results = await asyncio.gather(*coros, return_exceptions=True)

            for (nid, _), step_result in zip(tasks, step_results):
                if isinstance(step_result, Exception):
                    step = StepTrace(node_id=nid, type=node_map[nid].type, layer=layer_idx, status="failed", error=str(step_result))
                    trace.steps.append(step)
                    # Abort pipeline
                    trace.status = "failed"
                    trace.error = f"Node '{nid}' failed: {step_result}"
                    # Mark remaining nodes as skipped
                    for remaining_layer in layers[layer_idx + 1:]:
                        for remaining_nid in remaining_layer:
                            trace.steps.append(StepTrace(node_id=remaining_nid, type=node_map[remaining_nid].type, layer=layers.index(remaining_layer) if remaining_layer in layers else 0, status="skipped"))
                    trace.completed_at = datetime.now(timezone.utc).isoformat()
                    trace.duration_ms = int((time.monotonic()) * 1000)  # approximate
                    return EngineResult(
                        scam_result=ScamCheckResult(is_scam=False, confidence=0.0, reason="", error=trace.error, trace_json=trace.to_json()),
                        trace=trace,
                    )

                step = step_result
                step.node_id = nid
                step.layer = layer_idx
                trace.steps.append(step)

                if step.status == "failed":
                    # Abort pipeline
                    trace.status = "failed"
                    trace.error = f"Node '{nid}' failed: {step.error}"
                    for remaining_layer in layers[layer_idx + 1:]:
                        for remaining_nid in remaining_layer:
                            trace.steps.append(StepTrace(node_id=remaining_nid, type=node_map[remaining_nid].type, layer=0, status="skipped"))
                    trace.completed_at = datetime.now(timezone.utc).isoformat()
                    return EngineResult(
                        scam_result=ScamCheckResult(is_scam=False, confidence=0.0, reason="", error=trace.error, trace_json=trace.to_json()),
                        trace=trace,
                    )

                # Store output for downstream nodes
                if step.output:
                    results[nid] = step.output

                # TODO Phase 2: check short_circuit condition here

    # --- Step 4: Extract result from output node ---
    trace.status = "completed"
    trace.completed_at = datetime.now(timezone.utc).isoformat()
    t_start = datetime.fromisoformat(trace.started_at)
    t_end = datetime.fromisoformat(trace.completed_at)
    trace.duration_ms = int((t_end - t_start).total_seconds() * 1000)

    output = results.get(output_node_id, {})
    is_scam = bool(output.get("is_scam", False))
    confidence = max(0.0, min(1.0, float(output.get("confidence", 0.0))))
    category = output.get("category")
    reason = str(output.get("reason", ""))

    return EngineResult(
        scam_result=ScamCheckResult(
            is_scam=is_scam,
            confidence=confidence,
            reason=reason,
            category=category,
            raw_response=output,
            trace_json=trace.to_json(),
        ),
        trace=trace,
    )
