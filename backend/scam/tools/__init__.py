"""Tool registry for LLM tool use in scam detection pipeline.

Tools are functions that LLM nodes can call during reasoning.
Separate from pipeline scripts (DAG node executors).
"""

from dataclasses import dataclass
from typing import Callable, Awaitable, Optional

@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict  # JSON Schema
    execute: Callable[..., Awaitable[dict]]


TOOL_REGISTRY: dict[str, ToolDef] = {}


def register_tool(name: str, description: str, parameters: dict):
    """Decorator to register a tool for LLM use."""
    def decorator(fn):
        TOOL_REGISTRY[name] = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            execute=fn,
        )
        return fn
    return decorator


def get_tool(name: str) -> Optional[ToolDef]:
    return TOOL_REGISTRY.get(name)


# Explicitly import tool modules to trigger registration
# (pkgutil.iter_modules doesn't work in PyInstaller bundles)
from backend.scam.tools import search_scenarios  # noqa: F401, E402
