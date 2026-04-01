from typing import Optional, Tuple
from telethon import TelegramClient
from telethon.sessions import StringSession

from backend.config import TG_API_ID, TG_API_HASH
from backend.secrets import get_secret, save_secret

_client: Optional[TelegramClient] = None


def get_client() -> Optional[TelegramClient]:
    return _client


def _get_api_credentials() -> Tuple[int, str]:
    """
    Return (api_id, api_hash). Keychain takes priority over env/.env values.
    """
    api_id_str = get_secret("tg_api_id")
    api_hash = get_secret("tg_api_hash")
    if api_id_str and api_hash:
        return int(api_id_str), api_hash
    return TG_API_ID, TG_API_HASH


def has_credentials() -> bool:
    """Return True if TG API credentials are available from any source."""
    api_id, api_hash = _get_api_credentials()
    return bool(api_id and api_hash)


async def init_client() -> bool:
    """
    Load session from keychain and connect.
    Returns True if the session is already authorised (skip onboarding).
    """
    global _client
    api_id, api_hash = _get_api_credentials()
    session_string = get_secret("telegram_session") or ""
    _client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
        receive_updates=True,
    )
    await _client.connect()
    return await _client.is_user_authorized()


async def ensure_connected() -> None:
    """Reconnect the client if it has become disconnected."""
    global _client
    if _client is None:
        await init_client()
        return
    if not _client.is_connected():
        await _client.connect()


async def send_code(phone: str) -> str:
    """Request a login code. Returns phone_code_hash."""
    await ensure_connected()
    result = await _client.send_code_request(phone)
    return result.phone_code_hash


async def sign_in(phone: str, code: str, phone_code_hash: str):
    await ensure_connected()
    return await _client.sign_in(
        phone=phone,
        code=code,
        phone_code_hash=phone_code_hash,
    )


async def sign_in_2fa(password: str):
    await ensure_connected()
    return await _client.sign_in(password=password)


async def persist_session() -> None:
    """Save the current session string to OS Keychain."""
    save_secret("telegram_session", _client.session.save())


async def get_me():
    return await _client.get_me()


async def disconnect() -> None:
    global _client
    if _client:
        await _client.disconnect()
        _client = None
