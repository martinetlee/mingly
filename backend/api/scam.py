"""Scam detection API endpoints.

Verdict semantics live in ``backend.scam.verdict``: every response carries
``scam_status`` and the derived ``effective_is_scam``, and every verdict
change goes through ``set_verdict``. Checks are queued through
``backend.scam.queue``; results arrive as exactly one terminal WS event per
message (``scam_detected`` | ``scam_check_safe`` | ``scam_check_error``).
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select

from backend.config import DATA_DIR
from backend.db.database import AsyncSessionLocal
from backend.db.models import (
    SCAM_SUCCESS_STATUSES, Chat, Message, ScamAnalytics, ScamCheckRequest, ScamDismissal,
    ScamFeedback, ScamFlag, ScamReport,
)
from backend.scam.queue import enqueue_targets, get_scan_queue, scan_unscanned_messages
from backend.scam.service import get_scam_service
from backend.scam.verdict import FLAG_ACTIVE, not_pending_condition, scam_fields, scanned_condition, set_verdict
from backend.serialization import iso_utc
from backend.tasks import spawn

logger = logging.getLogger(__name__)

router = APIRouter()

_EXPORTS_DIR = DATA_DIR / "exports"
_EXPORT_TTL_SECONDS = 24 * 3600
_EXPORT_BATCH = 1000


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
    spawn(svc.check_flagged_users(req.telegram_ids), name="scam_check_flagged_users")
    return {"status": "queued", "count": len(req.telegram_ids)}


@router.post("/check-messages")
async def check_messages(req: CheckMessagesRequest):
    """Queue a scam check for each message (one request per target).

    Results arrive over WebSocket, one terminal event per message. A message
    already queued or running is not queued twice; its in-flight result
    answers this request too.
    """
    if not get_scam_service():
        raise HTTPException(status_code=503, detail="Scam detection not initialized")
    ids = list(dict.fromkeys(req.message_telegram_ids))
    if not ids:
        raise HTTPException(status_code=400, detail="No messages given")

    async with AsyncSessionLocal() as db:
        found = {row[0] for row in (await db.execute(
            select(Message.tg_message_id).where(
                Message.chat_id == req.chat_id, Message.tg_message_id.in_(ids)
            )
        )).all()}
    if not found:
        raise HTTPException(status_code=404, detail="No messages found")

    queue = get_scan_queue()
    already = [i for i in ids if i in found and queue.is_busy(req.chat_id, i)]
    await enqueue_targets(req.chat_id, [i for i in ids if i in found], source="manual")

    from backend.scam.analytics import get_analytics_tracker
    await get_analytics_tracker().increment(manual_checks=1)

    return {
        "status": "queued",
        "message_count": len(found),
        "queued_ids": [i for i in ids if i in found and i not in already],
        "already_running_ids": already,
        "missing_ids": [i for i in ids if i not in found],
    }


@router.post("/callback/{request_id}")
async def scam_callback(request_id: str, payload: CallbackPayload):
    """Webhook endpoint for external API responses."""
    svc = get_scam_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Scam detection not initialized")
    await svc.handle_api_response(request_id, payload.dict(exclude_none=True))
    return {"status": "received"}


def _flag_dict(f: ScamFlag, info: dict) -> dict:
    return {
        "telegram_id": f.telegram_id,
        "sender_name": info.get("sender_name"),
        "status": f.status,
        "source": f.source,
        "confidence": f.confidence,
        "reason": f.reason,
        "category": f.category,
        "flagged_message_text": f.flagged_message_text,
        "flagged_message_chat_id": f.flagged_message_chat_id,
        "flagged_message_id": f.flagged_message_id,
        "flagged_at": iso_utc(f.flagged_at),
        "dismissed_at": iso_utc(f.dismissed_at),
        "synced_to_cloud": f.synced_to_cloud or False,
        "last_chat_id": info.get("last_chat_id"),
        "last_chat_name": info.get("last_chat_name"),
        "last_message_id": info.get("last_message_id"),
        "last_active": info.get("last_active"),
        "last_message": info.get("last_message"),
    }


@router.get("/flags")
async def list_flags(include_dismissed: bool = Query(False)):
    """List flagged senders (status 'active'; dismissed ones on request) with last-active info."""
    async with AsyncSessionLocal() as db:
        q = select(ScamFlag).order_by(ScamFlag.flagged_at.desc())
        if not include_dismissed:
            q = q.where(ScamFlag.status == FLAG_ACTIVE)
        flags = (await db.execute(q)).scalars().all()

        active_tids = [f.telegram_id for f in flags if f.status == FLAG_ACTIVE]
        all_tids = [f.telegram_id for f in flags]
        flagged_chat_ids: list[int] = []
        sender_info: dict[int, dict] = {}

        if active_tids:
            flagged_chat_ids = [row[0] for row in (await db.execute(
                select(Message.chat_id).where(Message.sender_id.in_(active_tids)).distinct()
            )).all()]

        if all_tids:
            # Most recent message per sender (ties on date broken by id)
            latest_sub = (
                select(Message.sender_id, func.max(Message.id).label("max_id"))
                .where(Message.sender_id.in_(all_tids))
                .group_by(Message.sender_id)
                .subquery()
            )
            latest_msgs = (await db.execute(
                select(Message).join(latest_sub, Message.id == latest_sub.c.max_id)
            )).scalars().all()
            chat_names = await _chat_names(db, {m.chat_id for m in latest_msgs})
            for m in latest_msgs:
                sender_info[m.sender_id] = {
                    "sender_name": m.sender_name,
                    "last_chat_id": m.chat_id,
                    "last_chat_name": chat_names.get(m.chat_id, "Unknown"),
                    "last_message_id": m.tg_message_id,
                    "last_active": iso_utc(m.date),
                    "last_message": (m.text or "")[:200],
                }

        return {
            "flags": [_flag_dict(f, sender_info.get(f.telegram_id, {})) for f in flags],
            # Senders with an active flag, and the chats they appear in
            "flagged_sender_ids": active_tids,
            "flagged_chat_ids": flagged_chat_ids,
        }


async def _chat_names(db, chat_ids) -> dict[int, str]:
    chat_ids = list(chat_ids)
    if not chat_ids:
        return {}
    rows = (await db.execute(
        select(Chat.telegram_id, Chat.name).where(Chat.telegram_id.in_(chat_ids))
    )).all()
    return {row[0]: row[1] or "Unknown" for row in rows}


_VERDICT_FILTERS = ("scam", "not_scam", "failed", "pending", "manual", "unmarked")


def _scanned_message_filters(
    sender_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    min_members: Optional[int] = None,
    verdict_filter: Optional[str] = None,
):
    """Filter conditions for Shield's scanned list. All verdict filters use the
    stored ``effective_is_scam`` / ``scam_status``, never raw score checks."""
    conditions = [scanned_condition()]
    if sender_id is not None:
        conditions.append(Message.sender_id == sender_id)
    if chat_id is not None:
        conditions.append(Message.chat_id == chat_id)
    if min_members is not None:
        conditions.append(Message.chat_id.in_(
            select(Chat.telegram_id).where(Chat.members_count >= min_members)
        ))
    if verdict_filter in (None, "", "all"):
        pass
    elif verdict_filter == "scam":
        conditions.append(Message.effective_is_scam)
    elif verdict_filter == "not_scam":
        conditions.append(and_(
            ~Message.effective_is_scam,
            or_(Message.scam_manual_verdict.isnot(None), Message.scam_status.in_(SCAM_SUCCESS_STATUSES)),
        ))
    elif verdict_filter == "failed":
        conditions.append(Message.scam_status == "failed")
    elif verdict_filter == "pending":
        conditions.append(Message.scam_status == "pending")
    elif verdict_filter == "manual":
        conditions.append(Message.scam_manual_verdict.isnot(None))
    elif verdict_filter == "unmarked":  # legacy: user-marked not scam
        conditions.append(Message.scam_manual_verdict == "not_scam")
    else:
        raise HTTPException(status_code=400, detail=f"verdict_filter must be one of {', '.join(_VERDICT_FILTERS)}")
    return conditions


def _serialize_scanned_message(m: Message, chat_name: str) -> dict:
    return {
        "tg_message_id": m.tg_message_id,
        "chat_id": m.chat_id,
        "chat_name": chat_name,
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "text": m.text or "",
        "date": iso_utc(m.date),
        **scam_fields(m),
        "scam_attempts": m.scam_attempts or 0,
        "scam_next_retry_at": iso_utc(m.scam_next_retry_at),
        "scam_updated_at": iso_utc(m.scam_updated_at),
        "llm_prompt": m.scam_llm_prompt,
        "llm_response": m.scam_llm_response,
        "trace_json": m.scam_trace_json,
        "synced_to_cloud": _is_synced(m),
    }


def _is_synced(m: Message) -> bool:
    return bool(m.scam_synced_at and m.scam_updated_at and m.scam_synced_at >= m.scam_updated_at)


def _encode_cursor(m: Message) -> str:
    return f"{m.date.isoformat()}|{m.id}"


def _decode_cursor(cursor: str):
    try:
        date_s, id_s = cursor.rsplit("|", 1)
        return datetime.fromisoformat(date_s), int(id_s)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid cursor")


def _keyset_after(cursor: str):
    date, mid = _decode_cursor(cursor)
    return or_(Message.date < date, and_(Message.date == date, Message.id < mid))


@router.get("/scanned-messages")
async def list_scanned_messages(
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
    cursor: Optional[str] = Query(None, description="next_cursor from the previous page (keyset)"),
    sender_id: Optional[int] = Query(None),
    chat_id: Optional[int] = Query(None),
    min_members: Optional[int] = Query(None),
    verdict_filter: Optional[str] = Query(None),
):
    """Scanned messages, newest first, ordered by (date, id).

    Prefer ``cursor`` (keyset; stable while new scans arrive). ``page`` is
    kept for compatibility and is ignored when a cursor is given.
    """
    async with AsyncSessionLocal() as db:
        conditions = _scanned_message_filters(sender_id, chat_id, min_members, verdict_filter)
        total = (await db.execute(select(func.count()).select_from(Message).where(*conditions))).scalar() or 0

        q = select(Message).where(*conditions)
        if cursor:
            q = q.where(_keyset_after(cursor))
        else:
            q = q.offset((page - 1) * page_size)
        msgs = (await db.execute(
            q.order_by(Message.date.desc(), Message.id.desc()).limit(page_size + 1)
        )).scalars().all()
        has_more = len(msgs) > page_size
        msgs = msgs[:page_size]
        chat_names = await _chat_names(db, {m.chat_id for m in msgs})

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "next_cursor": _encode_cursor(msgs[-1]) if has_more and msgs else None,
            "messages": [_serialize_scanned_message(m, chat_names.get(m.chat_id, "Unknown")) for m in msgs],
        }


class MessageRef(BaseModel):
    chat_id: int
    tg_message_id: int


class SetVerdictRequest(BaseModel):
    messages: list[MessageRef]
    verdict: Optional[str] = None  # "scam", "not_scam", or null to clear


def _verdict_or_400(verdict: Optional[str]) -> None:
    if verdict not in ("scam", "not_scam", None):
        raise HTTPException(status_code=400, detail="verdict must be 'scam', 'not_scam', or null")


@router.put("/scanned-messages/verdict")
async def set_message_verdict(req: SetVerdictRequest):
    """Set (or clear, with null) the manual verdict on one or more messages."""
    _verdict_or_400(req.verdict)
    result = await set_verdict(
        scope="message", verdict=req.verdict,
        messages=[(m.chat_id, m.tg_message_id) for m in req.messages],
    )
    return {"status": "updated", **result.to_dict()}


class SenderVerdictRequest(BaseModel):
    verdict: Optional[str] = None  # "scam" flags, "not_scam" dismisses, null resets


@router.put("/senders/{telegram_id}/verdict")
async def set_sender_verdict(telegram_id: int, req: SenderVerdictRequest):
    """Sender-level verdict: flag, dismiss, or reset a sender."""
    _verdict_or_400(req.verdict)
    result = await set_verdict(scope="sender", verdict=req.verdict, telegram_id=telegram_id)
    return {"status": "updated", **result.to_dict()}


@router.delete("/flags/{telegram_id}")
async def dismiss_flag(telegram_id: int):
    """Dismiss a flagged sender (= sender verdict 'not_scam')."""
    result = await set_verdict(scope="sender", verdict="not_scam", telegram_id=telegram_id)
    return {"status": "dismissed", "telegram_id": telegram_id, **result.to_dict()}


@router.delete("/scanned-messages/{chat_id}/{tg_message_id}")
async def clear_message_scam(chat_id: int, tg_message_id: int):
    """Mark one message not scam (= message verdict 'not_scam'). Scores are kept."""
    result = await set_verdict(scope="message", verdict="not_scam", messages=[(chat_id, tg_message_id)])
    return {"status": "cleared", **result.to_dict()}


def _cleanup_exports() -> None:
    cutoff = time.time() - _EXPORT_TTL_SECONDS
    for f in _EXPORTS_DIR.glob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def _pipeline_input(trace_json: Optional[str]) -> Optional[str]:
    if not trace_json:
        return None
    try:
        trace = json.loads(trace_json)
    except (ValueError, TypeError):
        return None
    if trace.get("input"):
        return trace["input"]
    for step in trace.get("steps") or []:
        if isinstance(step.get("input"), dict) and "$message" in step["input"]:
            return step["input"]["$message"]
    return None


@router.get("/scanned-messages/export")
async def export_scanned_messages(
    sender_id: Optional[int] = Query(None),
    chat_id: Optional[int] = Query(None),
    min_members: Optional[int] = Query(None),
    verdict_filter: Optional[str] = Query(None),
):
    """Export every matching scanned message (no row cap) to a JSON file.

    Rows are streamed to a temp file in keyset batches and renamed into place;
    export files older than 24h are deleted on each export.
    """
    _EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_exports()
    filename = f"shield_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.json"
    filepath = _EXPORTS_DIR / filename
    tmp = _EXPORTS_DIR / f".{filename}.tmp"
    conditions = _scanned_message_filters(sender_id, chat_id, min_members, verdict_filter)

    count = 0
    cursor = None
    try:
        with open(tmp, "w", encoding="utf-8") as out:
            out.write("[")
            while True:
                async with AsyncSessionLocal() as db:
                    q = select(Message).where(*conditions)
                    if cursor is not None:
                        q = q.where(or_(Message.date < cursor[0], and_(Message.date == cursor[0], Message.id < cursor[1])))
                    msgs = (await db.execute(
                        q.order_by(Message.date.desc(), Message.id.desc()).limit(_EXPORT_BATCH)
                    )).scalars().all()
                    chat_names = await _chat_names(db, {m.chat_id for m in msgs})
                if not msgs:
                    break
                cursor = (msgs[-1].date, msgs[-1].id)
                for m in msgs:
                    row = {
                        "sender_id": m.sender_id,
                        "sender_name": m.sender_name,
                        "chat_id": m.chat_id,
                        "chat_name": chat_names.get(m.chat_id, "Unknown"),
                        "tg_message_id": m.tg_message_id,
                        "message_text": m.text or "",
                        "pipeline_input": _pipeline_input(m.scam_trace_json),
                        "pipeline_score": m.scam_score,
                        "pipeline_status": m.scam_status,
                        "pipeline_error": m.scam_error,
                        "pipeline_verdict": m.pipeline_is_scam,
                        "pipeline_category": m.scam_category,
                        "manual_verdict": m.scam_manual_verdict,
                        "effective_is_scam": bool(m.effective_is_scam),
                        "date": iso_utc(m.date),
                    }
                    out.write(("," if count else "") + "\n  " + json.dumps(row, ensure_ascii=False))
                    count += 1
            out.write("\n]\n")
        os.replace(tmp, filepath)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {"temp_path": str(filepath.resolve()), "filename": filename, "count": count}


@router.get("/scanned-messages/export/download")
async def download_export_file(path: str = Query(...)):
    """Serve a previously exported file for browser download."""
    from fastapi.responses import FileResponse

    filepath = Path(path)
    exports_dir = _EXPORTS_DIR.resolve()
    if not filepath.resolve().is_relative_to(exports_dir):
        raise HTTPException(status_code=403, detail="Invalid export path")
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Export file not found")
    return FileResponse(str(filepath), filename=filepath.name, media_type="application/json")


@router.get("/status/{chat_id}")
async def chat_scam_status(chat_id: int):
    """Scam status for a chat: effective-scam messages and active flagged senders."""
    async with AsyncSessionLocal() as db:
        flagged_msgs = (await db.execute(
            select(Message.tg_message_id, Message.sender_id).where(
                Message.chat_id == chat_id, Message.effective_is_scam,
            )
        )).all()
        flagged_users = (await db.execute(
            select(ScamFlag).where(
                ScamFlag.status == FLAG_ACTIVE,
                ScamFlag.telegram_id.in_(
                    select(Message.sender_id).where(Message.chat_id == chat_id, Message.sender_id.isnot(None))
                ),
            )
        )).scalars().all()

        return {
            "has_scam_flags": bool(flagged_msgs) or bool(flagged_users),
            "flagged_message_count": len(flagged_msgs),
            "flagged_message_ids": [row[0] for row in flagged_msgs],
            "flagged_users": [
                {"telegram_id": f.telegram_id, "confidence": f.confidence, "reason": f.reason, "source": f.source}
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
        reqs = (await db.execute(query)).scalars().all()
        return [
            {
                "request_id": r.request_id,
                "request_type": r.request_type,
                "status": r.status,
                "related_chat_id": r.related_chat_id,
                "related_message_ids": json.loads(r.related_message_ids_json or "[]"),
                "created_at": iso_utc(r.created_at),
                "completed_at": iso_utc(r.completed_at),
            }
            for r in reqs
        ]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

@router.post("/mass-scan")
async def mass_scan(max_messages: int = Query(200, ge=1, le=5000), max_age_hours: int = Query(48, ge=1)):
    """Queue recent messages with no successful scan (never scanned, or failed
    and due for retry). One request per message, through the bounded queue."""
    count = await scan_unscanned_messages(max_messages=max_messages, max_age_hours=max_age_hours)
    return {"status": "queued", "messages_queued": count, "queue": get_scan_queue().stats()}


@router.get("/queue")
async def queue_status():
    """Scan queue depth (queued / running / workers)."""
    return get_scan_queue().stats()


def _get_pipeline_tools() -> list:
    """Read tools array from the raw pipeline JSON file."""
    try:
        raw = json.loads((DATA_DIR / "scam_pipeline.json").read_text(encoding="utf-8"))
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
    """Run cloud sync (joining one already in progress), or reload from disk."""
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
            "analytics_submitted": result.analytics_submitted,
        }
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
    from backend.scam.pipeline import get_pipeline_manager
    sync_mgr = get_cloud_sync_manager()
    mgr = get_pipeline_manager()
    return {
        "configured": sync_mgr.is_configured if sync_mgr else False,
        "cloud_url": sync_mgr.base_url if sync_mgr else "",
        "url_error": sync_mgr.url_error if sync_mgr else None,
        "running": sync_mgr.is_running if sync_mgr else False,
        "last_synced_at": iso_utc(sync_mgr.last_synced_at) if sync_mgr else None,
        "pipeline_version": mgr.get_config().version if mgr else None,
    }


async def _count(db, *conditions) -> int:
    return (await db.execute(select(func.count()).select_from(Message).where(*conditions))).scalar() or 0


@router.get("/sync/history")
async def sync_history():
    """Sync status summary — counts of synced vs unsynced items."""
    async with AsyncSessionLocal() as db:
        flags_total = (await db.execute(select(func.count()).select_from(ScamFlag))).scalar() or 0
        flags_synced = (await db.execute(
            select(func.count()).select_from(ScamFlag).where(ScamFlag.synced_to_cloud == True)  # noqa: E712
        )).scalar() or 0

        reports_total = (await db.execute(select(func.count()).select_from(ScamReport))).scalar() or 0
        reports_synced = (await db.execute(
            select(func.count()).select_from(ScamReport).where(ScamReport.synced_at.isnot(None))
        )).scalar() or 0

        # Same row set the uploader sends (see cloud_sync._upload_scan_results)
        syncable = and_(
            Message.scam_updated_at.isnot(None),
            not_pending_condition(),
            or_(
                Message.effective_is_scam,
                Message.scam_status == "failed",
                Message.scam_manual_verdict.isnot(None),
                Message.scam_synced_at.isnot(None),
            ),
        )
        up_to_date = and_(Message.scam_synced_at.isnot(None), Message.scam_synced_at >= Message.scam_updated_at)
        scans_total_scanned = await _count(db, Message.scam_status.in_(SCAM_SUCCESS_STATUSES + ("failed",)))
        scans_scam = await _count(db, Message.effective_is_scam)
        scans_failed = await _count(db, Message.scam_status == "failed")
        scans_pending_queue = await _count(db, Message.scam_status == "pending")
        scans_syncable = await _count(db, syncable)
        scans_synced = await _count(db, syncable, up_to_date)

        analytics_total = (await db.execute(
            select(func.count()).select_from(ScamAnalytics).where(ScamAnalytics.total_scanned > 0)
        )).scalar() or 0
        analytics_synced = (await db.execute(
            select(func.count()).select_from(ScamAnalytics).where(
                ScamAnalytics.total_scanned > 0, ScamAnalytics.synced_at.isnot(None),
            )
        )).scalar() or 0

        dismissals_total = (await db.execute(select(func.count()).select_from(ScamDismissal))).scalar() or 0
        dismissals_synced = (await db.execute(
            select(func.count()).select_from(ScamDismissal).where(ScamDismissal.synced_at.isnot(None))
        )).scalar() or 0

        latest_period = (await db.execute(
            select(ScamAnalytics).where(ScamAnalytics.total_scanned > 0)
            .order_by(ScamAnalytics.period_start.desc()).limit(1)
        )).scalars().first()
        latest_analytics_summary = None
        if latest_period:
            latest_analytics_summary = {
                "period_start": iso_utc(latest_period.period_start),
                "total_scanned": latest_period.total_scanned,
                "scam_detected": latest_period.scam_detected,
                "safe_detected": latest_period.safe_detected,
                "pipeline_errors": latest_period.pipeline_errors,
                "avg_pipeline_ms": round(latest_period.total_pipeline_ms / max(latest_period.total_scanned, 1)),
            }

    from backend.scam.cloud_sync import get_cloud_sync_manager
    sync_mgr = get_cloud_sync_manager()
    return {
        "last_synced_at": iso_utc(sync_mgr.last_synced_at) if sync_mgr else None,
        "last_sync_status": sync_mgr._last_sync_status if sync_mgr else None,
        "last_sync_error": sync_mgr._last_sync_error if sync_mgr else None,
        "last_sync_details": sync_mgr._last_sync_details if sync_mgr else None,
        "running": sync_mgr.is_running if sync_mgr else False,
        "flags": {"total": flags_total, "synced": flags_synced, "pending": flags_total - flags_synced},
        "reports": {"total": reports_total, "synced": reports_synced, "pending": reports_total - reports_synced},
        "scans": {
            "total": scans_syncable,
            "synced": scans_synced,
            "pending": scans_syncable - scans_synced,
            "total_scanned": scans_total_scanned,
            "scam_detected": scans_scam,
            "failed": scans_failed,
            "queued": scans_pending_queue,
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
                    "date": iso_utc(m.date),
                    "is_outgoing": m.is_outgoing,
                    **scam_fields(m),
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
                entry["date"] = iso_utc(m.date)
            messages_data.append(entry)

        # Get local detection info if any scam scores exist
        local_detection = None
        scam_msgs = [m for m in included_msgs if m.scam_status is not None or m.scam_manual_verdict is not None]
        if scam_msgs:
            local_detection = {
                "scores": [
                    {"tg_message_id": m.tg_message_id, "scam_score": m.scam_score,
                     "scam_reason": m.scam_reason, "scam_category": m.scam_category,
                     "scam_status": m.scam_status, "manual_verdict": m.scam_manual_verdict,
                     "effective_is_scam": bool(m.effective_is_scam)}
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
            "created_at": iso_utc(report.created_at),
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
                    "synced_at": iso_utc(r.synced_at),
                    "created_at": iso_utc(r.created_at),
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
        "created_at": iso_utc(feedback.created_at),
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
                    "synced_at": iso_utc(f.synced_at),
                    "created_at": iso_utc(f.created_at),
                }
                for f in items
            ],
        }
