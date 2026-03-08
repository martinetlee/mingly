from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db import crud
from backend.telegram.sync import sync_messages

router = APIRouter()


class MonitorRequest(BaseModel):
    monitored: bool


@router.get("")
async def list_chats(db: AsyncSession = Depends(get_db)):
    chats = await crud.get_chats(db)
    return [
        {
            "id": c.id,
            "telegram_id": c.telegram_id,
            "name": c.name,
            "type": c.type,
            "username": c.username,
            "is_monitored": c.is_monitored,
            "last_synced_at": c.last_synced_at,
        }
        for c in chats
    ]


@router.post("/{telegram_id}/monitor")
async def toggle_monitor(
    telegram_id: int,
    req: MonitorRequest,
    db: AsyncSession = Depends(get_db),
):
    chat = await crud.set_monitored(db, telegram_id, req.monitored)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if req.monitored:
        await sync_messages(telegram_id)
    return {"telegram_id": telegram_id, "is_monitored": chat.is_monitored}


@router.get("/{telegram_id}/messages")
async def get_messages(
    telegram_id: int,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    messages = await crud.get_messages(db, telegram_id, limit=limit, offset=offset)
    # Return in chronological order (DB returns newest-first)
    return [
        {
            "id": m.id,
            "telegram_id": m.telegram_id,
            "chat_id": m.chat_id,
            "sender_id": m.sender_id,
            "sender_name": m.sender_name,
            "text": m.text,
            "date": m.date,
            "is_outgoing": m.is_outgoing,
            "media_type": m.media_type,
            "importance_score": m.importance_score,
            "user_flagged": m.user_flagged,
        }
        for m in reversed(messages)
    ]


@router.post("/{telegram_id}/sync")
async def sync_chat(telegram_id: int, db: AsyncSession = Depends(get_db)):
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    await sync_messages(telegram_id)
    return {"status": "ok"}
