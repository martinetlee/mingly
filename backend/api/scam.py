"""Scam detection API endpoints."""

import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select

from backend.db.database import AsyncSessionLocal
from backend.db.models import Message, ScamCheckRequest, ScamFlag
from backend.scam.service import get_scam_service

logger = logging.getLogger(__name__)

router = APIRouter()


class CheckUsersRequest(BaseModel):
    telegram_ids: list[int]


class CheckMessagesRequest(BaseModel):
    chat_id: int
    message_telegram_ids: list[int]


class CallbackPayload(BaseModel):
    status: Optional[str] = None
    is_scam: Optional[bool] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None
    data_type: Optional[str] = None
    params: Optional[dict] = None


@router.post("/check-users")
async def check_users(req: CheckUsersRequest):
    """Trigger areFlaggedScams check for a list of telegram IDs."""
    svc = get_scam_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Scam detection not initialized")
    import asyncio
    asyncio.create_task(svc.check_flagged_users(req.telegram_ids))
    return {"status": "queued", "count": len(req.telegram_ids)}


@router.post("/check-messages")
async def check_messages(req: CheckMessagesRequest):
    """Trigger isThisScam check for messages in a chat."""
    svc = get_scam_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Scam detection not initialized")

    # Fetch messages from DB to build the payload
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Message).where(
                Message.chat_id == req.chat_id,
                Message.tg_message_id.in_(req.message_telegram_ids),
            )
        )
        msgs = result.scalars().all()
        if not msgs:
            raise HTTPException(status_code=404, detail="No messages found")

        messages = [
            {
                "message_id": m.tg_message_id,
                "sender_id": m.sender_id,
                "sender_name": m.sender_name,
                "text": m.text,
                "date": m.date.isoformat() if m.date else None,
            }
            for m in msgs
        ]

        # Build sender profiles with contact status
        sender_ids = list({m.sender_id for m in msgs if m.sender_id})
        profiles = []
        if sender_ids:
            from backend.db.models import Contact
            contact_result = await db.execute(
                select(Contact.telegram_id).where(Contact.telegram_id.in_(sender_ids))
            )
            contact_ids = {row[0] for row in contact_result.fetchall()}
            for m in msgs:
                if m.sender_id and m.sender_id not in {p["telegram_id"] for p in profiles}:
                    profiles.append({
                        "telegram_id": m.sender_id,
                        "name": m.sender_name,
                        "is_contact": m.sender_id in contact_ids,
                    })

        # Fetch chat metadata
        from backend.db.models import Chat
        chat_result = await db.execute(
            select(Chat).where(Chat.telegram_id == req.chat_id)
        )
        chat = chat_result.scalars().first()
        chat_metadata = {
            "chat_id": req.chat_id,
            "name": chat.name or "Unknown" if chat else "Unknown",
            "type": chat.type or "private" if chat else "private",
        }

    import asyncio
    asyncio.create_task(svc.check_messages_for_scam(
        chat_id=req.chat_id,
        messages=messages,
        user_telegram_id=0,
        profiles=profiles,
        chat_metadata=chat_metadata,
    ))
    return {"status": "queued", "message_count": len(messages)}


@router.post("/callback/{request_id}")
async def scam_callback(request_id: str, payload: CallbackPayload):
    """Webhook endpoint for external API responses."""
    svc = get_scam_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Scam detection not initialized")
    await svc.handle_api_response(request_id, payload.dict(exclude_none=True))
    return {"status": "received"}


@router.get("/flags")
async def list_flags():
    """List all scam-flagged users."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ScamFlag).order_by(ScamFlag.flagged_at.desc()))
        flags = result.scalars().all()
        return [
            {
                "telegram_id": f.telegram_id,
                "source": f.source,
                "confidence": f.confidence,
                "reason": f.reason,
                "flagged_at": f.flagged_at.isoformat() if f.flagged_at else None,
            }
            for f in flags
        ]


@router.delete("/flags/{telegram_id}")
async def dismiss_flag(telegram_id: int):
    """Dismiss/clear a scam flag for a user."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(ScamFlag).where(ScamFlag.telegram_id == telegram_id)
        )
        # Also clear scam_score on messages from this sender
        await db.execute(
            select(Message).where(Message.sender_id == telegram_id)
        )
        from sqlalchemy import update
        await db.execute(
            update(Message)
            .where(Message.sender_id == telegram_id)
            .values(scam_score=None, scam_reason=None)
        )
        await db.commit()
    return {"status": "dismissed", "telegram_id": telegram_id}


@router.get("/status/{chat_id}")
async def chat_scam_status(chat_id: int):
    """Get scam status for a chat — any flagged senders or messages."""
    async with AsyncSessionLocal() as db:
        # Find flagged messages in this chat
        msg_result = await db.execute(
            select(Message).where(
                Message.chat_id == chat_id,
                Message.scam_score.isnot(None),
                Message.scam_score > 0.5,
            )
        )
        flagged_msgs = msg_result.scalars().all()

        # Find flagged senders who have messages in this chat
        sender_ids = set()
        for m in flagged_msgs:
            if m.sender_id:
                sender_ids.add(m.sender_id)

        # Also check scam_flags table for any sender in this chat
        all_senders_result = await db.execute(
            select(Message.sender_id).where(
                Message.chat_id == chat_id,
                Message.sender_id.isnot(None),
            ).distinct()
        )
        all_sender_ids = [row[0] for row in all_senders_result.fetchall()]

        flag_result = await db.execute(
            select(ScamFlag).where(ScamFlag.telegram_id.in_(all_sender_ids))
        ) if all_sender_ids else None
        flagged_users = flag_result.scalars().all() if flag_result else []

        return {
            "has_scam_flags": len(flagged_msgs) > 0 or len(flagged_users) > 0,
            "flagged_message_count": len(flagged_msgs),
            "flagged_users": [
                {
                    "telegram_id": f.telegram_id,
                    "confidence": f.confidence,
                    "reason": f.reason,
                }
                for f in flagged_users
            ],
        }


@router.get("/requests")
async def list_requests(status: Optional[str] = None):
    """List scam check requests, optionally filtered by status."""
    async with AsyncSessionLocal() as db:
        query = select(ScamCheckRequest).order_by(ScamCheckRequest.created_at.desc()).limit(50)
        if status:
            query = query.where(ScamCheckRequest.status == status)
        result = await db.execute(query)
        reqs = result.scalars().all()
        return [
            {
                "request_id": r.request_id,
                "request_type": r.request_type,
                "status": r.status,
                "related_chat_id": r.related_chat_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in reqs
        ]
