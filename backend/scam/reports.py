"""Writes from the Shield screens: scam reports and feedback (uploaded by
cloud sync; feedback is pushed right away when the cloud is configured)."""

import json
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, Identity, Message, ScamFeedback, ScamReport
from backend.errors import InvalidRequest, NotFound
from backend.scam.analytics import get_analytics_tracker
from backend.scam import cloud_sync
from backend.serialization import iso_utc


async def create_report(*, reported_identity_id: int, reported_in_conversation_id: int, reporter_verdict: str,
                        scam_category: Optional[str], custom_category: Optional[str],
                        included_message_ids: list[int]) -> dict:
    """Submit a scam report. Stored with internal ids; cloud sync resolves the
    channel ids on upload."""
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, reported_in_conversation_id)
        if conv is None:
            raise NotFound("Conversation not found")
        if await db.get(Identity, reported_identity_id) is None:
            raise NotFound("Sender not found")

        included_msgs = (await db.execute(
            select(Message).where(
                Message.conversation_id == conv.id,
                Message.id.in_(included_message_ids),
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
            is_reported = m.sender_identity_id == reported_identity_id
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
            reported_identity_id=reported_identity_id,
            reported_in_conversation_id=conv.id,
            chat_type=conv.kind,  # conversation kind
            reporter_verdict=reporter_verdict,
            scam_category=scam_category,
            custom_category=custom_category,
            messages_json=json.dumps(messages_data, default=str),
            local_detection_json=json.dumps(local_detection, default=str) if local_detection else None,
            status="submitted",
            created_at=now,
            updated_at=now,
        )
        db.add(report)
        await db.commit()

        await get_analytics_tracker().record_report_submitted()

        return {
            "report_id": report.report_id,
            "status": report.status,
            "created_at": iso_utc(report.created_at),
        }


async def submit_feedback(*, feedback_type: str, message: str, related_conversation_id: Optional[int],
                          related_message_id: Optional[int], related_flag_identity_id: Optional[int]) -> dict:
    """Submit user feedback about scam detection."""
    if not message.strip():
        raise InvalidRequest("Feedback message cannot be empty")

    now = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        for model, ref_id, what in (
            (Conversation, related_conversation_id, "Conversation"),
            (Message, related_message_id, "Message"),
            (Identity, related_flag_identity_id, "Sender"),
        ):
            if ref_id is not None and await db.get(model, ref_id) is None:
                raise NotFound(f"{what} not found")
        feedback = ScamFeedback(
            feedback_type=feedback_type,
            message=message.strip(),
            related_conversation_id=related_conversation_id,
            related_message_id=related_message_id,
            related_flag_identity_id=related_flag_identity_id,
            status="pending",
            created_at=now,
        )
        db.add(feedback)
        await db.commit()
        await db.refresh(feedback)

    # Try to sync immediately if cloud is configured
    synced = False
    sync_error = None
    sync_mgr = cloud_sync.get_cloud_sync_manager()
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
