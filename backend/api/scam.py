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
from backend.db.models import Chat, Message, ScamAnalytics, ScamCheckRequest, ScamDismissal, ScamFeedback, ScamFlag, ScamReport
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

        # Fetch recent context messages from the same sender(s)
        from backend.scam.pipeline import get_pipeline_manager
        context_limit = 5
        mgr = get_pipeline_manager()
        if mgr:
            context_limit = mgr.get_config().context_messages
        if context_limit > 0:
            target_sender_ids = list({m.sender_id for m in msgs if m.sender_id})
            target_msg_ids = set(req.message_telegram_ids)
            if target_sender_ids:
                ctx_result = await db.execute(
                    select(Message).where(
                        Message.chat_id == req.chat_id,
                        Message.sender_id.in_(target_sender_ids),
                        Message.tg_message_id.notin_(list(target_msg_ids)),
                        Message.text.isnot(None),
                        Message.text != "",
                    ).order_by(Message.date.desc()).limit(context_limit)
                )
                ctx_msgs = ctx_result.scalars().all()
                for m in reversed(ctx_msgs):
                    messages.insert(0, {
                        "message_id": m.tg_message_id,
                        "sender_id": m.sender_id,
                        "sender_name": m.sender_name,
                        "text": m.text,
                        "date": m.date.isoformat() if m.date else None,
                    })

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

    # Analytics
    from backend.scam.analytics import get_analytics_tracker
    await get_analytics_tracker().record_manual_check()

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
                    "synced_to_cloud": f.synced_to_cloud or False,
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


def _scanned_message_filters(
    sender_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    min_members: Optional[int] = None,
    verdict_filter: Optional[str] = None,
):
    """Build SQLAlchemy filter conditions for scanned messages."""
    conditions = [Message.scam_score.isnot(None)]
    if sender_id is not None:
        conditions.append(Message.sender_id == sender_id)
    if chat_id is not None:
        conditions.append(Message.chat_id == chat_id)
    if min_members is not None:
        conditions.append(Message.chat_id.in_(
            select(Chat.telegram_id).where(Chat.members_count >= min_members)
        ))
    if verdict_filter == "unmarked":
        conditions.append(Message.scam_manual_verdict == "not_scam")
    elif verdict_filter == "scam":
        conditions.append(Message.scam_score > 0.5)
    elif verdict_filter == "failed":
        conditions.append(Message.scam_trace_json.contains('"status": "failed"'))
    return conditions


def _serialize_scanned_message(m: Message, chat_name: str) -> dict:
    return {
        "tg_message_id": m.tg_message_id,
        "chat_id": m.chat_id,
        "chat_name": chat_name,
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "text": m.text or "",
        "date": m.date.isoformat() if m.date else None,
        "scam_score": m.scam_score,
        "scam_reason": m.scam_reason,
        "scam_category": m.scam_category,
        "scam_manual_verdict": m.scam_manual_verdict,
        "llm_prompt": m.scam_llm_prompt,
        "llm_response": m.scam_llm_response,
        "trace_json": m.scam_trace_json,
        "synced_to_cloud": m.scam_synced_to_cloud or False,
    }


@router.get("/scanned-messages")
async def list_scanned_messages(
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
    sender_id: Optional[int] = Query(None),
    chat_id: Optional[int] = Query(None),
    min_members: Optional[int] = Query(None),
    verdict_filter: Optional[str] = Query(None),
):
    """List messages that have been scam-checked, with pagination and filters."""
    async with AsyncSessionLocal() as db:
        from sqlalchemy import func

        conditions = _scanned_message_filters(sender_id, chat_id, min_members, verdict_filter)

        # Total count
        count_q = select(func.count()).select_from(Message).where(*conditions)
        total = (await db.execute(count_q)).scalar() or 0

        # Paginated results
        offset = (page - 1) * page_size
        result = await db.execute(
            select(Message).where(*conditions)
            .order_by(Message.date.desc())
            .offset(offset).limit(page_size)
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
            "total": total,
            "page": page,
            "page_size": page_size,
            "messages": [
                _serialize_scanned_message(m, chat_id_to_name.get(m.chat_id, "Unknown"))
                for m in msgs
            ],
        }


class SetVerdictRequest(BaseModel):
    messages: list[dict]  # [{"chat_id": int, "tg_message_id": int}]
    verdict: Optional[str] = None  # "scam", "not_scam", or null to clear


