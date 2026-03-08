from telethon import TelegramClient
from telethon.sessions import StringSession

from backend.config import TG_API_ID, TG_API_HASH
from backend.secrets import get_secret, save_secret

_client: TelegramClient | None = None


def get_client() -> TelegramClient | None:
    return _client


async def init_client() -> bool:
    """
    Load session from keychain and connect.
    Returns True if the session is already authorised (skip onboarding).
    """
    global _client
    session_string = get_secret("telegram_session") or ""
    _client = TelegramClient(
        StringSession(session_string),
        TG_API_ID,
        TG_API_HASH,
    )
    await _client.connect()
    return await _client.is_user_authorized()


async def send_code(phone: str) -> str:
    """Request a login code. Returns phone_code_hash."""
    result = await _client.send_code_request(phone)
    return result.phone_code_hash


async def sign_in(phone: str, code: str, phone_code_hash: str):
    return await _client.sign_in(
        phone=phone,
        code=code,
        phone_code_hash=phone_code_hash,
    )


async def sign_in_2fa(password: str):
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
