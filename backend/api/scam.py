"""Scam detection API endpoints."""

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, select

from backend.db.database import AsyncSessionLocal
from backend.db.models import Chat, Message, ScamCheckRequest, ScamFlag, ScamReport
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

    async def _run_check():
        try:
            await svc.check_messages_for_scam(
                chat_id=req.chat_id,
                messages=messages,
                user_telegram_id=0,
                profiles=profiles,
                chat_metadata=chat_metadata,
            )
        except Exception:
            logger.exception("Scam check task failed for chat %s", req.chat_id)

    asyncio.create_task(_run_check())
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
    """List all scam-flagged users with last-active info."""
    async with AsyncSessionLocal() as db:
        from sqlalchemy import distinct, func
        from backend.db.models import Chat

        result = await db.execute(select(ScamFlag).order_by(ScamFlag.flagged_at.desc()))
        flags = result.scalars().all()

        flagged_tids = [f.telegram_id for f in flags]
        flagged_chat_ids: list[int] = []
        # Per-flag: sender_name, last_active chat/time/message
        sender_info: dict[int, dict] = {}

        if flagged_tids:
            # Backward compat flat list for Chats.tsx
            chat_id_result = await db.execute(
                select(distinct(Message.chat_id)).where(
                    Message.sender_id.in_(flagged_tids)
                )
            )
            flagged_chat_ids = [row[0] for row in chat_id_result.fetchall()]

            # Get the most recent message per flagged sender (for last-active info)
            # Use a subquery to get max date per sender, then fetch the actual message
            latest_sub = (
                select(
                    Message.sender_id,
                    func.max(Message.date).label("max_date"),
                )
                .where(Message.sender_id.in_(flagged_tids))
                .group_by(Message.sender_id)
                .subquery()
            )
            latest_result = await db.execute(
                select(Message)
                .join(latest_sub, (Message.sender_id == latest_sub.c.sender_id) & (Message.date == latest_sub.c.max_date))
            )
            latest_msgs = latest_result.scalars().all()

            # Resolve chat names for these messages
            latest_chat_ids = list({m.chat_id for m in latest_msgs})
            chat_id_to_name: dict[int, str] = {}
            if latest_chat_ids:
                cn_result = await db.execute(
                    select(Chat.telegram_id, Chat.name).where(Chat.telegram_id.in_(latest_chat_ids))
                )
                chat_id_to_name = {row[0]: row[1] or "Unknown" for row in cn_result.fetchall()}

            for m in latest_msgs:
                sender_info[m.sender_id] = {
                    "sender_name": m.sender_name,
                    "last_chat_id": m.chat_id,
                    "last_chat_name": chat_id_to_name.get(m.chat_id, "Unknown"),
                    "last_message_id": m.tg_message_id,
                    "last_active": m.date.isoformat() if m.date else None,
                    "last_message": (m.text or "")[:200],
                }

        return {
            "flags": [
                {
                    "telegram_id": f.telegram_id,
                    "sender_name": sender_info.get(f.telegram_id, {}).get("sender_name"),
                    "source": f.source,
                    "confidence": f.confidence,
                    "reason": f.reason,
                    "category": f.category,
                    "flagged_message_text": f.flagged_message_text,
                    "flagged_message_chat_id": f.flagged_message_chat_id,
                    "flagged_message_id": f.flagged_message_id,
                    "flagged_at": f.flagged_at.isoformat() if f.flagged_at else None,
                    "last_chat_id": sender_info.get(f.telegram_id, {}).get("last_chat_id"),
                    "last_chat_name": sender_info.get(f.telegram_id, {}).get("last_chat_name"),
                    "last_message_id": sender_info.get(f.telegram_id, {}).get("last_message_id"),
                    "last_active": sender_info.get(f.telegram_id, {}).get("last_active"),
                    "last_message": sender_info.get(f.telegram_id, {}).get("last_message"),
                }
                for f in flags
            ],
            "flagged_chat_ids": flagged_chat_ids,
        }


