from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.telegram.client import get_client
from backend.db.database import AsyncSessionLocal
from backend.db import crud

router = APIRouter()


class SendRequest(BaseModel):
    chat_id: int
    text: str
    reply_to_id: Optional[int] = None


@router.post("/send")
async def send_message(req: SendRequest):
    client = get_client()
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        msg = await client.send_message(
            req.chat_id,
            req.text,
            reply_to=req.reply_to_id,
        )
        async with AsyncSessionLocal() as db:
            await crud.save_message(
                db,
                telegram_id=msg.id,
                chat_id=req.chat_id,
                sender_id=msg.sender_id,
                sender_name=None,
                text=msg.text or req.text,
                date=msg.date.replace(tzinfo=None),
                is_outgoing=True,
                media_type=None,
            )
        return {"success": True, "message_id": msg.id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class EditRequest(BaseModel):
    chat_id: int
    message_id: int
    text: str


@router.post("/edit")
async def edit_message(req: EditRequest):
    """Edit an outgoing message via Telegram."""
    client = get_client()
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        await client.edit_message(req.chat_id, req.message_id, req.text)
        # Update in local DB
        async with AsyncSessionLocal() as db:
            await crud.update_message_text(db, req.chat_id, req.message_id, req.text)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class DeleteRequest(BaseModel):
    chat_id: int
    message_ids: List[int]
    revoke: bool = False


@router.post("/delete")
async def delete_messages(req: DeleteRequest):
    """Delete messages. If revoke=True, delete for everyone."""
    client = get_client()
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        await client.delete_messages(req.chat_id, req.message_ids, revoke=req.revoke)
        # Remove from local DB
        async with AsyncSessionLocal() as db:
            await crud.delete_messages_by_telegram_ids(db, req.chat_id, req.message_ids)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class ForwardRequest(BaseModel):
    from_chat_id: int
    message_ids: List[int]
    to_chat_id: int


@router.post("/forward")
async def forward_messages(req: ForwardRequest):
    """Forward messages from one chat to another."""
    client = get_client()
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        await client.forward_messages(req.to_chat_id, req.message_ids, req.from_chat_id)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
