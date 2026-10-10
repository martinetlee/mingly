"""App-wide infra: Host/Origin middleware, error format, sessions, settings,
WebSocket eviction, lifespan readiness."""

import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from backend import instance
from backend import prefs as prefs_mod
from backend import secrets as vault
from backend import sessions
from backend.api import dependencies as deps
from backend.config import API_PORT
from backend.ws.manager import WebSocketManager


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT_FILE", tmp_path / "vault.enc")
    monkeypatch.setattr(prefs_mod, "PREFS_FILE", tmp_path / "prefs.json")
    prefs_mod.invalidate_cache()
    vault.lock()
    yield tmp_path
    vault.lock()
    prefs_mod.invalidate_cache()
    sessions.clear_session_token()


@pytest.fixture
def app():
    from backend.api.router import create_app

    return create_app()


def _client(app, base_url="http://127.0.0.1", **kw):
    return AsyncClient(transport=ASGITransport(app=app), base_url=base_url, **kw)


# ── Host / Origin ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("base_url,ok", [
    ("http://127.0.0.1", True),
    (f"http://localhost:{API_PORT}", True),
    ("http://localhost:5173", True),  # vite proxy forwards the dev Host
    ("http://evil.example.com", False),
    ("http://127.0.0.1.evil.com", False),
])
async def test_host_validation(app, base_url, ok):
    async with _client(app, base_url=base_url) as c:
        r = await c.get("/api/auth/vault-status")
    assert (r.status_code == 200) is ok
    if not ok:
        assert r.status_code == 403
        assert r.json() == {"detail": "Host not allowed"}


@pytest.mark.asyncio
async def test_foreign_origin_blocked_on_mutation(app):
    async with _client(app) as c:
        r = await c.post("/api/auth/lock", headers={"Origin": "http://evil.example.com"})
        assert r.status_code == 403
        r = await c.post("/api/auth/lock", headers={"Origin": "null"})
        assert r.status_code == 403
        # Safe methods are not origin-checked (CORS already prevents reading).
        r = await c.get("/api/auth/vault-status", headers={"Origin": "http://evil.example.com"})
        assert r.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [f"http://127.0.0.1:{API_PORT}", "http://localhost:5173"])
async def test_allowed_origin_passes_mutation(app, origin):
    token = sessions.generate_session_token()
    async with _client(app, headers={"X-Session-Token": token, "Origin": origin}) as c:
        r = await c.post("/api/settings/prefs/notifications", json={"pref": "all"})
    assert r.status_code == 200


def test_dev_origin_not_allowed_when_frozen(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert not deps._is_allowed_origin("http://localhost:5173")
    assert deps._is_allowed_origin(f"http://127.0.0.1:{API_PORT}")


# ── Error format ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_validation_error_is_flat_string(app):
    async with _client(app) as c:
        r = await c.post("/api/auth/vault/unlock", json={})
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["detail"], str)
    assert "password" in body["detail"]
    assert isinstance(body["errors"], list) and body["errors"]


@pytest.mark.asyncio
async def test_unknown_api_path_is_json_404(app):
    token = sessions.generate_session_token()
    async with _client(app, headers={"X-Session-Token": token}) as c:
        r = await c.get("/api/definitely/not/here")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["detail"] == "Not found: /api/definitely/not/here"
        # Known path, wrong method → 405, not 404.
        r = await c.delete("/api/auth/vault-status")
        assert r.status_code == 405


@pytest.mark.asyncio
async def test_vault_corrupt_maps_to_409_with_code(app, isolated_files):
    (isolated_files / "vault.enc").write_text("{broken")
    async with _client(app) as c:
        r = await c.post("/api/auth/vault/unlock", json={"password": "whatever"})
    assert r.status_code == 409
    assert r.json()["code"] == "vault_corrupt"
    assert isinstance(r.json()["detail"], str)


@pytest.mark.asyncio
async def test_dict_detail_flattened(app):
    from fastapi import HTTPException

    @app.get("/api/_test_dict_detail")
    async def _raise():
        raise HTTPException(status_code=400, detail={"message": "nope", "code": "x"})

    # Move the test route ahead of the /api catch-all.
    app.router.routes.insert(0, app.router.routes.pop())
    async with _client(app) as c:
        r = await c.get("/api/_test_dict_detail")
    assert r.json() == {"detail": "nope", "message": "nope", "code": "x"}


# ── Sessions ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_validate(app):
    async with _client(app) as c:
        r = await c.get("/api/session/validate")
        assert r.json()["valid"] is False
        assert r.json()["vault_status"] == "unset"
        token = sessions.generate_session_token()
        r = await c.get("/api/session/validate", headers={"X-Session-Token": token})
        assert r.json() == {"valid": True, "vault_status": "unset", "idle_timeout_minutes": 720}


@pytest.mark.asyncio
async def test_multiple_windows_stay_valid(app):
    t1 = sessions.generate_session_token()
    t2 = sessions.generate_session_token()
    async with _client(app) as c:
        for t in (t1, t2):
            r = await c.get("/api/settings/prefs", headers={"X-Session-Token": t})
            assert r.status_code == 200


