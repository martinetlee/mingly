"""
Shared authentication dependency for API routes and WebSocket connections.

Generates a session token when the vault is unlocked and validates it on
every incoming request. This adds an application-layer auth boundary so
that loopback-only transport is not the sole security mechanism.
"""

import secrets
from typing import Optional

from fastapi import Header, HTTPException, WebSocket, status

from backend.config import API_HOST, API_PORT

# ── In-memory session token (set on vault unlock / init) ─────────────────────

_session_token: Optional[str] = None

# Allowed WebSocket / request origins (same-origin policy)
_ALLOWED_ORIGINS: set[str] = {
    f"http://{API_HOST}:{API_PORT}",
    f"http://localhost:{API_PORT}",
    f"http://127.0.0.1:{API_PORT}",
    # Vite dev server
    "http://localhost:5173",
    "http://127.0.0.1:5173",
}


def _is_allowed_origin(origin: str) -> bool:
    """Check if origin is allowed — includes any localhost/127.0.0.1 port."""
    if origin in _ALLOWED_ORIGINS:
        return True
    # Accept any loopback origin (dynamic port)
    if origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:"):
        return True
    return False


def generate_session_token() -> str:
    """Create a new session token (called when vault is unlocked/initialized)."""
    global _session_token
    _session_token = secrets.token_urlsafe(32)
    return _session_token


def get_session_token() -> Optional[str]:
    """Return the current session token, or None if no session is active."""
    return _session_token


def clear_session_token() -> None:
    """Invalidate the current session (called on vault lock/reset)."""
    global _session_token
    _session_token = None


async def require_session_token(
    x_session_token: Optional[str] = Header(None),
) -> str:
    """FastAPI dependency: reject requests without a valid session token.

    Used on all state-changing and data-access routes (everything except
    the auth/vault bootstrap endpoints).
    """
    if _session_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Vault is locked — no active session",
        )
    if x_session_token != _session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing session token",
        )
    return x_session_token


def validate_ws_origin(websocket: WebSocket) -> bool:
    """Check that the WebSocket Origin header matches the app's own origin."""
    origin = websocket.headers.get("origin")
    if origin is None:
        # Electron / Tauri desktop clients may not send Origin
        return True
    return _is_allowed_origin(origin)