@router.get("/scanned-messages")
async def list_scanned_messages():
    """List all messages that have been scam-checked (have a scam_score)."""
    async with AsyncSessionLocal() as db:
        from backend.db.models import Chat

        result = await db.execute(
            select(Message)
            .where(Message.scam_score.isnot(None))
            .order_by(Message.date.desc())
            .limit(200)
        )
        msgs = result.scalars().all()

        # Resolve chat names
        chat_ids = list({m.chat_id for m in msgs})
        chat_id_to_name: dict[int, str] = {}
        if chat_ids:
            cn_result = await db.execute(
                select(Chat.telegram_id, Chat.name).where(Chat.telegram_id.in_(chat_ids))
            )
            chat_id_to_name = {row[0]: row[1] or "Unknown" for row in cn_result.fetchall()}

        return {
            "messages": [
                {
                    "tg_message_id": m.tg_message_id,
                    "chat_id": m.chat_id,
                    "chat_name": chat_id_to_name.get(m.chat_id, "Unknown"),
                    "sender_id": m.sender_id,
                    "sender_name": m.sender_name,
                    "text": (m.text or "")[:200],
                    "date": m.date.isoformat() if m.date else None,
                    "scam_score": m.scam_score,
                    "scam_reason": m.scam_reason,
                    "scam_category": m.scam_category,
                    "llm_prompt": m.scam_llm_prompt,
                    "llm_response": m.scam_llm_response,
                }
                for m in msgs
            ],
        }


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


@router.delete("/scanned-messages/{chat_id}/{tg_message_id}")
async def clear_message_scam(chat_id: int, tg_message_id: int):
    """Clear scam score on a specific message."""
    async with AsyncSessionLocal() as db:
        from sqlalchemy import update
        await db.execute(
            update(Message)
            .where(Message.chat_id == chat_id, Message.tg_message_id == tg_message_id)
            .values(scam_score=None, scam_reason=None)
        )
        await db.commit()
    return {"status": "cleared"}


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


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

@router.get("/pipeline")
async def get_pipeline():
    """Return pipeline metadata and categories."""
    from backend.scam.pipeline import get_pipeline_manager
    mgr = get_pipeline_manager()
    if not mgr:
        raise HTTPException(status_code=503, detail="Pipeline not initialized")
    config = mgr.get_config()
    return {
        "version": config.version,
        "updated_at": config.updated_at,
        "categories": [
            {"id": c.id, "label": c.label, "description": c.description}
            for c in config.categories
        ],
    }


@router.post("/pipeline/sync")
async def sync_pipeline():
    """Trigger pipeline sync (stub: reloads from disk)."""
    from backend.scam.pipeline import get_pipeline_manager
    mgr = get_pipeline_manager()
    if not mgr:
        raise HTTPException(status_code=503, detail="Pipeline not initialized")
    config = mgr.sync()
    return {"version": config.version, "updated_at": config.updated_at}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

class CreateReportRequest(BaseModel):
    reported_telegram_id: int
    reported_in_chat_id: int
    reporter_verdict: str = "scam"
    scam_category: Optional[str] = None
    custom_category: Optional[str] = None
    included_message_ids: list[int]  # tg_message_ids to include


@router.get("/reports/context")
async def get_report_context(
    chat_id: int = Query(...),
    target_message_id: int = Query(...),
):
    """Get surrounding messages for building a report."""
    async with AsyncSessionLocal() as db:
        # Get messages around the target
        target_result = await db.execute(
            select(Message).where(
                Message.chat_id == chat_id,
                Message.tg_message_id == target_message_id,
            )
        )
        target = target_result.scalars().first()
        if not target:
            raise HTTPException(status_code=404, detail="Target message not found")

        # Get 10 messages before and 5 after by date
        before_result = await db.execute(
            select(Message).where(
                Message.chat_id == chat_id,
                Message.date <= target.date,
                Message.tg_message_id != target_message_id,
            ).order_by(Message.date.desc()).limit(10)
        )
        after_result = await db.execute(
            select(Message).where(
                Message.chat_id == chat_id,
                Message.date > target.date,
            ).order_by(Message.date.asc()).limit(5)
        )

        before_msgs = list(reversed(before_result.scalars().all()))
        after_msgs = after_result.scalars().all()
        all_msgs = before_msgs + [target] + after_msgs

        return {
            "messages": [
                {
                    "tg_message_id": m.tg_message_id,
                    "sender_id": m.sender_id,
                    "sender_name": m.sender_name,
                    "text": m.text,
                    "date": m.date.isoformat() if m.date else None,
                    "is_outgoing": m.is_outgoing,
                    "scam_score": m.scam_score,
                }
                for m in all_msgs
            ],
        }


