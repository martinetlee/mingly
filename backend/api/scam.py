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
    SCAM_SUCCESS_STATUSES, Conversation, Identity, Message, ScamAnalytics, ScamCheckRequest, ScamDismissal,
    ScamFeedback, ScamFlag, ScamReport,
)
from backend.scam.queue import enqueue_targets, get_scan_queue, scan_unscanned_messages
from backend.scam.service import get_scam_service
from backend.scam.verdict import FLAG_ACTIVE, not_pending_condition, scanned_condition, set_verdict
from backend.serializers import scam_fields
from backend.serialization import iso_utc
from backend.tasks import spawn

logger = logging.getLogger(__name__)

router = APIRouter()

_EXPORTS_DIR = DATA_DIR / "exports"
_EXPORT_TTL_SECONDS = 24 * 3600
_EXPORT_BATCH = 1000


class CheckUsersRequest(BaseModel):
    identity_ids: list[int]


class CheckMessagesRequest(BaseModel):
    conversation_id: int
    message_ids: list[int]


class CallbackPayload(BaseModel):
    status: Optional[str] = None
    is_scam: Optional[bool] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None
    data_type: Optional[str] = None
    params: Optional[dict] = None


@router.post("/check-users")
async def check_users(req: CheckUsersRequest):
    """Trigger an areFlaggedScams check for a list of sender identities."""
    svc = get_scam_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Scam detection not initialized")
    spawn(svc.check_flagged_users(req.identity_ids), name="scam_check_flagged_users")
    return {"status": "queued", "count": len(req.identity_ids)}


@router.post("/check-messages")
async def check_messages(req: CheckMessagesRequest):
    """Queue a scam check for each message (one request per target).

    Results arrive over WebSocket, one terminal event per message. A message
    already queued or running is not queued twice; its in-flight result
    answers this request too.
    """
    if not get_scam_service():
        raise HTTPException(status_code=503, detail="Scam detection not initialized")
    ids = list(dict.fromkeys(req.message_ids))
    if not ids:
        raise HTTPException(status_code=400, detail="No messages given")

    async with AsyncSessionLocal() as db:
        found = {row[0] for row in (await db.execute(
            select(Message.id).where(Message.conversation_id == req.conversation_id, Message.id.in_(ids))
        )).all()}
    if not found:
        raise HTTPException(status_code=404, detail="No messages found")

    queue = get_scan_queue()
    already = [i for i in ids if i in found and queue.is_busy(req.conversation_id, i)]
    await enqueue_targets(req.conversation_id, [i for i in ids if i in found], source="manual")

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


