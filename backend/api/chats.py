import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import utils
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter

from backend.config import DATA_DIR
from backend.db.database import get_db
from backend.db import crud
from backend.telegram.client import get_client
from backend.telegram.sync import sync_messages, bulk_sync

router = APIRouter()


class MonitorRequest(BaseModel):
    monitored: bool


class HideRequest(BaseModel):
    hidden: bool


class BriefingRequest(BaseModel):
    briefing: bool


class TrackRequest(BaseModel):
    tracked: bool


@router.get("/folders")
async def list_folders():
    client = get_client()
    if not client:
        return []
    response = await client(GetDialogFiltersRequest())
    filters = response.filters if hasattr(response, 'filters') else response
    result = []
    for f in filters:
        if not isinstance(f, DialogFilter):
            continue  # skip DialogFilterDefault / DialogFilterChatlist
        chat_ids = []
        for peer in f.include_peers:
            try:
                chat_ids.append(utils.get_peer_id(peer))
            except Exception:
                pass
        title = f.title.text if hasattr(f.title, 'text') else str(f.title)
        result.append({
            "id": f.id,
            "title": title,
            "chat_ids": chat_ids,
            "include_contacts": f.contacts,
            "include_non_contacts": f.non_contacts,
            "include_groups": f.groups,
            "include_channels": f.broadcasts,
            "include_bots": f.bots,
        })
    return result


def _chat_dict(c) -> dict:
    return {
        "id": c.id,
        "telegram_id": c.telegram_id,
        "name": c.name,
        "type": c.type,
        "username": c.username,
        "is_monitored": c.is_monitored,
        "is_hidden": c.is_hidden,
        "is_briefing": c.is_briefing,
        "is_tracked": c.is_tracked,
        "last_synced_at": c.last_synced_at,
    }


@router.post("/bulk-sync")
async def bulk_sync_chats():
    synced = await bulk_sync(n_chats=50, messages_per_chat=30)
    return {"synced": synced}


@router.get("")
async def list_chats(db: AsyncSession = Depends(get_db)):
    return [_chat_dict(c) for c in await crud.get_chats(db)]


@router.get("/hidden")
async def list_hidden_chats(db: AsyncSession = Depends(get_db)):
    return [_chat_dict(c) for c in await crud.get_hidden_chats(db)]


@router.get("/tracked")
async def list_tracked_chats(db: AsyncSession = Depends(get_db)):
    return [_chat_dict(c) for c in await crud.get_tracked_chats(db)]


@router.get("/briefing")
async def list_briefing_chats(db: AsyncSession = Depends(get_db)):
    return [_chat_dict(c) for c in await crud.get_briefing_chats(db)]


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


@router.post("/{telegram_id}/hide")
async def toggle_hide(
    telegram_id: int,
    req: HideRequest,
    db: AsyncSession = Depends(get_db),
):
    chat = await crud.set_hidden(db, telegram_id, req.hidden)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"telegram_id": telegram_id, "is_hidden": chat.is_hidden}


@router.post("/{telegram_id}/briefing")
async def toggle_briefing(
    telegram_id: int,
    req: BriefingRequest,
    db: AsyncSession = Depends(get_db),
):
    chat = await crud.set_briefing(db, telegram_id, req.briefing)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"telegram_id": telegram_id, "is_briefing": chat.is_briefing}


@router.post("/{telegram_id}/track")
async def toggle_track(
    telegram_id: int,
    req: TrackRequest,
    db: AsyncSession = Depends(get_db),
):
    chat = await crud.set_tracked(db, telegram_id, req.tracked)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"telegram_id": telegram_id, "is_tracked": chat.is_tracked}


@router.get("/{telegram_id}/messages")
async def get_messages(
    telegram_id: int,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    # Auto-sync on first open (skip hidden chats)
    if offset == 0:
        chat = await crud.get_chat_by_telegram_id(db, telegram_id)
        if chat and chat.last_synced_at is None and not chat.is_hidden and not chat.is_briefing:
            await sync_messages(telegram_id, limit=100)

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
            "media_url": f"/api/media/{m.media_path}" if m.media_path else None,
            "text_html": m.text_html,
            "reactions": json.loads(m.reactions_json) if m.reactions_json else None,
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
    await sync_messages(telegram_id, limit=100)
    return {"status": "ok"}