def test_idle_expiry(monkeypatch):
    prefs_mod.save_prefs({"session_idle_timeout_minutes": 5})
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    t = sessions.generate_session_token()
    now[0] += 4 * 60
    assert sessions.is_valid_session_token(t, touch=True)  # activity resets idle timer
    now[0] += 4 * 60
    assert sessions.is_valid_session_token(t)
    now[0] += 6 * 60
    assert not sessions.is_valid_session_token(t)


def test_idle_timeout_zero_never_expires(monkeypatch):
    prefs_mod.save_prefs({"session_idle_timeout_minutes": 0})
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    t = sessions.generate_session_token()
    now[0] += 365 * 86400
    assert sessions.is_valid_session_token(t)


def test_session_cap_evicts_least_recent():
    tokens = [sessions.generate_session_token() for _ in range(sessions.MAX_SESSIONS + 1)]
    assert not sessions.is_valid_session_token(tokens[0])
    assert all(sessions.is_valid_session_token(t) for t in tokens[1:])


@pytest.mark.asyncio
async def test_expired_token_rejected_by_dependency(app, monkeypatch):
    prefs_mod.save_prefs({"session_idle_timeout_minutes": 5})
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    t = sessions.generate_session_token()
    now[0] += 10 * 60
    async with _client(app, headers={"X-Session-Token": t}) as c:
        r = await c.get("/api/settings/prefs")
    assert r.status_code == 401


# ── WebSocket manager ────────────────────────────────────────────────────────

class FakeWS:
    def __init__(self):
        self.closed = None
        self.sent = []

    async def accept(self):
        pass

    async def close(self, code=1000, reason=""):
        self.closed = code

    async def send_text(self, text):
        self.sent.append(text)


@pytest.mark.asyncio
async def test_ws_evicts_revoked_token_and_close_all():
    mgr = WebSocketManager()
    t1 = sessions.generate_session_token()
    t2 = sessions.generate_session_token()
    a, b = FakeWS(), FakeWS()
    await mgr.connect(a, t1)
    await mgr.connect(b, t2)

    import backend.ws.manager as manager_mod
    real = manager_mod.ws_manager
    manager_mod.ws_manager = mgr
    try:
        await deps.revoke_session(t1)
    finally:
        manager_mod.ws_manager = real
    assert a.closed == 4001 and b.closed is None

    await mgr.broadcast({"type": "x"})
    assert b.sent and not a.sent

    await mgr.close_all()
    assert b.closed == 4001
    assert mgr.connections == []


@pytest.mark.asyncio
async def test_ws_broadcast_drops_expired_sockets(monkeypatch):
    prefs_mod.save_prefs({"session_idle_timeout_minutes": 5})
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    mgr = WebSocketManager()
    ws = FakeWS()
    await mgr.connect(ws, sessions.generate_session_token())
    now[0] += 10 * 60
    await mgr.broadcast({"type": "x"})
    assert ws.closed == 4001 and ws.sent == []


def test_ws_endpoint_auth():
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from backend.api.router import create_app

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    host = {"Host": "127.0.0.1"}  # TestClient's WS handshake defaults to "testserver"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws?token=bad", headers=host) as ws:
            ws.receive_text()
    token = sessions.generate_session_token()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/api/ws?token={token}", headers={**host, "Origin": "http://evil.example.com"}
        ) as ws:
            ws.receive_text()
    with client.websocket_connect(f"/api/ws?token={token}", headers=host) as ws:
        ws.send_text("ping")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/ws?token={token}") as ws:  # Host: testserver
            ws.receive_text()


def test_broadcast_threadsafe_without_loop_is_noop():
    assert WebSocketManager().broadcast_threadsafe({"type": "x"}) is None


# ── Settings ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_prefs_is_whitelisted(app):
    prefs_mod.save_prefs({
        "notif_pref": "all",
        "scam_detection_api_key": "sk-secret",
        "shield_api_key": "sh-secret",
        "shield_last_sync_error": "boom",
    })
    token = sessions.generate_session_token()
    async with _client(app, headers={"X-Session-Token": token}) as c:
        r = await c.get("/api/settings/prefs")
    assert r.json() == {"notif_pref": "all"}


@pytest.mark.asyncio
@pytest.mark.parametrize("value,ok", [
    ("00:00", True), ("23:59", True), ("09:30", True),
    ("24:00", False), ("99:99", False), ("12:60", False), ("9:00", False), ("09:00 ", False), ("", False),
])
async def test_briefing_time_validation(app, value, ok):
    token = sessions.generate_session_token()
    async with _client(app, headers={"X-Session-Token": token}) as c:
        r = await c.post("/api/settings/prefs/briefing-time", json={"time": value})
    assert (r.status_code == 200) is ok
    if not ok:
        assert r.status_code == 400


