"""Shield (scam detection) API endpoints.

Verdict semantics live in ``backend.scam.verdict``: every response carries
``scam_status`` and the derived ``effective_is_scam``, and every verdict
change goes through ``set_verdict``. Checks are queued through
``backend.scam.queue``; results arrive as exactly one terminal WS event per
message (``scam_detected`` | ``scam_check_safe`` | ``scam_check_error``).
Read models are ``backend.scam.views``; reports and feedback
``backend.scam.reports``.
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
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.config import DATA_DIR
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.scam import reports, views
from backend.scam.analytics import get_analytics_tracker
from backend.scam import cloud_sync, pipeline
from backend.scam.queue import enqueue_targets, get_scan_queue, scan_unscanned_messages
from backend.scam.service import get_scam_service
from backend.scam.verdict import set_verdict
from backend.serialization import iso_utc
from backend.tasks import spawn

logger = logging.getLogger(__name__)

router = APIRouter()

_EXPORTS_DIR = DATA_DIR / "exports"
_EXPORT_TTL_SECONDS = 24 * 3600


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
        stored = await crud.get_messages_by_ids(db, ids)
    found = {mid for mid, m in stored.items() if m.conversation_id == req.conversation_id}
    if not found:
        raise HTTPException(status_code=404, detail="No messages found")

    queue = get_scan_queue()
    already = [i for i in ids if i in found and queue.is_busy(req.conversation_id, i)]
    await enqueue_targets(req.conversation_id, [i for i in ids if i in found], source="manual")
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
    await svc.handle_api_response(request_id, payload.model_dump(exclude_none=True))
    return {"status": "received"}


@router.get("/flags")
async def list_flags(include_dismissed: bool = Query(False)):
    """List flagged senders (status 'active'; dismissed ones on request) with last-active info."""
    return await views.flags_overview(include_dismissed)


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
    return await views.scanned_messages_page(
        page=page, page_size=page_size, cursor=cursor, sender_identity_id=sender_identity_id,
        conversation_id=conversation_id, min_members=min_members, verdict_filter=verdict_filter,
    )


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
    rows = views.export_rows(
        sender_identity_id=sender_identity_id, conversation_id=conversation_id,
        min_members=min_members, verdict_filter=verdict_filter,
    )
    _EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_exports()
    filename = f"shield_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.json"
    filepath = _EXPORTS_DIR / filename
    tmp = _EXPORTS_DIR / f".{filename}.tmp"
    count = 0
    try:
        with open(tmp, "w", encoding="utf-8") as out:
            out.write("[")
            async for row in rows:
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
    return await views.conversation_status(conversation_id)


@router.get("/requests")
async def list_requests(status: Optional[str] = None):
    """List scam check requests, optionally filtered by status."""
    return await views.recent_requests(status)


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
    mgr = pipeline.get_pipeline_manager()
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
    mgr = pipeline.get_pipeline_manager()
    if not mgr:
        raise HTTPException(status_code=503, detail="Pipeline not initialized")
    config = mgr.reload()
    return {"status": "reloaded", "version": config.version}


@router.post("/pipeline/sync")
async def sync_pipeline():
    """Run cloud sync (joining one already in progress), or reload from disk."""
    sync_mgr = cloud_sync.get_cloud_sync_manager()
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
    mgr = pipeline.get_pipeline_manager()
    if mgr:
        config = mgr.reload()
        return {"status": "reloaded", "version": config.version}
    return {"status": "no_pipeline"}


@router.get("/sync/status")
async def sync_status():
    """Get cloud sync configuration and status."""
    sync_mgr = cloud_sync.get_cloud_sync_manager()
    mgr = pipeline.get_pipeline_manager()
    return {
        "configured": sync_mgr.is_configured if sync_mgr else False,
        "cloud_url": sync_mgr.base_url if sync_mgr else "",
        "url_error": sync_mgr.url_error if sync_mgr else None,
        "running": sync_mgr.is_running if sync_mgr else False,
        "last_synced_at": iso_utc(sync_mgr.last_synced_at) if sync_mgr else None,
        "pipeline_version": mgr.get_config().version if mgr else None,
    }


@router.get("/sync/history")
async def sync_history():
    """Sync status summary — counts of synced vs unsynced items. Flags and
    scans count the rows the current (Telegram-only) cloud contract uploads."""
    return await views.sync_summary()


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


@router.get("/reports/context")
async def get_report_context(
    conversation_id: int = Query(...),
    target_message_id: int = Query(...),
):
    """Get surrounding messages for building a report."""
    return await views.report_context(conversation_id, target_message_id)


@router.post("/reports")
async def create_report(req: CreateReportRequest):
    """Submit a scam report. Stored with internal ids; cloud sync resolves the
    channel ids on upload."""
    return await reports.create_report(
        reported_identity_id=req.reported_identity_id, reported_in_conversation_id=req.reported_in_conversation_id,
        reporter_verdict=req.reporter_verdict, scam_category=req.scam_category,
        custom_category=req.custom_category, included_message_ids=req.included_message_ids,
    )


@router.get("/reports")
async def list_reports():
    """List all scam reports."""
    return await views.reports_list()


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
    return await reports.submit_feedback(
        feedback_type=req.feedback_type, message=req.message, related_conversation_id=req.related_conversation_id,
        related_message_id=req.related_message_id, related_flag_identity_id=req.related_flag_identity_id,
    )


@router.get("/feedback")
async def list_feedback():
    """List all submitted feedback."""
    return await views.feedback_list()
