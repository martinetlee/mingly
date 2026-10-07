"""Low-level Telethon client storage.

Only ``backend.telegram.runtime`` creates or replaces the client (under its
lock). Everything else obtains a connected client through
``runtime.client(require=True)``, which raises ``TelegramUnavailable``
(an HTTP 503) or, with ``require=False``, returns None.
"""

import logging
from typing import Optional, Tuple

from fastapi import HTTPException
from telethon import TelegramClient
from telethon.sessions import StringSession

from backend.config import TG_API_ID, TG_API_HASH
from backend.secrets import get_secret, save_secret

logger = logging.getLogger(__name__)

_client: Optional[TelegramClient] = None


class TelegramUnavailable(HTTPException):
    """No connected Telegram client. An HTTPException so any router that lets
    it propagate answers 503 without its own handler."""

    def __init__(self, detail: str = "Telegram client not connected"):
        super().__init__(status_code=503, detail=detail)


def get_client() -> Optional[TelegramClient]:
    """Peek at the current client without connecting (status checks only).
    Use ``runtime.client()`` to get a usable, connected client."""
    return _client


def _get_api_credentials() -> Tuple[int, str]:
    """Return (api_id, api_hash). Vault values take priority over env/.env."""
    api_id_str = get_secret("tg_api_id")
    api_hash = get_secret("tg_api_hash")
    if api_id_str and api_hash:
        return int(api_id_str), api_hash
    return TG_API_ID, TG_API_HASH


def has_credentials() -> bool:
    """Return True if TG API credentials are available from any source."""
    api_id, api_hash = _get_api_credentials()
    return bool(api_id and api_hash)


async def _create_client() -> bool:
    """Replace the client with a fresh, connected one. Returns whether the
    stored session is authorized. Call only from the runtime, under its lock:
    two live clients on one auth key duplicate every update and risk
    AUTH_KEY_DUPLICATED."""
    global _client
    old, _client = _client, None
    if old is not None:
        try:
            await old.disconnect()
        except Exception:
            logger.warning("Disconnecting previous Telegram client failed", exc_info=True)
    api_id, api_hash = _get_api_credentials()
    session_string = get_secret("telegram_session") or ""
    client = TelegramClient(StringSession(session_string), api_id, api_hash, receive_updates=True)
    _client = client
    await client.connect()
    return await client.is_user_authorized()


async def persist_session() -> None:
    """Save the current session string to the vault."""
    save_secret("telegram_session", _client.session.save())


async def get_me():
    return await _client.get_me()


async def log_out() -> None:
    """Terminate the Telegram session server-side (removes it from Active
    Sessions) and drop the client. Falls back to a plain disconnect."""
    global _client
    client, _client = _client, None
    if client is None:
        return
    try:
        if not client.is_connected():
            await client.connect()
        await client.log_out()
    except Exception:
        logger.warning("Telegram log_out failed; disconnecting only", exc_info=True)
        try:
            await client.disconnect()
        except Exception:
            pass


async def disconnect() -> None:
    global _client
    client, _client = _client, None
    if client is not None:
        await client.disconnect()