def test_get_briefing_time_falls_back_on_bad_stored_value():
    from backend.briefing.settings import get_briefing_time

    prefs_mod.save_prefs({"briefing_time": "99:99"})
    assert get_briefing_time() == (9, 0)
    prefs_mod.save_prefs({"briefing_time": "18:45"})
    assert get_briefing_time() == (18, 45)


def test_plaintext_keys_migrate_into_vault_on_unlock(isolated_files):
    from backend import secret_prefs
    from backend.api import settings

    vault.add_unlock_listener(settings.on_vault_unlocked)
    prefs_mod.save_prefs({"scam_detection_api_key": "sk-1", "shield_api_key": "sh-1", "notif_pref": "off"})
    # Locked: legacy plaintext value is still served (startup path).
    vault.initialize_vault("pw-123456")  # fires unlock listener → migration
    assert vault.get_secret("scam_detection_api_key") == "sk-1"
    assert vault.get_secret("shield_api_key") == "sh-1"
    on_disk = json.loads((isolated_files / "prefs.json").read_text())
    assert on_disk == {"notif_pref": "off"}
    assert secret_prefs.get_secret_pref("scam_detection_api_key") == "sk-1"
    vault.lock()
    assert secret_prefs.get_secret_pref("scam_detection_api_key") == ""


def test_secret_pref_served_from_prefs_while_locked():
    from backend.secret_prefs import get_shield_api_key

    prefs_mod.save_prefs({"shield_api_key": "legacy"})
    assert get_shield_api_key() == "legacy"


@pytest.mark.asyncio
@pytest.mark.shield
async def test_scam_key_saved_to_vault_not_prefs(app, isolated_files, monkeypatch):
    import backend.api.settings as settings

    monkeypatch.setattr(settings, "reconfigure_scam_service", lambda: None)
    vault.initialize_vault("pw-123456")
    token = sessions.generate_session_token()
    async with _client(app, headers={"X-Session-Token": token}) as c:
        r = await c.post("/api/settings/prefs/scam-detection",
                         json={"mode": "external", "api_url": "https://x", "api_key": "sk-new"})
        assert r.status_code == 200
        r = await c.get("/api/settings/prefs/scam-detection")
        assert r.json()["api_key"] == "" and r.json()["api_key_set"] is True
        r = await c.post("/api/settings/prefs/scam-detection", json={"mode": "local_llm"})
        assert r.status_code == 200
    assert "sk-new" not in (isolated_files / "prefs.json").read_text()
    assert vault.get_secret("scam_detection_api_key") is None


@pytest.mark.asyncio
async def test_session_timeout_setting(app):
    token = sessions.generate_session_token()
    async with _client(app, headers={"X-Session-Token": token}) as c:
        assert (await c.post("/api/settings/prefs/session-timeout", json={"minutes": 3})).status_code == 400
        assert (await c.post("/api/settings/prefs/session-timeout", json={"minutes": 60})).status_code == 200
        assert (await c.get("/api/settings/prefs/session-timeout")).json() == {"minutes": 60}
        assert (await c.get("/api/session/validate")).json()["idle_timeout_minutes"] == 60


# ── Health / lifespan ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lifespan_marks_ready_and_runs_handlers(app, monkeypatch):
    import backend.db.database as database

    events = []

    @app.on_event("startup")
    async def _s():
        events.append("startup")

    @app.on_event("shutdown")
    async def _d():
        events.append("shutdown")

    async def fake_dispose():
        events.append("dispose")

    monkeypatch.setattr(database, "dispose_db", fake_dispose)
    async with _client(app) as c:
        assert (await c.get("/api/health/ready")).status_code == 503
        async with app.router.lifespan_context(app):
            assert instance.is_ready()
            r = await c.get("/api/health/ready")
            assert r.status_code == 200
            assert (await c.get("/api/health")).json() == {"app": "mingly", "ready": True}
        assert not instance.is_ready()
    assert events == ["startup", "shutdown", "dispose"]


@pytest.mark.asyncio
async def test_focus_endpoint(app):
    calls = []
    instance.set_focus_callback(lambda: calls.append(1))
    try:
        async with _client(app) as c:
            r = await c.post("/api/instance/focus")
        assert r.json() == {"focused": True} and calls == [1]
    finally:
        instance.set_focus_callback(None)


@pytest.mark.asyncio
async def test_channel_unavailable_answers_503_with_its_message():
    """Domain code raises TelegramUnavailable (no HTTP types below the API);
    the API maps it to 503 with the message as detail."""
    from backend.api.router import create_app
    from backend.telegram.client import TelegramUnavailable

    from fastapi.routing import APIRoute

    async def _probe():
        raise TelegramUnavailable()

    app = create_app()
    app.router.routes.insert(0, APIRoute("/api/_probe_unavailable", _probe))  # ahead of the SPA catch-all

    async with _client(app) as c:
        r = await c.get("/api/_probe_unavailable")
    assert r.status_code == 503
    assert r.json() == {"detail": "Telegram client not connected"}

