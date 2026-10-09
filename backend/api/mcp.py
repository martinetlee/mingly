"""
Hand-rolled MCP (Model Context Protocol) streamable-HTTP endpoint.

Implements the JSON-RPC 2.0 subset generic MCP clients need over plain HTTP
POST (no SSE): initialize, notifications/*, ping, tools/list, tools/call,
prompts/list, prompts/get.
The official `mcp` SDK needs Python >= 3.10, so this is dependency-free.

Security model (see also backend/api/settings.py MCP section):
- Loopback only (served by the app's uvicorn on 127.0.0.1).
- Bearer key stored encrypted in the Fernet vault; while the vault is locked
  or the feature is disabled the endpoint answers a bare 404.
- Host/Origin validation against loopback to block DNS-rebinding.
- Read tools query the local DB (fetch_history also asks the archive to pull a
  conversation's history from Telegram). Write tools (notes, person fields, tags,
  review marks) only run while the user enables them (pref
  mcp_write_enabled, default off); nothing ever sends messages.
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
from backend import secrets as vault
from backend.api.dependencies import _is_allowed_origin, is_allowed_host
from backend.api.mcp_tools import TOOLS, ToolError, tool_descriptors, validate_args
from backend.api.settings import get_mcp_enabled, get_mcp_write_enabled
from backend.db.database import get_db
from backend.secrets import get_secret

logger = logging.getLogger(__name__)

router = APIRouter()

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
LATEST_PROTOCOL_VERSION = "2025-06-18"

SERVER_INFO = {"name": "mingly", "version": "1.0"}
KNOWN_LIMITS = (
    "Known limits: a DM whose history was deleted for both sides, or whose dialog was "
    "deleted, no longer exists on Telegram and cannot be recovered; secret chats are not "
    "available through the API; messages the other party deleted are gone; bots are "
    "excluded from list_dms unless include_bots=true; Saved Messages is not a DM."
)

REVIEW_PROCEDURE = (
    "1. get_archive_status. If telegram_connected is false, stop and tell the user to open and "
    "unlock Mingly (archiving resumes automatically; stored messages stay readable, but no "
    "review can be completed for DMs whose history is incomplete). If DMs are still archiving, "
    "wait (or call fetch_history on the DMs you need) — list_dms(filter='incomplete') shows which "
    "histories are not complete.\n"
    "2. list_dms(filter='unreviewed'), following next_cursor until it is null.\n"
    "3. For each DM: if history_complete is false, fetch_history(conversation_id) until it is (if it "
    "reports telegram_connected: false, stop retrying and tell the user). Then read "
    "get_messages(conversation_id, order='asc') from the start (or after_message_id = "
    "review.reviewed_through_message_id when re-reviewing), following next_cursor to the end.\n"
    "4. Record what you learned about the DM's person (person.person_id) with add_person_note "
    "(kind: context, due_diligence, risk, relationship or todo), citing the messages "
    "({conversation_id, message_id}) each claim rests on; use get_person first to avoid repeating "
    "existing notes (one person may have DMs on several channels). Optionally set_person_fields / "
    "add_position / tag_conversation.\n"
    "5. mark_reviewed(conversation_id, through_message_id = the newest message_id you read; omit it "
    "for an empty conversation).\n"
    "6. Repeat from step 2 until list_dms(filter='unreviewed') returns no DMs. New messages "
    "put a DM back in that queue, so nothing is silently skipped."
)

SERVER_INSTRUCTIONS = (
    "Access to Mingly's local message archive (Telegram and any other connected channel): list and "
    "search conversations and DMs, read full message histories (including service events), look up "
    "people (one person merges their identities on every channel) and organizations, and ask the "
    "archive to fetch missing Telegram history. Ids are Mingly's own conversation_id / message_id / "
    "person_id. Write tools (add_person_note, update_person_note, set_person_fields, add_position, "
    "tag_conversation, mark_reviewed) work only while the user allows agent writes in Mingly "
    "Settings › MCP. No tool sends messages. Paged tools return next_cursor; pass it back unchanged.\n\n"
    "To review everyone the user has DMed (the review_dms prompt has the same steps):\n"
    + REVIEW_PROCEDURE + "\n\n" + KNOWN_LIMITS
)

PROMPTS = {
    "review_dms": {
        "description": "Review every DM and record context / due diligence about each person, missing no one.",
        "arguments": [
            {"name": "focus", "description": "Optional emphasis, e.g. 'investors' or 'scam risk'", "required": False},
        ],
    },
}

# In-memory MCP sessions: creation time and the client's self-reported name
# (used as the default agent_label on writes).
_sessions: "dict[str, dict]" = {}
_MAX_SESSIONS = 64


def _new_session(client_name: Optional[str] = None) -> str:
    sid = pysecrets.token_urlsafe(16)
    if len(_sessions) >= _MAX_SESSIONS:
        oldest = min(_sessions, key=lambda k: _sessions[k]["created"])
        _sessions.pop(oldest, None)
    _sessions[sid] = {"created": time.monotonic(), "client_name": client_name}
    return sid


def clear_mcp_sessions() -> None:
    """Called on vault lock as hygiene; auth already dies with the vault."""
    _sessions.clear()


vault.add_lock_listener(clear_mcp_sessions)


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
    # The app-wide middleware already requires a loopback Host; MCP clients
    # connect directly, so also pin the exact port.
    if not is_allowed_host(request.headers.get("host"), port=API_PORT):
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
    params = params if isinstance(params, dict) else {}
    requested = params.get("protocolVersion")
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
    client_info = params.get("clientInfo")
    client_name = client_info.get("name") if isinstance(client_info, dict) else None
    sid = _new_session(str(client_name)[:100] if client_name else None)
    return _rpc_result(
        req_id,
        {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": SERVER_INSTRUCTIONS,
        },
        headers={"Mcp-Session-Id": sid},
    )


WRITE_DISABLED_MESSAGE = (
    "Write tools are turned off. The user can allow them in Mingly Settings › MCP "
    "(\"Allow agent to write\"). Read tools keep working."
)


async def _handle_tools_call(req_id, params: dict, db: AsyncSession, ctx: dict) -> JSONResponse:
    params = params if isinstance(params, dict) else {}
    name = params.get("name")
    args = params.get("arguments") or {}
    if name not in TOOLS:
        return _rpc_error(req_id, -32602, f"Unknown tool: {name}")
    if not isinstance(args, dict):
        return _rpc_error(req_id, -32602, "arguments must be an object")
    validation_error = validate_args(name, args)
    if validation_error:
        return _rpc_error(req_id, -32602, validation_error)
    if TOOLS[name]["access"] != "read" and not get_mcp_write_enabled():
        return _rpc_result(req_id, _tool_text_result(WRITE_DISABLED_MESSAGE, is_error=True))
    try:
        payload = await TOOLS[name]["handler"](db, args, ctx)
    except ToolError as exc:
        return _rpc_result(req_id, _tool_text_result(str(exc), is_error=True))
    except Exception:
        logger.exception("MCP tool %s failed", name)
        return _rpc_result(req_id, _tool_text_result("Internal error executing tool", is_error=True))
    return _rpc_result(req_id, _tool_text_result(payload))


def _review_prompt_text(focus: Optional[str]) -> str:
    text = (
        "Review everyone I have DMed on Telegram using the Mingly tools and record context and "
        "due diligence about each person. Nobody and no message may be skipped.\n\n"
        + REVIEW_PROCEDURE + "\n\n" + KNOWN_LIMITS
    )
    if focus:
        text += f"\n\nPay particular attention to: {focus}"
    return text


def _handle_prompts_get(req_id, params: dict) -> JSONResponse:
    params = params if isinstance(params, dict) else {}
    name = params.get("name")
    if name not in PROMPTS:
        return _rpc_error(req_id, -32602, f"Unknown prompt: {name}")
    arguments = params.get("arguments") or {}
    focus = arguments.get("focus") if isinstance(arguments, dict) else None
    return _rpc_result(req_id, {
        "description": PROMPTS[name]["description"],
        "messages": [{
            "role": "user",
            "content": {"type": "text", "text": _review_prompt_text(str(focus).strip() if focus else None)},
        }],
    })


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
        ctx = {"client_name": _sessions[request.headers["mcp-session-id"]].get("client_name")}
        return await _handle_tools_call(req_id, params, db, ctx)
    if method == "prompts/list":
        return _rpc_result(req_id, {"prompts": [{"name": n, **p} for n, p in PROMPTS.items()]})
    if method == "prompts/get":
        return _handle_prompts_get(req_id, params)

    return _rpc_error(req_id, -32601, f"Method not found: {method}")
