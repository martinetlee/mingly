"""Tests for the MCP endpoint: gating, auth, protocol handshake, and tools."""

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.config import API_PORT
from backend.api import mcp as mcp_module
from tests import factories

BASE_URL = f"http://127.0.0.1:{API_PORT}"
KEY = "test-mcp-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture(autouse=True)
def _clean_sessions():
    mcp_module.clear_mcp_sessions()
    yield
    mcp_module.clear_mcp_sessions()


@pytest.fixture(autouse=True)
def _mcp_available():
    """Default state for tests: feature enabled, vault unlocked, key set."""
    with patch("backend.api.mcp.get_mcp_enabled", return_value=True), \
         patch("backend.api.mcp.get_secret", return_value=KEY):
        yield


@pytest.fixture
def app(db_engine):
    with patch("backend.api.auth.get_client", return_value=None), \
         patch("backend.api.auth.has_credentials", return_value=False):
        from backend.api.router import create_app
        return create_app()


def _client(app, base_url=BASE_URL):
    return AsyncClient(transport=ASGITransport(app=app), base_url=base_url)


def _rpc(method, req_id=1, params=None):
    body = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        body["id"] = req_id
    if params is not None:
        body["params"] = params
    return body


async def _initialize(client, version="2025-06-18"):
    resp = await client.post(
        "/mcp",
        headers=AUTH,
        json=_rpc("initialize", params={
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        }),
    )
    return resp


# ── Availability gating ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disabled_returns_404(app):
    with patch("backend.api.mcp.get_mcp_enabled", return_value=False):
        async with _client(app) as client:
            resp = await client.post("/mcp", headers=AUTH, json=_rpc("ping"))
            assert resp.status_code == 404


@pytest.mark.asyncio
async def test_vault_locked_returns_404(app):
    with patch("backend.api.mcp.get_secret", return_value=None):
        async with _client(app) as client:
            resp = await client.post("/mcp", headers=AUTH, json=_rpc("ping"))
            assert resp.status_code == 404


