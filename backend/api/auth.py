import asyncio
import logging
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from telethon.errors import PhoneCodeInvalidError

from backend.connectors.manager import manager
from backend.telegram.client import get_client, get_me, has_credentials
from backend.telegram.connector import InvalidCredentials, connector as telegram
from backend.telegram.runtime import runtime
from backend.secrets import (
    get_secret,
    vault_status, initialize_vault, unlock as unlock_vault_fn, reset_vault, lock as lock_vault_fn,
)
from backend.api.dependencies import generate_session_token, require_session_token, revoke_session
from backend.tasks import spawn

logger = logging.getLogger(__name__)

router = APIRouter()

# Every auth route except the vault bootstrap ones (status/init/unlock/reset)
# requires the session token: without it any web page could log the account
# out or overwrite the API credentials.
protected = [Depends(require_session_token)]

# How long unlock waits for the connectors to connect before answering
# anyway; the start keeps going in the background and reports via
# `connector_status` / `telegram_status`.
UNLOCK_START_TIMEOUT = 12


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


class VaultResetRequest(BaseModel):
    confirm: str = ""


async def _start_connectors_bounded() -> None:
    """Start every configured connector, waiting at most UNLOCK_START_TIMEOUT.

    Connect + authorize + handler registration are fast; syncing always runs in
    the background. A slow network must not hang the unlock screen, so after
    the timeout the start continues detached."""
    if not any(c.is_configured() for c in manager.all()):
        return
    task = spawn(manager.start_all(), name="connectors_start_all")
    try:
        await asyncio.wait_for(asyncio.shield(task), UNLOCK_START_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Connector start still in progress after %ss; continuing in background", UNLOCK_START_TIMEOUT)


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
    already_unlocked = vault_status() == "unlocked"
    # VaultCorruptError propagates to the app-wide handler (409, code "vault_corrupt").
    if not unlock_vault_fn(req.password):
        raise HTTPException(status_code=400, detail={"message": "Incorrect password", "code": "vault_wrong_password"})
    token = generate_session_token()
    if already_unlocked:
        # Re-unlock (second window, double Enter): only issue a token. The
        # runtime is idempotent, so make sure it is up without re-initialising.
        spawn(manager.start_all(), name="connectors_start_all")
    else:
        await _start_connectors_bounded()
    return {"success": True, "session_token": token}


@router.post("/vault/reset")
async def do_reset_vault(req: VaultResetRequest):
    """Destroy the vault. Callable without a session token (the user forgot
    the password), so it requires a typed confirmation instead."""
    if req.confirm != "RESET":
        raise HTTPException(status_code=400, detail='Type "RESET" to confirm')
    from backend.api.mcp import clear_mcp_sessions

    await manager.stop_all()
    clear_mcp_sessions()
    reset_vault()
    # Revoke every session token and close every WebSocket.
    await revoke_session()
    return {"success": True}


@router.post("/lock", dependencies=protected)
async def do_lock_vault():
    """Lock the vault: stop every connector (dropping in-memory sessions),
    forget the decrypted secrets (lock listeners drop derived caches, tokens
    and MCP sessions), then revoke every session token and close every
    WebSocket."""
    await manager.stop_all()
    lock_vault_fn()
    await revoke_session()
    return {"success": True}


# ── Credentials / auth ────────────────────────────────────────────────────────

@router.get("/has-credentials", dependencies=protected)
async def auth_has_credentials():
    return {"has_credentials": has_credentials()}


@router.post("/resolve-1password", dependencies=protected)
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


@router.post("/setup", dependencies=protected)
async def auth_setup(req: CredentialsRequest):
    """Save Telegram API credentials to the vault and connect a client."""
    try:
        await telegram.save_credentials(req.api_id, req.api_hash)
    except InvalidCredentials as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Credentials saved but client init failed: {e}")
    return {"success": True}


@router.get("/status", dependencies=protected)
async def auth_status():
    client = get_client()
    runtime_status = runtime.status()
    if not client:
        return {"authenticated": False, "connected": False, "runtime": runtime_status}
    try:
        if not await client.is_user_authorized():
            return {"authenticated": False, "connected": client.is_connected(), "runtime": runtime_status}
        me = await get_me()
        return {
            "authenticated": True,
            "connected": True,
            "username": me.username,
            "first_name": me.first_name,
            "phone": me.phone,
            "runtime": runtime_status,
        }
    except Exception:
        # Offline: a stored session still means the account is logged in —
        # don't bounce the user to onboarding because Wi-Fi is down.
        return {
            "authenticated": bool(get_secret("telegram_session")),
            "connected": False,
            "runtime": runtime_status,
        }


@router.post("/send-code", dependencies=protected)
async def auth_send_code(req: PhoneRequest):
    try:
        phone_code_hash = await telegram.send_code(req.phone)
        return {"phone_code_hash": phone_code_hash}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/verify-code", dependencies=protected)
async def auth_verify_code(req: CodeRequest):
    try:
        info = await telegram.sign_in(req.phone, req.code, req.phone_code_hash)
    except PhoneCodeInvalidError:
        raise HTTPException(status_code=400, detail="Invalid code. Please try again.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if info is None:
        return {"success": True, "requires_2fa": True}
    return {"success": True, "requires_2fa": False, **info}


@router.post("/verify-2fa", dependencies=protected)
async def auth_verify_2fa(req: PasswordRequest):
    try:
        info = await telegram.sign_in_2fa(req.password)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, **info}


@router.post("/logout", dependencies=protected)
async def auth_logout():
    """Log out of Telegram: terminate the server-side session, stop all sync
    work and forget the local session string."""
    await telegram.logout()
    # The vault stays unlocked (the user continues to onboarding with the same
    # session token), so only drop the sockets carrying the old account's events.
    from backend.ws.manager import ws_manager
    await ws_manager.close_all(reason="Telegram logged out")
    return {"success": True}
