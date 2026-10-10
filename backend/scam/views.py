"""Read models of the Shield screens: flagged senders, scanned messages
(paged and exported), per-conversation status, recent requests, the cloud
sync summary, report context and the report / feedback lists.

Filters use the stored ``effective_is_scam`` / ``scam_status``
(``backend.scam.verdict``), never raw scores. Invalid parameters raise
``InvalidRequest`` (400); missing rows ``NotFound`` (404).
"""

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import and_, func, or_, select

from backend.db.database import AsyncSessionLocal
from backend.db.models import (
    SCAM_SUCCESS_STATUSES, Conversation, Identity, Message, ScamAnalytics, ScamCheckRequest, ScamDismissal,
    ScamFeedback, ScamFlag, ScamReport,
)
from backend.errors import InvalidRequest, NotFound
from backend.scam.verdict import FLAG_ACTIVE, not_pending_condition, scanned_condition
from backend.serialization import iso_utc
from backend.serializers import scam_fields
from backend.scam.cloud_payloads import telegram_flag_condition, telegram_message_condition


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
        raise InvalidRequest(f"verdict_filter must be one of {', '.join(_VERDICT_FILTERS)}")
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
        raise InvalidRequest("Invalid cursor")


def _keyset_after(cursor: str):
    date, mid = _decode_cursor(cursor)
    return or_(Message.date < date, and_(Message.date == date, Message.id < mid))


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


async def _count(db, *conditions) -> int:
    return (await db.execute(select(func.count()).select_from(Message).where(*conditions))).scalar() or 0


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


async def flags_overview(include_dismissed: bool = False) -> dict:
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


async def scanned_messages_page(*, page: int, page_size: int, cursor: Optional[str], sender_identity_id: Optional[int],
                                conversation_id: Optional[int], min_members: Optional[int],
                                verdict_filter: Optional[str]) -> dict:
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


EXPORT_BATCH = 1000  # rows read per session while exporting


async def export_rows(*, sender_identity_id: Optional[int], conversation_id: Optional[int],
                      min_members: Optional[int], verdict_filter: Optional[str]):
    """Every matching scanned message as an export row, newest first, read in
    keyset batches (one short session per batch)."""
    conditions = _scanned_message_filters(sender_identity_id, conversation_id, min_members, verdict_filter)
    cursor = None
    while True:
        async with AsyncSessionLocal() as db:
            q = select(Message).where(*conditions)
            if cursor is not None:
                q = q.where(or_(Message.date < cursor[0], and_(Message.date == cursor[0], Message.id < cursor[1])))
            msgs = (await db.execute(
                q.order_by(Message.date.desc(), Message.id.desc()).limit(EXPORT_BATCH)
            )).scalars().all()
            titles = await _conversation_titles(db, {m.conversation_id for m in msgs})
        if not msgs:
            return
        cursor = (msgs[-1].date, msgs[-1].id)
        for m in msgs:
            yield {
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


async def conversation_status(conversation_id: int):
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


async def recent_requests(status: Optional[str] = None):
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


async def sync_summary():
    """Sync status summary — counts of synced vs unsynced items. Flags and
    scans count the rows the current (Telegram-only) cloud contract uploads."""

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

        # Same row set the uploader sends (see cloud_upload.CloudUploads._upload_scan_results)
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
        **(sync_mgr.last_sync_outcome if sync_mgr else {
            "last_sync_status": None, "last_sync_error": None, "last_sync_details": None,
        }),
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


async def report_context(conversation_id: int, target_message_id: int) -> dict:
    """Get surrounding messages for building a report."""
    async with AsyncSessionLocal() as db:
        target = (await db.execute(
            select(Message).where(Message.conversation_id == conversation_id, Message.id == target_message_id)
        )).scalars().first()
        if not target:
            raise NotFound("Target message not found")

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


async def reports_list():
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


async def feedback_list():
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