@pytest.mark.asyncio
async def test_missing_bearer_returns_401(app):
    async with _client(app) as client:
        resp = await client.post("/mcp", json=_rpc("ping"))
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wrong_bearer_returns_401(app):
    async with _client(app) as client:
        resp = await client.post(
            "/mcp", headers={"Authorization": "Bearer nope"}, json=_rpc("ping")
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wrong_host_returns_403(app):
    async with _client(app, base_url="http://evil.example.com") as client:
        resp = await client.post("/mcp", headers=AUTH, json=_rpc("ping"))
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bad_origin_returns_403(app):
    async with _client(app) as client:
        resp = await client.post(
            "/mcp",
            headers={**AUTH, "Origin": "http://evil.example.com"},
            json=_rpc("ping"),
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_returns_405_despite_spa_catchall(app):
    async with _client(app) as client:
        resp = await client.get("/mcp")
        assert resp.status_code == 405
        assert "POST" in resp.headers.get("allow", "")


# ── Protocol handshake ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_initialize_negotiates_supported_version(app):
    async with _client(app) as client:
        resp = await _initialize(client, version="2025-03-26")
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"]["protocolVersion"] == "2025-03-26"
        assert data["result"]["serverInfo"]["name"] == "mingly"
        assert resp.headers.get("mcp-session-id")


@pytest.mark.asyncio
async def test_initialize_falls_back_on_unsupported_version(app):
    async with _client(app) as client:
        resp = await _initialize(client, version="1999-01-01")
        assert resp.json()["result"]["protocolVersion"] == "2025-06-18"


@pytest.mark.asyncio
async def test_request_without_session_returns_400(app):
    async with _client(app) as client:
        resp = await client.post("/mcp", headers=AUTH, json=_rpc("ping"))
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_request_with_unknown_session_returns_404(app):
    async with _client(app) as client:
        resp = await client.post(
            "/mcp", headers={**AUTH, "Mcp-Session-Id": "bogus"}, json=_rpc("ping")
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_initialized_notification_returns_202(app):
    async with _client(app) as client:
        sid = (await _initialize(client)).headers["mcp-session-id"]
        resp = await client.post(
            "/mcp",
            headers={**AUTH, "Mcp-Session-Id": sid},
            json=_rpc("notifications/initialized", req_id=None),
        )
        assert resp.status_code == 202


@pytest.mark.asyncio
async def test_delete_terminates_session(app):
    async with _client(app) as client:
        sid = (await _initialize(client)).headers["mcp-session-id"]
        headers = {**AUTH, "Mcp-Session-Id": sid}
        resp = await client.request("DELETE", "/mcp", headers=headers)
        assert resp.status_code == 200
        # Session is gone now
        resp = await client.post("/mcp", headers=headers, json=_rpc("ping"))
        assert resp.status_code == 404


# ── JSON-RPC errors ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_parse_error(app):
    async with _client(app) as client:
        resp = await client.post(
            "/mcp", headers={**AUTH, "Content-Type": "application/json"}, content=b"{nope"
        )
        assert resp.json()["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_batch_rejected(app):
    async with _client(app) as client:
        resp = await client.post("/mcp", headers=AUTH, json=[_rpc("ping")])
        assert resp.json()["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_unknown_method(app):
    async with _client(app) as client:
        sid = (await _initialize(client)).headers["mcp-session-id"]
        resp = await client.post(
            "/mcp", headers={**AUTH, "Mcp-Session-Id": sid}, json=_rpc("resources/list")
        )
        assert resp.json()["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_unknown_tool_returns_invalid_params(app):
    async with _client(app) as client:
        sid = (await _initialize(client)).headers["mcp-session-id"]
        resp = await client.post(
            "/mcp",
            headers={**AUTH, "Mcp-Session-Id": sid},
            json=_rpc("tools/call", params={"name": "send_message", "arguments": {}}),
        )
        assert resp.json()["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_missing_required_arg_returns_invalid_params(app):
    async with _client(app) as client:
        sid = (await _initialize(client)).headers["mcp-session-id"]
        resp = await client.post(
            "/mcp",
            headers={**AUTH, "Mcp-Session-Id": sid},
            json=_rpc("tools/call", params={"name": "get_messages", "arguments": {}}),
        )
        assert resp.json()["error"]["code"] == -32602


# ── Tools ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tools_list(app):
    async with _client(app) as client:
        sid = (await _initialize(client)).headers["mcp-session-id"]
        resp = await client.post(
            "/mcp", headers={**AUTH, "Mcp-Session-Id": sid}, json=_rpc("tools/list")
        )
        tools = resp.json()["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == {
            "get_archive_status", "list_dms", "list_conversations", "search_conversations",
            "get_conversation", "get_messages", "search_messages", "list_tags", "get_conversations_by_tag",
            "search_people", "get_person", "fetch_history", "list_organizations",
            # write tools (gated by the mcp_write_enabled pref)
            "add_person_note", "update_person_note", "set_person_fields",
            "add_position", "tag_conversation", "mark_reviewed",
        }
        assert all("inputSchema" in t and "description" in t and "annotations" in t for t in tools)
        by_name = {t["name"]: t for t in tools}
        assert by_name["get_messages"]["annotations"]["readOnlyHint"] is True
        assert by_name["add_person_note"]["annotations"]["readOnlyHint"] is False
        assert by_name["fetch_history"]["annotations"]["openWorldHint"] is True


@pytest.mark.asyncio
async def test_list_conversations_happy_path(app, db):
    await factories.conversation(db, 111, title="Test Group", kind="group")
    await factories.conversation(db, "t-1", title="Mail Thread", kind="email_thread", channel="gmail")
    async with _client(app) as client:
        sid = (await _initialize(client)).headers["mcp-session-id"]
        resp = await client.post(
            "/mcp",
            headers={**AUTH, "Mcp-Session-Id": sid},
            json=_rpc("tools/call", params={"name": "list_conversations", "arguments": {"limit": 5}}),
        )
        result = resp.json()["result"]
        assert result["isError"] is False
        text = result["content"][0]["text"]
        assert "Test Group" in text and "Mail Thread" in text
        assert "scam_llm" not in text
        assert "scam_trace" not in text
        resp = await client.post(
            "/mcp",
            headers={**AUTH, "Mcp-Session-Id": sid},
            json=_rpc("tools/call", params={
                "name": "list_conversations", "arguments": {"channel": "telegram"},
            }),
        )
        text = resp.json()["result"]["content"][0]["text"]
        assert "Test Group" in text and "Mail Thread" not in text


@pytest.mark.asyncio
async def test_get_conversation_deactivated_is_not_found(app, db):
    dead = await factories.conversation(db, 111, title="Old Group", kind="group", is_deactivated=True)
    async with _client(app) as client:
        sid = (await _initialize(client)).headers["mcp-session-id"]
        resp = await client.post(
            "/mcp",
            headers={**AUTH, "Mcp-Session-Id": sid},
            json=_rpc("tools/call", params={
                "name": "get_conversation", "arguments": {"conversation_id": dead.id},
            }),
        )
        result = resp.json()["result"]
        assert result["isError"] is True
        assert "not found" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_get_conversation_hidden_is_visible_and_flagged(app, db):
    """Hidden only declutters the app's inbox; an agent covering every DM
    must still see the conversation, flagged."""
    import json as _json

    conv = await factories.conversation(db, 111, title="Hidden Friend", is_hidden=True)
    async with _client(app) as client:
        sid = (await _initialize(client)).headers["mcp-session-id"]
        resp = await client.post(
            "/mcp",
            headers={**AUTH, "Mcp-Session-Id": sid},
            json=_rpc("tools/call", params={
                "name": "get_conversation", "arguments": {"conversation_id": conv.id},
            }),
        )
        result = resp.json()["result"]
        assert result["isError"] is False
        data = _json.loads(result["content"][0]["text"])
        assert data["conversation_id"] == conv.id and data["channel"] == "telegram"
        assert data["is_hidden"] is True
        assert data["peer"]["person_id"] == conv.peer_identity.person_id
        assert data["coverage"]["history_complete"] is False