@router.post("/reports")
async def create_report(req: CreateReportRequest):
    """Submit a scam report."""
    async with AsyncSessionLocal() as db:
        # Fetch the included messages
        msg_result = await db.execute(
            select(Message).where(
                Message.chat_id == req.reported_in_chat_id,
                Message.tg_message_id.in_(req.included_message_ids),
            ).order_by(Message.date.asc())
        )
        included_msgs = msg_result.scalars().all()
        included_ids = {m.tg_message_id for m in included_msgs}

        # Get chat info
        chat_result = await db.execute(
            select(Chat).where(Chat.telegram_id == req.reported_in_chat_id)
        )
        chat = chat_result.scalars().first()
        chat_type = chat.type if chat else None

        # Build messages_json — only include text for checked messages
        # Get the full context window to show omitted positions
        if included_msgs:
            min_date = min(m.date for m in included_msgs if m.date)
            max_date = max(m.date for m in included_msgs if m.date)
            context_result = await db.execute(
                select(Message).where(
                    Message.chat_id == req.reported_in_chat_id,
                    Message.date >= min_date,
                    Message.date <= max_date,
                ).order_by(Message.date.asc())
            )
            context_msgs = context_result.scalars().all()
        else:
            context_msgs = []

        messages_data = []
        for i, m in enumerate(context_msgs):
            is_reported = m.sender_id == req.reported_telegram_id
            is_included = m.tg_message_id in included_ids
            entry: dict = {
                "position": i,
                "telegram_id": m.sender_id,
                "role": "reported" if is_reported else ("reporter" if m.is_outgoing else "bystander"),
                "included": is_included,
            }
            if is_included:
                entry["sender_name"] = m.sender_name
                entry["text"] = m.text
                entry["date"] = m.date.isoformat() if m.date else None
            messages_data.append(entry)

        # Get local detection info if any scam scores exist
        local_detection = None
        scam_msgs = [m for m in included_msgs if m.scam_score is not None]
        if scam_msgs:
            local_detection = {
                "scores": [
                    {"tg_message_id": m.tg_message_id, "scam_score": m.scam_score,
                     "scam_reason": m.scam_reason, "scam_category": m.scam_category}
                    for m in scam_msgs
                ],
            }

        now = datetime.utcnow()
        report = ScamReport(
            report_id=str(uuid.uuid4()),
            reported_telegram_id=req.reported_telegram_id,
            reported_in_chat_id=req.reported_in_chat_id,
            chat_type=chat_type,
            reporter_verdict=req.reporter_verdict,
            scam_category=req.scam_category,
            custom_category=req.custom_category,
            messages_json=json.dumps(messages_data, default=str),
            local_detection_json=json.dumps(local_detection, default=str) if local_detection else None,
            status="submitted",
            created_at=now,
            updated_at=now,
        )
        db.add(report)
        await db.commit()

        return {
            "report_id": report.report_id,
            "status": report.status,
            "created_at": report.created_at.isoformat(),
        }


@router.get("/reports")
async def list_reports():
    """List all scam reports."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ScamReport).order_by(ScamReport.created_at.desc()).limit(100)
        )
        reports = result.scalars().all()

        # Resolve chat names and sender names
        chat_ids = list({r.reported_in_chat_id for r in reports})
        sender_ids = list({r.reported_telegram_id for r in reports})
        chat_names: dict[int, str] = {}
        sender_names: dict[int, str] = {}

        if chat_ids:
            cn_result = await db.execute(
                select(Chat.telegram_id, Chat.name).where(Chat.telegram_id.in_(chat_ids))
            )
            chat_names = {row[0]: row[1] or "Unknown" for row in cn_result.fetchall()}

        if sender_ids:
            from sqlalchemy import func
            sn_result = await db.execute(
                select(Message.sender_id, Message.sender_name)
                .where(Message.sender_id.in_(sender_ids), Message.sender_name.isnot(None))
                .order_by(Message.date.desc())
            )
            for row in sn_result.fetchall():
                if row[0] not in sender_names and row[1]:
                    sender_names[row[0]] = row[1]

        return {
            "reports": [
                {
                    "id": r.id,
                    "report_id": r.report_id,
                    "reported_telegram_id": r.reported_telegram_id,
                    "reported_sender_name": sender_names.get(r.reported_telegram_id),
                    "reported_in_chat_id": r.reported_in_chat_id,
                    "chat_name": chat_names.get(r.reported_in_chat_id, "Unknown"),
                    "chat_type": r.chat_type,
                    "reporter_verdict": r.reporter_verdict,
                    "scam_category": r.scam_category,
                    "custom_category": r.custom_category,
                    "status": r.status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in reports
            ],
        }