def _flag_dict(f: ScamFlag, person_id: Optional[int], info: dict) -> dict:
    return {
        "identity_id": f.identity_id,
        "person_id": person_id,
        "sender_name": info.get("sender_name"),
        "status": f.status,
        "source": f.source,
        "confidence": f.confidence,
        "reason": f.reason,
        "category": f.category,
        "flagged_message_text": f.flagged_message_text,
        "flagged_conversation_id": f.flagged_conversation_id,
        "flagged_message_id": f.flagged_message_id,
        "flagged_at": iso_utc(f.flagged_at),
        "dismissed_at": iso_utc(f.dismissed_at),
        "synced_to_cloud": f.synced_to_cloud or False,
        "last_conversation_id": info.get("last_conversation_id"),
        "last_conversation_title": info.get("last_conversation_title"),
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

        active_ids = [f.identity_id for f in flags if f.status == FLAG_ACTIVE]
        all_ids = [f.identity_id for f in flags]
        persons = await _person_ids(db, all_ids)
        flagged_conversation_ids: list[int] = []
        sender_info: dict[int, dict] = {}

        if active_ids:
            flagged_conversation_ids = [row[0] for row in (await db.execute(
                select(Message.conversation_id).where(Message.sender_identity_id.in_(active_ids)).distinct()
            )).all()]

        if all_ids:
            # Most recent message per sender (ties on date broken by id)
            latest_sub = (
                select(Message.sender_identity_id, func.max(Message.id).label("max_id"))
                .where(Message.sender_identity_id.in_(all_ids))
                .group_by(Message.sender_identity_id)
                .subquery()
            )
            latest_msgs = (await db.execute(
                select(Message).join(latest_sub, Message.id == latest_sub.c.max_id)
            )).scalars().all()
            titles = await _conversation_titles(db, {m.conversation_id for m in latest_msgs})
            for m in latest_msgs:
                sender_info[m.sender_identity_id] = {
                    "sender_name": m.sender_name,
                    "last_conversation_id": m.conversation_id,
                    "last_conversation_title": titles.get(m.conversation_id, "Unknown"),
                    "last_message_id": m.id,
                    "last_active": iso_utc(m.date),
                    "last_message": (m.text or "")[:200],
                }

        return {
            "flags": [_flag_dict(f, persons.get(f.identity_id), sender_info.get(f.identity_id, {})) for f in flags],
            # Senders with an active flag, their people, and the conversations they appear in
            "flagged_identity_ids": active_ids,
            "flagged_person_ids": list(dict.fromkeys(persons[i] for i in active_ids if i in persons)),
            "flagged_conversation_ids": flagged_conversation_ids,
        }


async def _conversation_titles(db, conversation_ids) -> dict[int, str]:
    conversation_ids = list(conversation_ids)
    if not conversation_ids:
        return {}
    rows = (await db.execute(
        select(Conversation.id, Conversation.title).where(Conversation.id.in_(conversation_ids))
    )).all()
    return {row[0]: row[1] or "Unknown" for row in rows}


async def _person_ids(db, identity_ids) -> dict[int, int]:
    """{identity_id: person_id}"""
    ids = [i for i in set(identity_ids) if i]
    if not ids:
        return {}
    return dict((await db.execute(select(Identity.id, Identity.person_id).where(Identity.id.in_(ids)))).all())


_VERDICT_FILTERS = ("scam", "not_scam", "failed", "pending", "manual", "unmarked")


def _scanned_message_filters(
    sender_identity_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    min_members: Optional[int] = None,
    verdict_filter: Optional[str] = None,
):
    """Filter conditions for Shield's scanned list. All verdict filters use the
    stored ``effective_is_scam`` / ``scam_status``, never raw score checks."""
    conditions = [scanned_condition()]
    if sender_identity_id is not None:
        conditions.append(Message.sender_identity_id == sender_identity_id)
    if conversation_id is not None:
        conditions.append(Message.conversation_id == conversation_id)
    if min_members is not None:
        conditions.append(Message.conversation_id.in_(
            select(Conversation.id).where(Conversation.members_count >= min_members)
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


def _sender_person_id(m: Message) -> Optional[int]:
    return m.sender_identity.person_id if m.sender_identity is not None else None


def _serialize_scanned_message(m: Message, conversation_title: str) -> dict:
    return {
        "message_id": m.id,
        "conversation_id": m.conversation_id,
        "conversation_title": conversation_title,
        "sender_identity_id": m.sender_identity_id,
        "sender_person_id": _sender_person_id(m),
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
    sender_identity_id: Optional[int] = Query(None),
    conversation_id: Optional[int] = Query(None),
    min_members: Optional[int] = Query(None),
    verdict_filter: Optional[str] = Query(None),
):
    """Scanned messages, newest first, ordered by (date, id).

    Prefer ``cursor`` (keyset; stable while new scans arrive). ``page`` is
    kept for compatibility and is ignored when a cursor is given.
    """
    async with AsyncSessionLocal() as db:
        conditions = _scanned_message_filters(sender_identity_id, conversation_id, min_members, verdict_filter)
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
        titles = await _conversation_titles(db, {m.conversation_id for m in msgs})

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "next_cursor": _encode_cursor(msgs[-1]) if has_more and msgs else None,
            "messages": [_serialize_scanned_message(m, titles.get(m.conversation_id, "Unknown")) for m in msgs],
        }


class MessageRef(BaseModel):
    message_id: int
    conversation_id: Optional[int] = None  # informational; message ids are unique


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
        messages=[m.message_id for m in req.messages],
    )
    return {"status": "updated", **result.to_dict()}


class SenderVerdictRequest(BaseModel):
    verdict: Optional[str] = None  # "scam" flags, "not_scam" dismisses, null resets


async def _sender_verdict(identity_id: int, verdict: Optional[str]):
    try:
        return await set_verdict(scope="sender", verdict=verdict, identity_id=identity_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Sender not found")


@router.put("/senders/{identity_id}/verdict")
async def set_sender_verdict(identity_id: int, req: SenderVerdictRequest):
    """Sender-level verdict: flag, dismiss, or reset a sender identity."""
    _verdict_or_400(req.verdict)
    result = await _sender_verdict(identity_id, req.verdict)
    return {"status": "updated", **result.to_dict()}


@router.delete("/flags/{identity_id}")
async def dismiss_flag(identity_id: int):
    """Dismiss a flagged sender (= sender verdict 'not_scam')."""
    result = await _sender_verdict(identity_id, "not_scam")
    return {"status": "dismissed", "identity_id": identity_id, **result.to_dict()}


@router.delete("/scanned-messages/{message_id}")
async def clear_message_scam(message_id: int):
    """Mark one message not scam (= message verdict 'not_scam'). Scores are kept."""
    result = await set_verdict(scope="message", verdict="not_scam", messages=[message_id])
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
    sender_identity_id: Optional[int] = Query(None),
    conversation_id: Optional[int] = Query(None),
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
    conditions = _scanned_message_filters(sender_identity_id, conversation_id, min_members, verdict_filter)

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
                    titles = await _conversation_titles(db, {m.conversation_id for m in msgs})
                if not msgs:
                    break
                cursor = (msgs[-1].date, msgs[-1].id)
                for m in msgs:
                    row = {
                        "sender_identity_id": m.sender_identity_id,
                        "sender_person_id": _sender_person_id(m),
                        "sender_name": m.sender_name,
                        "conversation_id": m.conversation_id,
                        "conversation_title": titles.get(m.conversation_id, "Unknown"),
                        "message_id": m.id,
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


@router.get("/status/{conversation_id}")
async def conversation_scam_status(conversation_id: int):
    """Scam status for a conversation: effective-scam messages and active flagged senders."""
    async with AsyncSessionLocal() as db:
        flagged_msgs = (await db.execute(
            select(Message.id).where(Message.conversation_id == conversation_id, Message.effective_is_scam)
        )).all()
        flagged_users = (await db.execute(
            select(ScamFlag).where(
                ScamFlag.status == FLAG_ACTIVE,
                ScamFlag.identity_id.in_(
                    select(Message.sender_identity_id).where(
                        Message.conversation_id == conversation_id, Message.sender_identity_id.isnot(None),
                    )
                ),
            )
        )).scalars().all()
        persons = await _person_ids(db, [f.identity_id for f in flagged_users])

        return {
            "has_scam_flags": bool(flagged_msgs) or bool(flagged_users),
            "flagged_message_count": len(flagged_msgs),
            "flagged_message_ids": [row[0] for row in flagged_msgs],
            "flagged_users": [
                {"identity_id": f.identity_id, "person_id": persons.get(f.identity_id),
                 "confidence": f.confidence, "reason": f.reason, "source": f.source}
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
                "related_conversation_id": r.related_conversation_id,
                "related_message_ids": json.loads(r.related_message_ids_json or "[]"),
                "related_identity_ids": json.loads(r.related_identity_ids_json or "[]"),
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
    """Sync status summary — counts of synced vs unsynced items. Flags and
    scans count the rows the current (Telegram-only) cloud contract uploads."""
    from backend.scam.cloud_sync import telegram_flag_condition, telegram_message_condition

    async with AsyncSessionLocal() as db:
        flags_total = (await db.execute(
            select(func.count()).select_from(ScamFlag).where(telegram_flag_condition())
        )).scalar() or 0
        flags_synced = (await db.execute(
            select(func.count()).select_from(ScamFlag).where(
                telegram_flag_condition(), ScamFlag.synced_to_cloud == True,  # noqa: E712
            )
        )).scalar() or 0

        reports_total = (await db.execute(select(func.count()).select_from(ScamReport))).scalar() or 0
        reports_synced = (await db.execute(
            select(func.count()).select_from(ScamReport).where(ScamReport.synced_at.isnot(None))
        )).scalar() or 0

        # Same row set the uploader sends (see cloud_sync._upload_scan_results)
        syncable = and_(
            telegram_message_condition(),
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

        # Skipped dismissals left the queue without an upload (no contract carries them)
        dismissals_by_status = dict((await db.execute(
            select(ScamDismissal.cloud_status, func.count()).group_by(ScamDismissal.cloud_status)
        )).all())
        dismissals_total = sum(dismissals_by_status.values())

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
            "synced": dismissals_by_status.get("synced", 0),
            "pending": dismissals_by_status.get("pending", 0),
            "skipped": dismissals_by_status.get("skipped", 0),
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
    reported_identity_id: int
    reported_in_conversation_id: int
    reporter_verdict: str = "scam"
    scam_category: Optional[str] = None
    custom_category: Optional[str] = None
    included_message_ids: list[int]  # message ids to include


def _report_context_entry(m: Message) -> dict:
    return {
        "message_id": m.id,
        "sender_identity_id": m.sender_identity_id,
        "sender_person_id": _sender_person_id(m),
        "sender_name": m.sender_name,
        "text": m.text,
        "date": iso_utc(m.date),
        "is_outgoing": m.is_outgoing,
        **scam_fields(m),
    }


@router.get("/reports/context")
async def get_report_context(
    conversation_id: int = Query(...),
    target_message_id: int = Query(...),
):
    """Get surrounding messages for building a report."""
    async with AsyncSessionLocal() as db:
        target = (await db.execute(
            select(Message).where(Message.conversation_id == conversation_id, Message.id == target_message_id)
        )).scalars().first()
        if not target:
            raise HTTPException(status_code=404, detail="Target message not found")

        # 10 messages before and 5 after by date
        before_msgs = (await db.execute(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.date <= target.date,
                Message.id != target.id,
            ).order_by(Message.date.desc(), Message.id.desc()).limit(10)
        )).scalars().all()
        after_msgs = (await db.execute(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.date > target.date,
            ).order_by(Message.date.asc(), Message.id.asc()).limit(5)
        )).scalars().all()

        all_msgs = list(reversed(before_msgs)) + [target] + list(after_msgs)
        return {"messages": [_report_context_entry(m) for m in all_msgs]}


@router.post("/reports")
async def create_report(req: CreateReportRequest):
    """Submit a scam report. Stored with internal ids; cloud sync resolves the
    channel ids on upload."""
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, req.reported_in_conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if await db.get(Identity, req.reported_identity_id) is None:
            raise HTTPException(status_code=404, detail="Sender not found")

        included_msgs = (await db.execute(
            select(Message).where(
                Message.conversation_id == conv.id,
                Message.id.in_(req.included_message_ids),
            ).order_by(Message.date.asc(), Message.id.asc())
        )).scalars().all()
        included_ids = {m.id for m in included_msgs}

        # messages_json covers the whole window so omitted positions show;
        # only included messages carry their text
        if included_msgs:
            min_date = min(m.date for m in included_msgs if m.date)
            max_date = max(m.date for m in included_msgs if m.date)
            context_msgs = (await db.execute(
                select(Message).where(
                    Message.conversation_id == conv.id,
                    Message.date >= min_date,
                    Message.date <= max_date,
                ).order_by(Message.date.asc(), Message.id.asc())
            )).scalars().all()
        else:
            context_msgs = []

        messages_data = []
        for i, m in enumerate(context_msgs):
            is_reported = m.sender_identity_id == req.reported_identity_id
            is_included = m.id in included_ids
            entry: dict = {
                "position": i,
                "identity_id": m.sender_identity_id,
                "role": "reported" if is_reported else ("reporter" if m.is_outgoing else "bystander"),
                "included": is_included,
            }
            if is_included:
                entry["sender_name"] = m.sender_name
                entry["text"] = m.text
                entry["date"] = iso_utc(m.date)
            messages_data.append(entry)

        # Local detection info if any message was scanned or marked
        local_detection = None
        scam_msgs = [m for m in included_msgs if m.scam_status is not None or m.scam_manual_verdict is not None]
        if scam_msgs:
            local_detection = {
                "scores": [
                    {"message_id": m.id, "scam_score": m.scam_score,
                     "scam_reason": m.scam_reason, "scam_category": m.scam_category,
                     "scam_status": m.scam_status, "manual_verdict": m.scam_manual_verdict,
                     "effective_is_scam": bool(m.effective_is_scam)}
                    for m in scam_msgs
                ],
            }

        now = datetime.utcnow()
        report = ScamReport(
            report_id=str(uuid.uuid4()),
            reported_identity_id=req.reported_identity_id,
            reported_in_conversation_id=conv.id,
            chat_type=conv.kind,  # conversation kind
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
        reports = (await db.execute(
            select(ScamReport).order_by(ScamReport.created_at.desc()).limit(100)
        )).scalars().all()

        titles = await _conversation_titles(db, {r.reported_in_conversation_id for r in reports})
        sender_ids = list({r.reported_identity_id for r in reports if r.reported_identity_id})
        identities: dict[int, Identity] = {}
        sender_names: dict[int, str] = {}
        if sender_ids:
            identities = {i.id: i for i in (await db.execute(
                select(Identity).where(Identity.id.in_(sender_ids))
            )).scalars()}
            rows = (await db.execute(
                select(Message.sender_identity_id, Message.sender_name)
                .where(Message.sender_identity_id.in_(sender_ids), Message.sender_name.isnot(None))
                .order_by(Message.date.desc())
            )).all()
            for iid, name in rows:
                if iid not in sender_names and name:
                    sender_names[iid] = name

        def _sender_name(iid: Optional[int]) -> Optional[str]:
            ident = identities.get(iid)
            return sender_names.get(iid) or (ident.display_name or ident.username if ident is not None else None)

        return {
            "reports": [
                {
                    "id": r.id,
                    "report_id": r.report_id,
                    "reported_identity_id": r.reported_identity_id,
                    "reported_person_id": (
                        identities[r.reported_identity_id].person_id if r.reported_identity_id in identities else None
                    ),
                    "reported_sender_name": _sender_name(r.reported_identity_id),
                    "reported_in_conversation_id": r.reported_in_conversation_id,
                    "conversation_title": titles.get(r.reported_in_conversation_id, "Unknown"),
                    "conversation_kind": r.chat_type,
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
    related_conversation_id: Optional[int] = None
    related_message_id: Optional[int] = None
    related_flag_identity_id: Optional[int] = None


@router.post("/feedback")
async def submit_feedback(req: CreateFeedbackRequest):
    """Submit user feedback about scam detection."""
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Feedback message cannot be empty")

    now = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        for model, ref_id, what in (
            (Conversation, req.related_conversation_id, "Conversation"),
            (Message, req.related_message_id, "Message"),
            (Identity, req.related_flag_identity_id, "Sender"),
        ):
            if ref_id is not None and await db.get(model, ref_id) is None:
                raise HTTPException(status_code=404, detail=f"{what} not found")
        feedback = ScamFeedback(
            feedback_type=req.feedback_type,
            message=req.message.strip(),
            related_conversation_id=req.related_conversation_id,
            related_message_id=req.related_message_id,
            related_flag_identity_id=req.related_flag_identity_id,
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
                    "related_conversation_id": f.related_conversation_id,
                    "related_message_id": f.related_message_id,
                    "related_flag_identity_id": f.related_flag_identity_id,
                    "status": f.status,
                    "synced_at": iso_utc(f.synced_at),
                    "created_at": iso_utc(f.created_at),
                }
                for f in items
            ],
        }
