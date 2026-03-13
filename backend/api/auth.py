import asyncio
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

from backend.telegram.client import (
    get_client, send_code, sign_in, sign_in_2fa, persist_session,
    get_me, disconnect, has_credentials, init_client,
)
from backend.telegram.sync import sync_dialogs, register_event_handlers, bulk_sync
from backend.secrets import (
    save_secret, delete_secret,
    vault_status, initialize_vault, unlock as unlock_vault_fn, reset_vault,
)
from backend.api.dependencies import generate_session_token, clear_session_token

router = APIRouter()


class PhoneRequest(BaseModel):
    phone: str


class CodeRequest(BaseModel):
    phone: str
    code: str
    phone_code_hash: str


class PasswordRequest(BaseModel):
    password: str


class CredentialsRequest(BaseModel):
    api_id: str
    api_hash: str


class ResolveRequest(BaseModel):
    reference: str


class VaultPasswordRequest(BaseModel):
    password: str


# ── Vault endpoints ───────────────────────────────────────────────────────────

@router.get("/vault-status")
async def get_vault_status():
    return {"status": vault_status()}


@router.post("/vault/init")
async def init_vault(req: VaultPasswordRequest):
    if vault_status() != "unset":
        raise HTTPException(status_code=400, detail="Vault already initialized")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    initialize_vault(req.password)
    token = generate_session_token()
    return {"success": True, "session_token": token}


@router.post("/vault/unlock")
async def do_unlock_vault(req: VaultPasswordRequest):
    if not unlock_vault_fn(req.password):
        raise HTTPException(status_code=400, detail="Incorrect password")
    token = generate_session_token()
    # Re-init Telegram if credentials are already stored
    if has_credentials():
        try:
            authenticated = await init_client()
            if authenticated:
                await sync_dialogs()
                await register_event_handlers()
                asyncio.create_task(bulk_sync(n_chats=50, messages_per_chat=30))
        except Exception:
            pass
    return {"success": True, "session_token": token}


@router.post("/vault/reset")
async def do_reset_vault():
    clear_session_token()
    reset_vault()
    return {"success": True}


# ── Credentials / auth ────────────────────────────────────────────────────────

@router.get("/has-credentials")
async def auth_has_credentials():
    return {"has_credentials": has_credentials()}


@router.post("/resolve-1password")
async def auth_resolve_1password(req: ResolveRequest):
    """Resolve a 1Password secret reference (op://vault/item/field) via the op CLI."""
    ref = req.reference.strip()
    if not ref.startswith("op://"):
        raise HTTPException(status_code=400, detail="Not a 1Password reference (must start with op://)")
    try:
        result = subprocess.run(
            ["op", "read", ref],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail=result.stderr.strip() or "Failed to resolve 1Password reference",
            )
        return {"value": result.stdout.strip()}
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail="1Password CLI (op) not found. Install it from 1password.com/downloads/command-line/")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="1Password CLI timed out. Make sure 1Password is unlocked.")


@router.post("/setup")
async def auth_setup(req: CredentialsRequest):
    """Save Telegram API credentials to OS Keychain and initialise the client."""
    api_id = req.api_id.strip()
    api_hash = req.api_hash.strip()
    if not api_id.isdigit():
        raise HTTPException(status_code=400, detail="API ID must be a number")
    if not api_hash:
        raise HTTPException(status_code=400, detail="API Hash is required")
    save_secret("tg_api_id", api_id)
    save_secret("tg_api_hash", api_hash)
    try:
        await init_client()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Credentials saved but client init failed: {e}")
    return {"success": True}


@router.get("/status")
async def auth_status():
    client = get_client()
    if not client:
        return {"authenticated": False}
    try:
        if not await client.is_user_authorized():
            return {"authenticated": False}
        me = await get_me()
        return {
            "authenticated": True,
            "username": me.username,
            "first_name": me.first_name,
            "phone": me.phone,
        }
    except Exception:
        return {"authenticated": False}


@router.post("/send-code")
async def auth_send_code(req: PhoneRequest):
    try:
        phone_code_hash = await send_code(req.phone)
        return {"phone_code_hash": phone_code_hash}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/verify-code")
async def auth_verify_code(req: CodeRequest):
    try:
        await sign_in(req.phone, req.code, req.phone_code_hash)
        await persist_session()
        await sync_dialogs()
        await register_event_handlers()
        me = await get_me()
        return {
            "success": True,
            "requires_2fa": False,
            "username": me.username,
            "first_name": me.first_name,
        }
    except SessionPasswordNeededError:
        return {"success": True, "requires_2fa": True}
    except PhoneCodeInvalidError:
        raise HTTPException(status_code=400, detail="Invalid code. Please try again.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/verify-2fa")
async def auth_verify_2fa(req: PasswordRequest):
    try:
        await sign_in_2fa(req.password)
        await persist_session()
        await sync_dialogs()
        await register_event_handlers()
        me = await get_me()
        return {
            "success": True,
            "username": me.username,
            "first_name": me.first_name,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/logout")
async def auth_logout():
    await disconnect()
    delete_secret("telegram_session")
    return {"success": True}
