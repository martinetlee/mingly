"""Single owner of Shield verdict state.

* ``scam_fields(m)`` is the one serialization of a message's scam state, used
  by every API response and WS event (``effective_is_scam`` comes from the
  ``Message.effective_is_scam`` hybrid property, so the UI never re-derives it).
* ``set_verdict(...)`` is the one write path for user verdicts. Every entry
  point (PUT verdict, DELETE flag, DELETE scanned message, sender verdict)
  calls it, so they all give the same end state, the same cloud-sync change
  tracking and the same analytics.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import and_, or_, select, tuple_

from backend.db.database import AsyncSessionLocal
from backend.db.models import Message, ScamDismissal, ScamFlag
from backend.prefs import get_pref
from backend.serialization import iso_utc
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

VERDICTS = ("scam", "not_scam", None)
FLAG_ACTIVE = "active"
FLAG_DISMISSED = "dismissed"


def scam_fields(m: Message) -> dict:
    """Scam state of one message as stored, plus the derived effective verdict."""
    return {
        "scam_score": m.scam_score,
        "scam_reason": m.scam_reason,
        "scam_category": m.scam_category,
        "scam_manual_verdict": m.scam_manual_verdict,
        "scam_status": m.scam_status,
        "scam_error": m.scam_error,
        "effective_is_scam": bool(m.effective_is_scam),
    }


def ws_message_entry(m: Message) -> dict:
    return {"chat_id": m.chat_id, "tg_message_id": m.tg_message_id, "sender_id": m.sender_id, **scam_fields(m)}


def scanned_condition():
    """A message belongs in Shield's scanned list once it has any scan state or
    a manual verdict (dismissed messages no longer vanish)."""
    return or_(Message.scam_status.isnot(None), Message.scam_manual_verdict.isnot(None))


def not_pending_condition():
    """``scam_status != 'pending'`` that keeps NULL-status rows (e.g. a manual
    verdict on a never-scanned message); plain != drops NULLs in SQL."""
    return or_(Message.scam_status.is_(None), Message.scam_status != "pending")


def mark_changed_values(now: Optional[datetime] = None) -> dict:
    """Column values that put a message back in the cloud upload set."""
    return {"scam_updated_at": now or datetime.utcnow(), "scam_synced_to_cloud": False}


def _sync_dismissals_enabled() -> bool:
    return bool(get_pref("shield_sync_dismissals", False))


@dataclass
class VerdictResult:
    scope: str
    verdict: Optional[str]
    messages: list = field(default_factory=list)  # ws_message_entry dicts
    telegram_ids: list = field(default_factory=list)  # senders whose flag state changed
    flag_status: dict = field(default_factory=dict)  # telegram_id -> active|dismissed|None

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "verdict": self.verdict,
            "count": len(self.messages) if self.scope == "message" else len(self.telegram_ids),
            "messages": self.messages,
            "flags": [
                {"telegram_id": tid, "status": self.flag_status.get(tid)} for tid in self.telegram_ids
            ],
        }


def _flag_snapshot(flag: ScamFlag) -> dict:
    return {
        "telegram_id": flag.telegram_id,
        "source": flag.source,
        "confidence": flag.confidence,
        "reason": flag.reason,
        "category": flag.category,
        "flagged_message_text": flag.flagged_message_text,
        "flagged_at": iso_utc(flag.flagged_at),
    }


def _scan_snapshot(m: Message) -> dict:
    trace = None
    if m.scam_trace_json:
        try:
            trace = json.loads(m.scam_trace_json)
        except (ValueError, TypeError):
            trace = None
    return {
        "chat_id": m.chat_id,
        "tg_message_id": m.tg_message_id,
        "sender_id": m.sender_id,
        "scam_score": m.scam_score,
        "scam_reason": m.scam_reason,
        "scam_category": m.scam_category,
        "llm_prompt": m.scam_llm_prompt,
        "llm_response": m.scam_llm_response,
        "trace": trace,
    }


async def set_verdict(
    *,
    scope: str,
    verdict: Optional[str],
    messages: Optional[Iterable[tuple]] = None,
    telegram_id: Optional[int] = None,
) -> VerdictResult:
    """Apply a user verdict.

    scope="message": set ``scam_manual_verdict`` on each (chat_id, tg_message_id).
      Scores are never touched. 'scam' activates the sender's flag (explicit
      user intent); 'not_scam' dismisses an automated sender flag once that
      sender has no remaining effective-scam messages, so "mark not scam" on a
      sender's only flagged message equals "dismiss".
    scope="sender": 'not_scam' persists a dismissed per-sender record (which
      automated flagging respects) and marks that sender's *unmarked*
      pipeline-scam messages not_scam (existing manual verdicts are never
      overwritten); 'scam' activates the flag; None deletes the record.
    """
    if verdict not in VERDICTS:
        raise ValueError("verdict must be 'scam', 'not_scam' or null")
    if scope == "message":
        result = await _set_message_verdict(list(messages or []), verdict)
    elif scope == "sender":
        if telegram_id is None:
            raise ValueError("telegram_id is required for scope=sender")
        result = await _set_sender_verdict(telegram_id, verdict)
    else:
        raise ValueError("scope must be 'message' or 'sender'")

    await ws_manager.broadcast({"type": "scam_verdict_changed", **result.to_dict()})
    return result


async def _set_message_verdict(keys: list[tuple], verdict: Optional[str]) -> VerdictResult:
    keys = list({(int(c), int(t)) for c, t in keys})
    result = VerdictResult(scope="message", verdict=verdict)
    if not keys:
        return result
    now = datetime.utcnow()
    unflagged = 0
    dismissals: list[dict] = []
    sync_dismissals = _sync_dismissals_enabled()

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Message).where(tuple_(Message.chat_id, Message.tg_message_id).in_(keys))
        )).scalars().all()
        for m in rows:
            was_scam = bool(m.effective_is_scam)
            m.scam_manual_verdict = verdict
            m.scam_updated_at = now
            m.scam_synced_to_cloud = False
            if was_scam and not m.effective_is_scam:
                unflagged += 1
                if sync_dismissals and m.scam_status is not None:
                    dismissals.append(_scan_snapshot(m))
        await db.flush()

        sender_ids = {m.sender_id for m in rows if m.sender_id}
        for sid in sender_ids:
            flag = (await db.execute(select(ScamFlag).where(ScamFlag.telegram_id == sid))).scalars().first()
            if verdict == "scam":
                trigger = next(m for m in rows if m.sender_id == sid)
                flag = _activate_flag(db, flag, sid, now, source="manual", trigger=trigger)
                result.telegram_ids.append(sid)
                result.flag_status[sid] = flag.status
            elif verdict == "not_scam" and flag is not None and flag.status == FLAG_ACTIVE and flag.source != "manual":
                remaining = (await db.execute(
                    select(Message.id).where(Message.sender_id == sid, Message.effective_is_scam).limit(1)
                )).first()
                if remaining is None:
                    if sync_dismissals:
                        dismissals.append({"__flag__": True, **_flag_snapshot(flag)})
                    _dismiss_flag(flag, now)
                    result.telegram_ids.append(sid)
                    result.flag_status[sid] = flag.status

        for d in dismissals:
            is_flag = d.pop("__flag__", False)
            db.add(ScamDismissal(
                dismissal_type="scammer" if is_flag else "scan",
                payload_json=json.dumps(d, default=str),
                created_at=now,
            ))
        await db.commit()
        result.messages = [ws_message_entry(m) for m in rows]

    await _record_analytics(messages_unflagged=unflagged,
                            flags_dismissed=sum(1 for s in result.flag_status.values() if s == FLAG_DISMISSED))
    return result


async def _set_sender_verdict(telegram_id: int, verdict: Optional[str]) -> VerdictResult:
    now = datetime.utcnow()
    result = VerdictResult(scope="sender", verdict=verdict, telegram_ids=[telegram_id])
    dismissed = False
    async with AsyncSessionLocal() as db:
        flag = (await db.execute(select(ScamFlag).where(ScamFlag.telegram_id == telegram_id))).scalars().first()
        if verdict is None:
            if flag is not None:
                await db.delete(flag)
            result.flag_status[telegram_id] = None
        elif verdict == "scam":
            flag = _activate_flag(db, flag, telegram_id, now, source="manual")
            result.flag_status[telegram_id] = flag.status
        else:
            if flag is None:
                flag = ScamFlag(telegram_id=telegram_id, source="manual", flagged_at=now, synced_to_cloud=False)
                db.add(flag)
            elif flag.status == FLAG_ACTIVE:
                dismissed = True
                if _sync_dismissals_enabled():
                    db.add(ScamDismissal(
                        dismissal_type="scammer",
                        payload_json=json.dumps(_flag_snapshot(flag), default=str),
                        created_at=now,
                    ))
            _dismiss_flag(flag, now)
            result.flag_status[telegram_id] = flag.status

            # Clear this sender's red banners without overriding any verdict
            # the user already set by hand and without touching scores.
            targets = (await db.execute(
                select(Message).where(
                    Message.sender_id == telegram_id,
                    Message.scam_manual_verdict.is_(None),
                    Message.effective_is_scam,
                )
            )).scalars().all()
            for m in targets:
                m.scam_manual_verdict = "not_scam"
                m.scam_updated_at = now
                m.scam_synced_to_cloud = False
            result.messages = [ws_message_entry(m) for m in targets]
        await db.commit()

    await _record_analytics(flags_dismissed=1 if dismissed else 0,
                            messages_unflagged=len(result.messages))
    return result


def _activate_flag(db, flag: Optional[ScamFlag], telegram_id: int, now: datetime,
                   *, source: str, trigger: Optional[Message] = None) -> ScamFlag:
    if flag is None:
        flag = ScamFlag(telegram_id=telegram_id, source=source, flagged_at=now)
        db.add(flag)
    flag.status = FLAG_ACTIVE
    flag.source = source
    flag.dismissed_at = None
    flag.updated_at = now
    flag.synced_to_cloud = False
    if trigger is not None and not flag.flagged_message_id:
        flag.flagged_message_text = (trigger.text or "")[:500]
        flag.flagged_message_chat_id = trigger.chat_id
        flag.flagged_message_id = trigger.tg_message_id
    return flag


def _dismiss_flag(flag: ScamFlag, now: datetime) -> None:
    flag.status = FLAG_DISMISSED
    flag.dismissed_at = now
    flag.updated_at = now
    flag.synced_to_cloud = False


async def _record_analytics(*, messages_unflagged: int = 0, flags_dismissed: int = 0) -> None:
    from backend.scam.analytics import get_analytics_tracker

    counters = {}
    if messages_unflagged:
        counters["messages_unflagged"] = messages_unflagged
    if flags_dismissed:
        counters["flags_dismissed"] = flags_dismissed
    if counters:
        try:
            await get_analytics_tracker().increment(**counters)
        except Exception:
            logger.exception("Failed to record verdict analytics")


def unscanned_condition(now: Optional[datetime] = None):
    """Rows with no successful scan that are due: never scanned, stale
    'pending' from a crash, or failed with the retry backoff elapsed."""
    now = now or datetime.utcnow()
    return and_(
        Message.scam_manual_verdict.is_(None),
        or_(
            Message.scam_status.is_(None),
            Message.scam_status == "pending",
            and_(
                Message.scam_status == "failed",
                or_(Message.scam_next_retry_at.is_(None), Message.scam_next_retry_at <= now),
            ),
        ),
    )

