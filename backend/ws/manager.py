import asyncio
import concurrent.futures
import json
import logging
from typing import Dict, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Close code the frontend treats as "session ended — re-authenticate".
SESSION_ENDED_CODE = 4001


class WebSocketManager:
    """Tracks open sockets and the session token each one authenticated with,
    so revoking or expiring a token also disconnects its sockets."""

    def __init__(self):
        self._tokens: Dict[WebSocket, str] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def connections(self) -> "list[WebSocket]":
        return list(self._tokens)

    async def connect(self, ws: WebSocket, token: str = ""):
        await ws.accept()
        self._loop = asyncio.get_running_loop()
        self._tokens[ws] = token

    def disconnect(self, ws: WebSocket):
        self._tokens.pop(ws, None)

    async def _close(self, ws: WebSocket, code: int, reason: str) -> None:
        self.disconnect(ws)
        try:
            await ws.close(code=code, reason=reason)
        except Exception:
            pass  # already closed by the peer

    async def evict_invalid(self) -> int:
        """Close sockets whose session token was revoked or has expired."""
        from backend.api.dependencies import is_valid_session_token

        stale = [ws for ws, tok in list(self._tokens.items()) if not is_valid_session_token(tok)]
        for ws in stale:
            await self._close(ws, SESSION_ENDED_CODE, "Session ended")
        return len(stale)

    async def close_all(self, code: int = SESSION_ENDED_CODE, reason: str = "Session ended") -> None:
        for ws in list(self._tokens):
            await self._close(ws, code, reason)

    async def broadcast(self, data: dict):
        await self.evict_invalid()
        msg = json.dumps(data, default=str)
        for ws in list(self._tokens):
            try:
                await ws.send_text(msg)
            except Exception:
                self.disconnect(ws)

    def broadcast_threadsafe(self, data: dict) -> Optional[concurrent.futures.Future]:
        """Broadcast from a non-server thread (e.g. the Cocoa main thread).

        Schedules onto the server loop that owns the sockets. Returns None if
        no socket has connected yet (nothing to deliver to).
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return None
        return asyncio.run_coroutine_threadsafe(self.broadcast(data), loop)


ws_manager = WebSocketManager()