@router.put("/scanned-messages/verdict")
async def set_message_verdict(req: SetVerdictRequest):
    """Set manual verdict on one or more scanned messages."""
    if req.verdict not in ("scam", "not_scam", None):
        raise HTTPException(status_code=400, detail="verdict must be 'scam', 'not_scam', or null")
    from sqlalchemy import update, and_, or_
    async with AsyncSessionLocal() as db:
        for item in req.messages:
            await db.execute(
                update(Message)
                .where(Message.chat_id == item["chat_id"], Message.tg_message_id == item["tg_message_id"])
                .values(scam_manual_verdict=req.verdict)
            )
        await db.commit()
    return {"status": "updated", "count": len(req.messages), "verdict": req.verdict}


@router.get("/scanned-messages/export")
async def export_scanned_messages(
    sender_id: Optional[int] = Query(None),
    chat_id: Optional[int] = Query(None),
    min_members: Optional[int] = Query(None),
    verdict_filter: Optional[str] = Query(None),
):
    """Export scanned messages as JSON for download."""
    from fastapi.responses import Response

    async with AsyncSessionLocal() as db:
        conditions = _scanned_message_filters(sender_id, chat_id, min_members, verdict_filter)
        result = await db.execute(
            select(Message).where(*conditions)
            .order_by(Message.date.desc())
            .limit(10000)
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

        rows = []
        for m in msgs:
            # Extract pipeline input from trace
            pipeline_input = None
            if m.scam_trace_json:
                try:
                    trace = json.loads(m.scam_trace_json)
                    pipeline_input = trace.get("input")
                    # Fallback for old traces without top-level input
                    if not pipeline_input and trace.get("steps"):
                        for step in trace["steps"]:
                            if step.get("input") and "$message" in step["input"]:
                                pipeline_input = step["input"]["$message"]
                                break
                except (json.JSONDecodeError, TypeError):
                    pass

            rows.append({
                "sender_id": m.sender_id,
                "sender_name": m.sender_name,
                "chat_id": m.chat_id,
                "chat_name": chat_id_to_name.get(m.chat_id, "Unknown"),
                "tg_message_id": m.tg_message_id,
                "message_text": m.text or "",
                "pipeline_input": pipeline_input,
                "pipeline_score": m.scam_score,
                "pipeline_verdict": (m.scam_score or 0) > 0.5,
                "pipeline_category": m.scam_category,
                "manual_verdict": m.scam_manual_verdict,
                "date": m.date.isoformat() if m.date else None,
            })

    # Write to temp file and return the path
    from backend.config import DATA_DIR
    import os
    exports_dir = DATA_DIR / "exports"
    exports_dir.mkdir(exist_ok=True)
    filename = f"shield_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = exports_dir / filename
    filepath.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"temp_path": str(filepath.resolve()), "filename": filename, "count": len(rows)}


@router.get("/scanned-messages/export/download")
async def download_export_file(path: str = Query(...)):
    """Serve a previously exported file for browser download."""
    from fastapi.responses import FileResponse
    from pathlib import Path
    filepath = Path(path)
    # Security: only allow files from our exports directory
    from backend.config import DATA_DIR
    exports_dir = (DATA_DIR / "exports").resolve()
    if not filepath.resolve().is_relative_to(exports_dir):
        raise HTTPException(status_code=403, detail="Invalid export path")
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Export file not found")
    return FileResponse(str(filepath), filename=filepath.name, media_type="application/json")


@router.delete("/flags/{telegram_id}")
async def dismiss_flag(telegram_id: int):
    """Dismiss/clear a scam flag for a user."""
    # Capture flag data before deletion (for cloud sync of dismissals)
    dismissed_flag_data = None
    async with AsyncSessionLocal() as db:
        flag_result = await db.execute(
            select(ScamFlag).where(ScamFlag.telegram_id == telegram_id)
        )
        flag = flag_result.scalars().first()
        if flag:
            dismissed_flag_data = {
                "telegram_id": flag.telegram_id,
                "source": flag.source,
                "confidence": flag.confidence,
                "reason": flag.reason,
                "category": flag.category,
                "flagged_message_text": flag.flagged_message_text,
                "flagged_at": flag.flagged_at.isoformat() if flag.flagged_at else None,
            }

        await db.execute(
            delete(ScamFlag).where(ScamFlag.telegram_id == telegram_id)
        )
        # Mark all messages from this sender as manually safe and clear scam_score
        from sqlalchemy import update
        await db.execute(
            update(Message)
            .where(Message.sender_id == telegram_id)
            .values(scam_score=None, scam_reason=None, scam_manual_verdict="not_scam")
        )
        await db.commit()

    # Analytics
    from backend.scam.analytics import get_analytics_tracker
    await get_analytics_tracker().record_flag_dismissed()

    # Queue dismissal for cloud sync if enabled
    if dismissed_flag_data:
        from backend.api.settings import _load_prefs
        prefs = _load_prefs()
        if prefs.get("shield_sync_dismissals", False):
            async with AsyncSessionLocal() as db:
                db.add(ScamDismissal(
                    dismissal_type="scammer",
                    payload_json=json.dumps(dismissed_flag_data, default=str),
                ))
                await db.commit()

    return {"status": "dismissed", "telegram_id": telegram_id}


