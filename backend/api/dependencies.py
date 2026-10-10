"""
Shared authentication and request-origin checks for API routes and WebSockets.

Sessions: the token store is ``backend.sessions``; ``require_session_token``
is its FastAPI dependency. WebSockets opened with a revoked or expired token
are closed by the WebSocket manager.

Origins: ``HostOriginMiddleware`` (installed app-wide in router.py) rejects
non-loopback Host headers (DNS rebinding) and state-changing requests whose
Origin isn't the app's own (or the Vite dev server in development).
"""

import json
import sys
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, WebSocket, status

from backend import sessions as _session_store
from backend.config import API_PORT
from backend.features import shield_enabled
from backend.sessions import has_sessions, is_valid_session_token
from backend.ws import manager as ws

# ── Session tokens ───────────────────────────────────────────────────────────


async def revoke_session(token: Optional[str] = None) -> None:
    """Revoke one session (*token*) or all of them, and close affected WebSockets.

    Use from lock/logout/reset flows; ``revoke_session()`` with no argument
    also closes every WebSocket immediately.
    """
    _session_store.revoke(token)
    if token is None:
        await ws.ws_manager.close_all()
    else:
        await ws.ws_manager.evict_invalid()


def require_shield() -> None:
    """Route dependency: Shield endpoints don't exist while it's disabled."""
    if not shield_enabled():
        raise HTTPException(status_code=404, detail="Not found")


async def require_session_token(
    x_session_token: Optional[str] = Header(None),
) -> str:
    """FastAPI dependency: reject requests without a valid session token.

    Used on all state-changing and data-access routes (everything except
    the auth/vault bootstrap endpoints). A valid request refreshes the
    token's idle timer.
    """
    if not has_sessions():
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
