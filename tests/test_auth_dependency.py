"""Tests for application-layer auth and origin checks."""

from unittest.mock import patch, AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.api.dependencies import (
    generate_session_token,
    get_session_token,
    clear_session_token,
    validate_ws_origin,
)


@pytest.fixture(autouse=True)
def _clean_token():
    """Ensure each test starts with no session token."""
    clear_session_token()
    yield
    clear_session_token()


# ── Unit tests for dependency helpers ────────────────────────────────────────

def test_generate_session_token():
    token = generate_session_token()
    assert token is not None
    assert len(token) > 20
    assert get_session_token() == token


def test_clear_session_token():
    token = generate_session_token()
    clear_session_token()
    assert get_session_token() is None


def test_generate_new_token_replaces_old():
    t1 = generate_session_token()
    t2 = generate_session_token()
    assert t1 != t2
    assert get_session_token() == t2


# ── Integration tests against the FastAPI app ────────────────────────────────

@pytest.fixture
def app():
    # Patch heavy dependencies so the app can be constructed without Telegram
    with patch("backend.api.auth.get_client", return_value=None), \
         patch("backend.api.auth.has_credentials", return_value=False):
        from backend.api.router import create_app
        return create_app()


@pytest.mark.asyncio
async def test_protected_route_rejects_without_token(app):
    """A protected route should return 401 when no session token is active."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/chats")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_rejects_wrong_token(app):
    """A protected route should reject an incorrect token."""
    token = generate_session_token()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/chats",
            headers={"X-Session-Token": "wrong-token"},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_routes_accessible_without_token(app):
    """Auth/vault routes should be accessible without a session token."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/auth/vault-status")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_vault_init_returns_session_token(app):
    """POST /api/auth/vault/init should return a session_token."""
    with patch("backend.api.auth.vault_status", return_value="unset"), \
         patch("backend.api.auth.initialize_vault"):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/auth/vault/init",
                json={"password": "testpassword123"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "session_token" in data
            assert data["session_token"] == get_session_token()


@pytest.mark.asyncio
async def test_vault_unlock_returns_session_token(app):
    """POST /api/auth/vault/unlock should return a session_token."""
    with patch("backend.api.auth.unlock_vault_fn", return_value=True), \
         patch("backend.api.auth.has_credentials", return_value=False):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/auth/vault/unlock",
                json={"password": "testpassword123"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "session_token" in data
            assert data["session_token"] == get_session_token()


@pytest.mark.asyncio
async def test_vault_reset_clears_session_token(app):
    """POST /api/auth/vault/reset should clear the session token."""
    token = generate_session_token()
    assert get_session_token() is not None

    with patch("backend.api.auth.reset_vault"):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/auth/vault/reset",
                headers={"X-Session-Token": token},
            )
            assert resp.status_code == 200
            assert get_session_token() is None


# ── WebSocket origin validation ──────────────────────────────────────────────

def test_validate_ws_origin_allows_app_origin():
    """WebSocket from the app's own origin should be allowed."""
    ws = AsyncMock()
    ws.headers = {"origin": "http://127.0.0.1:8765"}
    assert validate_ws_origin(ws) is True


def test_validate_ws_origin_allows_vite_dev():
    """WebSocket from the Vite dev server should be allowed."""
    ws = AsyncMock()
    ws.headers = {"origin": "http://localhost:5173"}
    assert validate_ws_origin(ws) is True


def test_validate_ws_origin_rejects_foreign_origin():
    """WebSocket from a foreign origin should be rejected."""
    ws = AsyncMock()
    ws.headers = {"origin": "http://evil.example.com"}
    assert validate_ws_origin(ws) is False


def test_validate_ws_origin_allows_missing_origin():
    """Missing Origin (e.g. desktop / Electron) should be allowed."""
    ws = AsyncMock()
    ws.headers = {}
    assert validate_ws_origin(ws) is True
