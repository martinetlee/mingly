"""Tool registry for LLM tool use in the scam detection pipeline.

Two layers:
  - Scripts: generic reusable functions (ship with app, updated via app release)
  - Tools: named configurations of scripts (defined in pipeline JSON, synced from cloud)

The pipeline JSON can create new tools by referencing existing scripts with
different parameters. This lets the cloud add new tools without an app update.
"""

import logging
from dataclasses import dataclass
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict  # JSON Schema for LLM function calling
    execute: Callable[..., Awaitable[dict]]


# Script registry — generic functions that ship with the app
SCRIPT_REGISTRY: dict[str, Callable[..., Awaitable[dict]]] = {}

# Tool registry — named tool instances (scripts + config)
TOOL_REGISTRY: dict[str, ToolDef] = {}


def register_script(name: str):
    """Decorator to register a generic reusable script function."""
    def decorator(fn):
        SCRIPT_REGISTRY[name] = fn
        return fn
    return decorator


def register_tool(name: str, description: str, parameters: dict):
    """Decorator to register a tool directly (for built-in tools like ml_prefilter)."""
    def decorator(fn):
        TOOL_REGISTRY[name] = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            execute=fn,
        )
        return fn
    return decorator


def register_tool_from_config(tool_config: dict) -> Optional[ToolDef]:
    """Register a tool from pipeline JSON config.

    tool_config: {
        "name": "search_scam_scenarios",
        "script": "search_library",
        "description": "...",
        "parameters": {...},
        "script_params": {"filename": "scenarios.json"}
    }
    """
    name = tool_config.get("name", "")
    script_name = tool_config.get("script", "")
    description = tool_config.get("description", "")
    parameters = tool_config.get("parameters", {})
    script_params = tool_config.get("script_params", {})

    script_fn = SCRIPT_REGISTRY.get(script_name)
    if not script_fn:
        logger.warning("Tool '%s' references unknown script '%s'", name, script_name)
        return None

    # Create a wrapper that injects script_params into every call
    async def _execute(args: dict) -> dict:
        merged = {**script_params, **args}
        return await script_fn(merged)

    tool = ToolDef(
        name=name,
        description=description,
        parameters=parameters,
        execute=_execute,
    )
    TOOL_REGISTRY[name] = tool
    logger.info("Registered tool '%s' (script: %s, params: %s)", name, script_name, list(script_params.keys()))
    return tool


def register_tools_from_pipeline(pipeline_data: dict):
    """Register all tools defined in a pipeline JSON. Called on pipeline load/reload."""
    tools = pipeline_data.get("tools", [])
    for tool_config in tools:
        register_tool_from_config(tool_config)


def get_tool(name: str) -> Optional[ToolDef]:
    return TOOL_REGISTRY.get(name)
