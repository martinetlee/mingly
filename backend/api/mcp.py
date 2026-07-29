"""
Hand-rolled MCP (Model Context Protocol) streamable-HTTP endpoint.

Implements the JSON-RPC 2.0 subset generic MCP clients need over plain HTTP
POST (no SSE): initialize, notifications/*, ping, tools/list, tools/call.
The official `mcp` SDK needs Python >= 3.10, so this is dependency-free.

Security model (see also backend/api/settings.py MCP section):
- Loopback only (served by the app's uvicorn on 127.0.0.1).
- Bearer key stored encrypted in the Fernet vault; while the vault is locked
  or the feature is disabled the endpoint answers a bare 404.
- Host/Origin validation against loopback to block DNS-rebinding.
- Read-only: every tool wraps an existing local DB query.
"""
import json
import logging
import secrets as pysecrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import API_PORT
from backend.api.dependencies import _is_allowed_origin
from backend.api.mcp_tools import TOOLS, ToolError, tool_descriptors, validate_args
from backend.api.settings import get_mcp_enabled
from backend.db.database import get_db
from backend.secrets import get_secret

logger = logging.getLogger(__name__)

router = APIRouter()

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
LATEST_PROTOCOL_VERSION = "2025-06-18"

SERVER_INFO = {"name": "mingly", "version": "1.0"}
SERVER_INSTRUCTIONS = (
    "Read-only access to Mingly's local Telegram archive: list and search "
    "chats, read messages, query chats by tag, and look up contacts and "
    "organizations. All data stays on this machine; no tool sends messages "
    "or modifies anything."
)

# In-memory MCP sessions (existence check only — no per-session state).
_sessions: "dict[str, float]" = {}
_MAX_SESSIONS = 64


def _new_session() -> str:
    sid = pysecrets.token_urlsafe(16)
    if len(_sessions) >= _MAX_SESSIONS:
        oldest = min(_sessions, key=_sessions.get)
        _sessions.pop(oldest, None)
    _sessions[sid] = time.monotonic()
    return sid


def clear_mcp_sessions() -> None:
    """Called on vault lock as hygiene; auth already dies with the vault."""
    _sessions.clear()


# ── JSON-RPC helpers ─────────────────────────────────────────────────────────

def _rpc_error(req_id, code: int, message: str, status: int = 200, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        status_code=status,
        headers=headers,
    )


def _rpc_result(req_id, result: dict, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "result": result},
        headers=headers,
    )


def _tool_text_result(payload: dict, is_error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


# ── Request gating ───────────────────────────────────────────────────────────

def _gate(request: Request) -> Optional[Response]:
    """Host/Origin, availability, and bearer-key checks. None means pass."""
    host = request.headers.get("host", "")
    if host not in (f"127.0.0.1:{API_PORT}", f"localhost:{API_PORT}"):
        return Response(status_code=403)

    origin = request.headers.get("origin")
    if origin is not None and not _is_allowed_origin(origin):
        return Response(status_code=403)

    # While disabled or vault-locked the endpoint does not exist.
    key = get_secret("mcp_api_key")
    if not get_mcp_enabled() or key is None:
        return Response(status_code=404)

    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return Response(status_code=401)
    provided = auth[len("Bearer "):].strip()
    if not pysecrets.compare_digest(provided, key):
        return Response(status_code=401)
    return None


def _check_session(request: Request) -> Optional[Response]:
    sid = request.headers.get("mcp-session-id")
    if not sid:
        return Response(status_code=400)
    if sid not in _sessions:
        return Response(status_code=404)
    return None


# ── Method handlers ──────────────────────────────────────────────────────────

def _handle_initialize(req_id, params: dict) -> JSONResponse:
    requested = (params or {}).get("protocolVersion")
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
    sid = _new_session()
    return _rpc_result(
        req_id,
        {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": SERVER_INSTRUCTIONS,
        },
        headers={"Mcp-Session-Id": sid},
    )


async def _handle_tools_call(req_id, params: dict, db: AsyncSession) -> JSONResponse:
    params = params or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    if name not in TOOLS:
        return _rpc_error(req_id, -32602, f"Unknown tool: {name}")
    if not isinstance(args, dict):
        return _rpc_error(req_id, -32602, "arguments must be an object")
    validation_error = validate_args(name, args)
    if validation_error:
        return _rpc_error(req_id, -32602, validation_error)
    try:
        payload = await TOOLS[name]["handler"](db, args)
    except ToolError as exc:
        return _rpc_result(req_id, _tool_text_result(str(exc), is_error=True))
    except Exception:
        logger.exception("MCP tool %s failed", name)
        return _rpc_result(req_id, _tool_text_result("Internal error executing tool", is_error=True))
    return _rpc_result(req_id, _tool_text_result(payload))


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/mcp")
async def mcp_get():
    # Registered explicitly so the SPA catch-all can't serve index.html here.
    # No SSE support — clients must POST.
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


@router.delete("/mcp")
async def mcp_delete(request: Request):
    blocked = _gate(request)
    if blocked is not None:
        return blocked
    sid = request.headers.get("mcp-session-id")
    if sid and _sessions.pop(sid, None) is not None:
        return JSONResponse({"status": "terminated"})
    return Response(status_code=404)


@router.post("/mcp")
async def mcp_post(request: Request, db: AsyncSession = Depends(get_db)):
    blocked = _gate(request)
    if blocked is not None:
        return blocked

    proto = request.headers.get("mcp-protocol-version")
    if proto is not None and proto not in SUPPORTED_PROTOCOL_VERSIONS:
        return Response(status_code=400)

    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _rpc_error(None, -32700, "Parse error")

    if isinstance(body, list):
        return _rpc_error(None, -32600, "Batching is not supported")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        return _rpc_error(None, -32600, "Invalid request")

    method = body["method"]
    params = body.get("params")
    req_id = body.get("id")

    # Notifications (no id) are accepted with 202 and no body.
    if req_id is None:
        if method == "notifications/initialized" or method.startswith("notifications/"):
            blocked = _check_session(request)
            if blocked is not None:
                return blocked
            return Response(status_code=202)
        return Response(status_code=202)

    if method == "initialize":
        return _handle_initialize(req_id, params)

    blocked = _check_session(request)
    if blocked is not None:
        return blocked

    if method == "ping":
        return _rpc_result(req_id, {})
    if method == "tools/list":
        return _rpc_result(req_id, {"tools": tool_descriptors()})
    if method == "tools/call":
        return await _handle_tools_call(req_id, params, db)

    return _rpc_error(req_id, -32601, f"Method not found: {method}")