@router.delete("/scanned-messages/{chat_id}/{tg_message_id}")
async def clear_message_scam(chat_id: int, tg_message_id: int):
    """Clear scam score on a specific message."""
    # Capture message scam data before clearing (for cloud sync of dismissals)
    dismissed_scan_data = None
    async with AsyncSessionLocal() as db:
        msg_result = await db.execute(
            select(Message).where(
                Message.chat_id == chat_id, Message.tg_message_id == tg_message_id
            )
        )
        msg = msg_result.scalars().first()
        if msg and msg.scam_score is not None:
            # Parse trace JSON to include full pipeline execution details
            trace_data = None
            if msg.scam_trace_json:
                try:
                    trace_data = json.loads(msg.scam_trace_json)
                except (json.JSONDecodeError, TypeError):
                    trace_data = None

            dismissed_scan_data = {
                "chat_id": chat_id,
                "tg_message_id": tg_message_id,
                "sender_id": msg.sender_id,
                "scam_score": msg.scam_score,
                "scam_reason": msg.scam_reason,
                "scam_category": msg.scam_category,
                "llm_prompt": msg.scam_llm_prompt,
                "llm_response": msg.scam_llm_response,
                "trace": trace_data,
            }

        from sqlalchemy import update
        await db.execute(
            update(Message)
            .where(Message.chat_id == chat_id, Message.tg_message_id == tg_message_id)
            .values(scam_score=None, scam_reason=None, scam_manual_verdict="not_scam")
        )
        await db.commit()

    from backend.scam.analytics import get_analytics_tracker
    await get_analytics_tracker().record_message_unflagged()

    # Queue dismissal for cloud sync if enabled
    if dismissed_scan_data:
        from backend.api.settings import _load_prefs
        prefs = _load_prefs()
        if prefs.get("shield_sync_dismissals", False):
            async with AsyncSessionLocal() as db:
                db.add(ScamDismissal(
                    dismissal_type="scan",
                    payload_json=json.dumps(dismissed_scan_data, default=str),
                ))
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

@router.post("/mass-scan")
async def mass_scan(max_messages: int = Query(200), max_age_hours: int = Query(48)):
    """Trigger scam scan on recent unscanned messages. Developer mode only."""
    from backend.telegram.sync import scan_unscanned_messages
    import asyncio
    count = await scan_unscanned_messages(max_messages=max_messages, max_age_hours=max_age_hours)
    return {"status": "queued", "messages_queued": count}


def _get_pipeline_tools() -> list:
    """Read tools array from the raw pipeline JSON file."""
    from backend.config import DATA_DIR
    import json as _json
    try:
        raw = _json.loads((DATA_DIR / "scam_pipeline.json").read_text(encoding="utf-8"))
        return raw.get("tools", [])
    except Exception:
        return []


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
        "nodes": [
            {"id": n.id, "type": n.type, "config": n.config, "short_circuit": n.short_circuit,
             "output_forward": n.output_forward, "output_trace_only": n.output_trace_only}
            for n in config.nodes
        ],
        "tools": _get_pipeline_tools(),
        "edges": [
            {"from": e.from_node, "to": e.to_node}
            for e in config.edges
        ],
        "output": config.output,
    }


@router.post("/pipeline/reload")
async def reload_pipeline():
    """Force reload pipeline from disk."""
    from backend.scam.pipeline import get_pipeline_manager
    mgr = get_pipeline_manager()
    if not mgr:
        raise HTTPException(status_code=503, detail="Pipeline not initialized")
    config = mgr.reload()
    return {"status": "reloaded", "version": config.version}


