"""Single owner of Shield verdict state.

* ``serializers.scam_fields(m)`` is the one serialization of a message's scam
  state, used by every API response and WS event (``effective_is_scam`` comes
  from the ``Message.effective_is_scam`` hybrid property).
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

from sqlalchemy import and_, or_, select

from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, Identity, Message, ScamDismissal, ScamFlag
from backend.prefs import get_pref
from backend.scam.channel_ids import telegram_scan_ids, telegram_user_ids
from backend.serialization import iso_utc
from backend.serializers import scam_fields
from backend.ws.manager import ws_manager
from backend.scam.analytics import get_analytics_tracker

logger = logging.getLogger(__name__)

VERDICTS = ("scam", "not_scam", None)
FLAG_ACTIVE = "active"
FLAG_DISMISSED = "dismissed"



def ws_message_entry(m: Message) -> dict:
    """A message's scam state for WS events and verdict responses (v2 ids)."""
    ident = m.sender_identity
    return {
        "message_id": m.id,
        "conversation_id": m.conversation_id,
        "identity_id": m.sender_identity_id,
        "person_id": ident.person_id if ident is not None else None,
        **scam_fields(m),
    }


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
    identity_ids: list = field(default_factory=list)  # senders whose flag state changed
    flag_status: dict = field(default_factory=dict)  # identity_id -> active|dismissed|None
    person_ids: dict = field(default_factory=dict)  # identity_id -> person_id

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "verdict": self.verdict,
            "count": len(self.messages) if self.scope == "message" else len(self.identity_ids),
            "messages": self.messages,
            "flags": [
                {"identity_id": iid, "person_id": self.person_ids.get(iid), "status": self.flag_status.get(iid)}
                for iid in self.identity_ids
            ],
        }


def _flag_snapshot(flag: ScamFlag) -> dict:
    """Dismissal record of a sender flag. Internal ids; cloud sync resolves
    the channel ids when it uploads (``_add_cloud_v1_ids`` keeps Telegram's
    for when the row is gone by then)."""
    return {
        "identity_id": flag.identity_id,
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
        "conversation_id": m.conversation_id,
        "message_id": m.id,
        "identity_id": m.sender_identity_id,
        "scam_score": m.scam_score,
        "scam_reason": m.scam_reason,
        "scam_category": m.scam_category,
        "llm_prompt": m.scam_llm_prompt,
        "llm_response": m.scam_llm_response,
        "trace": trace,
    }


async def _add_cloud_v1_ids(db, dismissals: list[dict], messages: Iterable[Message]) -> None:
    """Record, while the rows still exist, the Telegram ids cloud contract v1
    identifies dismissed rows by (``cloud_v1``): scans {chat_id,
    tg_message_id, sender_id}, sender flags {telegram_id}. An upload after
    the message or sender was deleted (scammers delete their messages)
    still carries them. Other channels get nothing (v2 refs only)."""
    by_id = {m.id: m for m in messages}
    scans = [d for d in dismissals if "message_id" in d and d["message_id"] in by_id]
    by_conv: dict[int, list[Message]] = {}
    for d in scans:
        m = by_id[d["message_id"]]
        by_conv.setdefault(m.conversation_id, []).append(m)
    tg_ids = {
        cid: await telegram_scan_ids(db, await db.get(Conversation, cid), msgs) for cid, msgs in by_conv.items()
    }
    for d in scans:
        m = by_id[d["message_id"]]
        tg = tg_ids.get(m.conversation_id)
        if tg is not None:
            d["cloud_v1"] = {"chat_id": tg.peer_id, "tg_message_id": tg.message_ids[m.id], "sender_id": tg.senders[m.id]}
    flags = [d for d in dismissals if "message_id" not in d and d.get("identity_id")]
    users = await telegram_user_ids(db, [d["identity_id"] for d in flags])
    for d in flags:
        if d["identity_id"] in users:
            d["cloud_v1"] = {"telegram_id": users[d["identity_id"]]}


