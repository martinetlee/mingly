"""Tools the scam detection pipeline's LLM nodes can call.

The registry lives in ``registry``; importing this package also imports the
built-in script modules so they register themselves.
"""

import logging

from backend.scam.tools.registry import (
    SCRIPT_REGISTRY,
    TOOL_REGISTRY,
    ToolDef,
    get_tool,
    register_script,
    register_tool,
    register_tool_from_config,
    register_tools_from_pipeline,
)

__all__ = [
    "SCRIPT_REGISTRY", "TOOL_REGISTRY", "ToolDef", "get_tool", "register_script", "register_tool",
    "register_tool_from_config", "register_tools_from_pipeline",
]

logger = logging.getLogger(__name__)

# Explicitly import script modules to trigger registration (static imports so
# PyInstaller bundles them)
from backend.scam.tools import search_library as _search_library  # noqa: E402

_SCRIPT_MODULES = [_search_library]
try:
    from backend.scam.tools import ml_prefilter as _ml_prefilter  # noqa: E402

    _SCRIPT_MODULES.append(_ml_prefilter)
except ImportError:
    logger.warning("ML prefilter script not available — missing dependencies (joblib/scikit-learn/lightgbm)")