@router.post("/pipeline/sync")
async def sync_pipeline():
    """Trigger cloud sync or reload from disk."""
    from backend.scam.cloud_sync import get_cloud_sync_manager
    sync_mgr = get_cloud_sync_manager()
    if sync_mgr and sync_mgr.is_configured:
        result = await sync_mgr.sync()
        return {
            "status": result.status,
            "error": result.error,
            "errors": result.errors,
            "files_updated": result.files_updated,
            "reports_submitted": result.reports_submitted,
            "flags_submitted": result.flags_submitted,
            "scan_results_submitted": result.scan_results_submitted,
            "dismissals_submitted": result.dismissals_submitted,
        }
    else:
        # No cloud configured — just reload from disk
        from backend.scam.pipeline import get_pipeline_manager
        mgr = get_pipeline_manager()
        if mgr:
            config = mgr.reload()
            return {"status": "reloaded", "version": config.version}
        return {"status": "no_pipeline"}


@router.get("/sync/status")
async def sync_status():
    """Get cloud sync configuration and status."""
    from backend.scam.cloud_sync import get_cloud_sync_manager
    sync_mgr = get_cloud_sync_manager()
    from backend.scam.pipeline import get_pipeline_manager
    mgr = get_pipeline_manager()

    return {
        "configured": sync_mgr.is_configured if sync_mgr else False,
        "cloud_url": sync_mgr._base_url if sync_mgr else "",
        "last_synced_at": sync_mgr.last_synced_at.isoformat() if sync_mgr and sync_mgr.last_synced_at else None,
        "pipeline_version": mgr.get_config().version if mgr else None,
    }


@router.get("/sync/history")
async def sync_history():
    """Get sync status summary — counts of synced vs unsynced items."""
    from sqlalchemy import func, or_

    async with AsyncSessionLocal() as db:
        # Flags: synced vs total
        flags_total_r = await db.execute(select(func.count()).select_from(ScamFlag))
        flags_total = flags_total_r.scalar() or 0
        flags_synced_r = await db.execute(
            select(func.count()).select_from(ScamFlag).where(ScamFlag.synced_to_cloud == True)
        )
        flags_synced = flags_synced_r.scalar() or 0

        # Reports: synced vs total
        reports_total_r = await db.execute(select(func.count()).select_from(ScamReport))
        reports_total = reports_total_r.scalar() or 0
        reports_synced_r = await db.execute(
            select(func.count()).select_from(ScamReport).where(ScamReport.synced_at.isnot(None))
        )
        reports_synced = reports_synced_r.scalar() or 0

        # Scan results breakdown
        from sqlalchemy import and_

        # Total scanned
        scans_total_scanned_r = await db.execute(
            select(func.count()).select_from(Message).where(
                or_(Message.scam_score.isnot(None), Message.scam_trace_json.isnot(None))
            )
        )
        scans_total_scanned = scans_total_scanned_r.scalar() or 0

        # Scam detected (score > 0.5)
        scans_scam_r = await db.execute(
            select(func.count()).select_from(Message).where(Message.scam_score > 0.5)
        )
        scans_scam = scans_scam_r.scalar() or 0

        # Failed scans
        scans_failed_r = await db.execute(
            select(func.count()).select_from(Message).where(
                Message.scam_trace_json.isnot(None),
                Message.scam_trace_json.contains('"status": "failed"'),
            )
        )
        scans_failed = scans_failed_r.scalar() or 0

        # Syncable = scam + failed (what gets uploaded)
        scans_syncable = scans_scam + scans_failed

        # Synced count (among syncable)
        scans_synced_r = await db.execute(
            select(func.count()).select_from(Message).where(
                or_(
                    Message.scam_score > 0.5,
                    and_(
                        Message.scam_trace_json.isnot(None),
                        Message.scam_trace_json.contains('"status": "failed"'),
                    ),
                ),
                Message.scam_synced_to_cloud == True,
            )
        )
        scans_synced = scans_synced_r.scalar() or 0

        # Analytics: synced vs total periods
        analytics_total_r = await db.execute(
            select(func.count()).select_from(ScamAnalytics).where(ScamAnalytics.total_scanned > 0)
        )
        analytics_total = analytics_total_r.scalar() or 0
        analytics_synced_r = await db.execute(
            select(func.count()).select_from(ScamAnalytics).where(
                ScamAnalytics.total_scanned > 0,
                ScamAnalytics.synced_at.isnot(None),
            )
        )
        analytics_synced = analytics_synced_r.scalar() or 0

        # Dismissals queue
        dismissals_total_r = await db.execute(
            select(func.count()).select_from(ScamDismissal)
        )
        dismissals_total = dismissals_total_r.scalar() or 0
        dismissals_synced_r = await db.execute(
            select(func.count()).select_from(ScamDismissal).where(ScamDismissal.synced_at.isnot(None))
        )
        dismissals_synced = dismissals_synced_r.scalar() or 0

        # Latest analytics period summary
        latest_analytics_r = await db.execute(
            select(ScamAnalytics).where(ScamAnalytics.total_scanned > 0)
            .order_by(ScamAnalytics.period_start.desc()).limit(1)
        )
        latest_period = latest_analytics_r.scalars().first()
        latest_analytics_summary = None
        if latest_period:
            latest_analytics_summary = {
                "period_start": latest_period.period_start.isoformat(),
                "total_scanned": latest_period.total_scanned,
                "scam_detected": latest_period.scam_detected,
                "safe_detected": latest_period.safe_detected,
                "pipeline_errors": latest_period.pipeline_errors,
                "avg_pipeline_ms": round(latest_period.total_pipeline_ms / max(latest_period.total_scanned, 1)),
            }

        # Last synced timestamp and status
        from backend.scam.cloud_sync import get_cloud_sync_manager
        sync_mgr = get_cloud_sync_manager()
        last_synced = sync_mgr.last_synced_at.isoformat() if sync_mgr and sync_mgr.last_synced_at else None

        return {
            "last_synced_at": last_synced,
            "last_sync_status": sync_mgr._last_sync_status if sync_mgr else None,
            "last_sync_error": sync_mgr._last_sync_error if sync_mgr else None,
            "last_sync_details": sync_mgr._last_sync_details if sync_mgr else None,
            "flags": {"total": flags_total, "synced": flags_synced, "pending": flags_total - flags_synced},
            "reports": {"total": reports_total, "synced": reports_synced, "pending": reports_total - reports_synced},
            "scans": {
                "total": scans_syncable,
                "synced": scans_synced,
                "pending": scans_syncable - scans_synced,
                "total_scanned": scans_total_scanned,
                "scam_detected": scans_scam,
                "failed": scans_failed,
            },
            "dismissals": {
                "total": dismissals_total,
                "synced": dismissals_synced,
                "pending": dismissals_total - dismissals_synced,
            },
            "analytics": {
                "total": analytics_total,
                "synced": analytics_synced,
                "pending": analytics_total - analytics_synced,
                "latest": latest_analytics_summary,
            },
        }


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

        # Analytics
        from backend.scam.analytics import get_analytics_tracker
        await get_analytics_tracker().record_report_submitted()

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
                    "synced_at": r.synced_at.isoformat() if r.synced_at else None,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in reports
            ],
        }


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

