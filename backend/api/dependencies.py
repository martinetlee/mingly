"""
Shared authentication and request-origin checks for API routes and WebSockets.

Sessions: every vault unlock/init issues a new token and ADDS it to a set of
valid tokens, so a second app window doesn't invalidate the first. Each token
expires after an idle period (prefs ``session_idle_timeout_minutes``, default
12h, 0 = never). Locking or resetting the vault revokes every token, and
WebSockets opened with a revoked or expired token are closed.

Origins: ``HostOriginMiddleware`` (installed app-wide in router.py) rejects
non-loopback Host headers (DNS rebinding) and state-changing requests whose
Origin isn't the app's own (or the Vite dev server in development).
"""

import asyncio
import hmac
import json
import secrets
import sys
import time
from typing import Dict, Optional
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, WebSocket, status

from backend import secrets as vault
from backend.config import API_PORT

# ── Session tokens ───────────────────────────────────────────────────────────

DEFAULT_IDLE_TIMEOUT_MINUTES = 720
MAX_SESSIONS = 16

# token -> last activity (time.monotonic())
_sessions: Dict[str, float] = {}
_latest_token: Optional[str] = None


def get_idle_timeout_minutes() -> int:
    from backend.prefs import get_pref

    raw = get_pref("session_idle_timeout_minutes", DEFAULT_IDLE_TIMEOUT_MINUTES)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_IDLE_TIMEOUT_MINUTES
    return max(0, value)


def _expired(last_seen: float, now: float, timeout_s: float) -> bool:
    return timeout_s > 0 and now - last_seen > timeout_s


def _prune(now: float) -> bool:
    """Drop idle-expired tokens. Returns True if any were removed."""
    timeout_s = get_idle_timeout_minutes() * 60
    stale = [t for t, seen in _sessions.items() if _expired(seen, now, timeout_s)]
    for t in stale:
        _sessions.pop(t, None)
    return bool(stale)


def _lookup(token: Optional[str]) -> Optional[str]:
    """Constant-time match of *token* against the active set."""
    if not token:
        return None
    match = None
    for t in _sessions:
        if hmac.compare_digest(t, token):
            match = t
    return match


def _schedule_ws_eviction() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop (sync caller/tests); sockets are rechecked on broadcast
    from backend.tasks import spawn
    from backend.ws.manager import ws_manager

    spawn(ws_manager.evict_invalid(), name="ws_evict_invalid")


def generate_session_token() -> str:
    """Issue a new session token (called when the vault is unlocked/initialized).

    Existing tokens stay valid; the least recently used is evicted beyond
    MAX_SESSIONS.
    """
    global _latest_token
    now = time.monotonic()
    pruned = _prune(now)
    token = secrets.token_urlsafe(32)
    _sessions[token] = now
    evicted = False
    while len(_sessions) > MAX_SESSIONS:
        oldest = min(_sessions, key=_sessions.get)
        _sessions.pop(oldest, None)
        evicted = True
    _latest_token = token
    if pruned or evicted:
        _schedule_ws_eviction()
    return token


def is_valid_session_token(token: Optional[str], touch: bool = False) -> bool:
    """True if *token* is active and not idle-expired. ``touch`` records activity."""
    match = _lookup(token)
    if match is None:
        return False
    now = time.monotonic()
    if _expired(_sessions[match], now, get_idle_timeout_minutes() * 60):
        _sessions.pop(match, None)
        return False
    if touch:
        _sessions[match] = now
    return True


def get_session_token() -> Optional[str]:
    """Return the most recently issued token if it is still valid, else None."""
    if _latest_token is not None and is_valid_session_token(_latest_token):
        return _latest_token
    return None


def active_session_count() -> int:
    _prune(time.monotonic())
    return len(_sessions)


def clear_session_token() -> None:
    """Revoke every session (vault lock/reset). Open WebSockets are closed."""
    global _latest_token
    had_sessions = bool(_sessions)
    _sessions.clear()
    _latest_token = None
    if had_sessions:
        _schedule_ws_eviction()


async def revoke_session(token: Optional[str] = None) -> None:
    """Revoke one session (*token*) or all of them, and close affected WebSockets.

    Use from lock/logout/reset flows; ``revoke_session()`` with no argument
    also closes every WebSocket immediately.
    """
    global _latest_token
    from backend.ws.manager import ws_manager

    if token is None:
        _sessions.clear()
        _latest_token = None
        await ws_manager.close_all()
        return
    match = _lookup(token)
    if match is not None:
        _sessions.pop(match, None)
        if match == _latest_token:
            _latest_token = None
    await ws_manager.evict_invalid()


# Locking or resetting the vault ends every session.
vault.add_lock_listener(clear_session_token)


async def require_session_token(
    x_session_token: Optional[str] = Header(None),
) -> str:
    """FastAPI dependency: reject requests without a valid session token.

    Used on all state-changing and data-access routes (everything except
    the auth/vault bootstrap endpoints). A valid request refreshes the
    token's idle timer.
    """
    if not _sessions:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No active session — unlock the vault",
        )
    if not is_valid_session_token(x_session_token, touch=True):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token",
        )
    return x_session_token  # type: ignore[return-value]


# ── Host / Origin validation ─────────────────────────────────────────────────

_LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DEV_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}


def allowed_origins() -> set:
    origins = {f"http://127.0.0.1:{API_PORT}", f"http://localhost:{API_PORT}"}
    if not getattr(sys, "frozen", False):
        origins |= DEV_ORIGINS
    return origins


def _is_allowed_origin(origin: str) -> bool:
    """Exact match against the app origin (and the Vite dev server in dev)."""
    return origin in allowed_origins()


def is_allowed_host(host: Optional[str], port: Optional[int] = None) -> bool:
    """Loopback Host header check. With *port*, the port must match exactly."""
    if not host:
        return False
    parts = urlsplit(f"//{host}")
    try:
        host_port = parts.port
    except ValueError:
        return False
    if parts.hostname not in _LOOPBACK_HOSTNAMES:
        return False
    return port is None or host_port == port


def validate_ws_origin(websocket: WebSocket) -> bool:
    """Check that the WebSocket Origin header matches the app's own origin."""
    origin = websocket.headers.get("origin")
    if origin is None:
        # Non-browser clients may not send Origin
        return True
    return _is_allowed_origin(origin)


class HostOriginMiddleware:
    """Pure ASGI middleware: loopback Host on every request; allowed Origin on
    state-changing HTTP requests and on WebSocket handshakes.

    Requests without an Origin header pass the origin check (same-origin GETs
    and non-browser clients); browsers always send Origin cross-origin.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        reason = None
        if not is_allowed_host(headers.get("host")):
            reason = "Host not allowed"
        else:
            origin = headers.get("origin")
            checks_origin = scope["type"] == "websocket" or scope.get("method", "GET") not in _SAFE_METHODS
            if checks_origin and origin is not None and not _is_allowed_origin(origin):
                reason = "Origin not allowed"
        if reason is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4003, "reason": reason})
            return
        body = json.dumps({"detail": reason}).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})