async def set_verdict(
    *,
    scope: str,
    verdict: Optional[str],
    messages: Optional[Iterable[int]] = None,
    identity_id: Optional[int] = None,
) -> VerdictResult:
    """Apply a user verdict.

    scope="message": set ``scam_manual_verdict`` on each message id.
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
        if identity_id is None:
            raise ValueError("identity_id is required for scope=sender")
        result = await _set_sender_verdict(identity_id, verdict)
    else:
        raise ValueError("scope must be 'message' or 'sender'")

    if result.identity_ids:
        async with AsyncSessionLocal() as db:
            result.person_ids = dict((await db.execute(
                select(Identity.id, Identity.person_id).where(Identity.id.in_(result.identity_ids))
            )).all())

    await ws_manager.broadcast({"type": "scam_verdict_changed", **result.to_dict()})
    return result


async def _set_message_verdict(message_ids: list, verdict: Optional[str]) -> VerdictResult:
    message_ids = list({int(i) for i in message_ids})
    result = VerdictResult(scope="message", verdict=verdict)
    if not message_ids:
        return result
    now = datetime.utcnow()
    unflagged = 0
    dismissals: list[dict] = []
    sync_dismissals = _sync_dismissals_enabled()

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Message).where(Message.id.in_(message_ids))
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

        sender_ids = list(dict.fromkeys(m.sender_identity_id for m in rows if m.sender_identity_id))
        for sid in sender_ids:
            flag = (await db.execute(select(ScamFlag).where(ScamFlag.identity_id == sid))).scalars().first()
            if verdict == "scam":
                trigger = next(m for m in rows if m.sender_identity_id == sid)
                flag = _activate_flag(db, flag, sid, now, source="manual", trigger=trigger)
                result.identity_ids.append(sid)
                result.flag_status[sid] = flag.status
            elif verdict == "not_scam" and flag is not None and flag.status == FLAG_ACTIVE and flag.source != "manual":
                remaining = (await db.execute(
                    select(Message.id).where(Message.sender_identity_id == sid, Message.effective_is_scam).limit(1)
                )).first()
                if remaining is None:
                    if sync_dismissals:
                        dismissals.append({"__flag__": True, **_flag_snapshot(flag)})
                    _dismiss_flag(flag, now)
                    result.identity_ids.append(sid)
                    result.flag_status[sid] = flag.status

        await _add_cloud_v1_ids(db, dismissals, rows)
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


async def _set_sender_verdict(identity_id: int, verdict: Optional[str]) -> VerdictResult:
    now = datetime.utcnow()
    result = VerdictResult(scope="sender", verdict=verdict, identity_ids=[identity_id])
    dismissed = False
    async with AsyncSessionLocal() as db:
        if await db.get(Identity, identity_id) is None:
            raise LookupError(f"identity {identity_id} not found")
        flag = (await db.execute(select(ScamFlag).where(ScamFlag.identity_id == identity_id))).scalars().first()
        if verdict is None:
            if flag is not None:
                await db.delete(flag)
            result.flag_status[identity_id] = None
        elif verdict == "scam":
            flag = _activate_flag(db, flag, identity_id, now, source="manual")
            result.flag_status[identity_id] = flag.status
        else:
            if flag is None:
                flag = ScamFlag(identity_id=identity_id, source="manual", flagged_at=now, synced_to_cloud=False)
                db.add(flag)
            elif flag.status == FLAG_ACTIVE:
                dismissed = True
                if _sync_dismissals_enabled():
                    snapshot = _flag_snapshot(flag)
                    await _add_cloud_v1_ids(db, [snapshot], [])
                    db.add(ScamDismissal(
                        dismissal_type="scammer",
                        payload_json=json.dumps(snapshot, default=str),
                        created_at=now,
                    ))
            _dismiss_flag(flag, now)
            result.flag_status[identity_id] = flag.status

            # Clear this sender's red banners without overriding any verdict
            # the user already set by hand and without touching scores.
            targets = (await db.execute(
                select(Message).where(
                    Message.sender_identity_id == identity_id,
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


def _activate_flag(db, flag: Optional[ScamFlag], identity_id: int, now: datetime,
                   *, source: str, trigger: Optional[Message] = None) -> ScamFlag:
    if flag is None:
        flag = ScamFlag(identity_id=identity_id, source=source, flagged_at=now)
        db.add(flag)
    flag.status = FLAG_ACTIVE
    flag.source = source
    flag.dismissed_at = None
    flag.updated_at = now
    flag.synced_to_cloud = False
    if trigger is not None and not flag.flagged_message_id:
        flag.flagged_message_text = (trigger.text or "")[:500]
        flag.flagged_conversation_id = trigger.conversation_id
        flag.flagged_message_id = trigger.id
    return flag


def _dismiss_flag(flag: ScamFlag, now: datetime) -> None:
    flag.status = FLAG_DISMISSED
    flag.dismissed_at = now
    flag.updated_at = now
    flag.synced_to_cloud = False


async def _record_analytics(*, messages_unflagged: int = 0, flags_dismissed: int = 0) -> None:

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