class CreateFeedbackRequest(BaseModel):
    feedback_type: str  # general, false_positive, missed_scam, feature_request, bug
    message: str
    related_chat_id: Optional[int] = None
    related_message_id: Optional[int] = None
    related_flag_telegram_id: Optional[int] = None


@router.post("/feedback")
async def submit_feedback(req: CreateFeedbackRequest):
    """Submit user feedback about scam detection."""
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Feedback message cannot be empty")

    now = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        feedback = ScamFeedback(
            feedback_type=req.feedback_type,
            message=req.message.strip(),
            related_chat_id=req.related_chat_id,
            related_message_id=req.related_message_id,
            related_flag_telegram_id=req.related_flag_telegram_id,
            status="pending",
            created_at=now,
        )
        db.add(feedback)
        await db.commit()
        await db.refresh(feedback)

    # Try to sync immediately if cloud is configured
    synced = False
    sync_error = None
    from backend.scam.cloud_sync import get_cloud_sync_manager
    sync_mgr = get_cloud_sync_manager()
    if sync_mgr and sync_mgr.is_configured:
        try:
            synced = await sync_mgr.upload_feedback(feedback.id)
        except Exception as e:
            sync_error = str(e)

    return {
        "id": feedback.id,
        "status": "synced" if synced else "saved_locally",
        "sync_error": sync_error,
        "created_at": feedback.created_at.isoformat(),
    }


@router.get("/feedback")
async def list_feedback():
    """List all submitted feedback."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ScamFeedback).order_by(ScamFeedback.created_at.desc()).limit(50)
        )
        items = result.scalars().all()
        return {
            "feedback": [
                {
                    "id": f.id,
                    "feedback_type": f.feedback_type,
                    "message": f.message,
                    "related_chat_id": f.related_chat_id,
                    "related_message_id": f.related_message_id,
                    "related_flag_telegram_id": f.related_flag_telegram_id,
                    "status": f.status,
                    "synced_at": f.synced_at.isoformat() if f.synced_at else None,
                    "created_at": f.created_at.isoformat() if f.created_at else None,
                }
                for f in items
            ],
        }
