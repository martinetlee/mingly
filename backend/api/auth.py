from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

from backend.telegram.client import (
    get_client, send_code, sign_in, sign_in_2fa, persist_session,
    get_me, disconnect,
)
from backend.telegram.sync import sync_dialogs, register_event_handlers
from backend.secrets import delete_secret

router = APIRouter()


class PhoneRequest(BaseModel):
    phone: str


class CodeRequest(BaseModel):
    phone: str
    code: str
    phone_code_hash: str


class PasswordRequest(BaseModel):
    password: str


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
