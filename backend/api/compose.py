from typing import Optional
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
