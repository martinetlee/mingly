"""Tool registry for the MCP endpoint (``backend/api/mcp.py``).

Each tool returns a plain dict that the transport serializes into an MCP
text-content result; every tool is tagged ``access`` "read" or "write" and
mcp.py refuses write tools unless the user enabled them. The tools are split
by responsibility: ``conversations``, ``people``, ``archive``, ``writes``;
``common`` holds cursors, coverage / review state and serializations
(visibility and paging rules are documented there); ``registry`` the schemas
and argument validation.
"""

from backend.api.mcp_tools.common import ToolError
from backend.api.mcp_tools.registry import tool_descriptors, TOOLS, validate_args

__all__ = ["TOOLS", "ToolError", "tool_descriptors", "validate_args"]
