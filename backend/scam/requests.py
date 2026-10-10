"""Building a ``ScanRequest`` from stored rows: the targets, recent context
from the same senders, sender profiles (with hints the live channel client
passed along) and the conversation metadata."""

import logging
from collections import OrderedDict
from typing import Optional

from sqlalchemy import or_, select

from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, Identity, Message
from backend.features import shield_enabled
from backend.scam.channel_ids import (
    TELEGRAM, TelegramScanIds, telegram_scan_ids, telegram_sender_condition, telegram_user_ids,
)
from backend.scam.scan import ScanRequest

logger = logging.getLogger(__name__)


_DEFAULT_CONTEXT = 5


_profile_hints: "OrderedDict[int, dict]" = OrderedDict()


_MAX_HINTS = 2000


def note_sender_profile(identity_id: int, *, bio: Optional[str] = None, is_contact: Optional[bool] = None) -> None:
    """Optional: remember what the channel told us about a sender so the next
    scan of their message can include it, ahead of the stored identity."""
    if not identity_id or not shield_enabled():
        return
    hint = _profile_hints.pop(identity_id, {})
    if bio is not None:
        hint["bio"] = bio
    if is_contact is not None:
        hint["is_contact"] = is_contact
    _profile_hints[identity_id] = hint
    while len(_profile_hints) > _MAX_HINTS:
        _profile_hints.popitem(last=False)


def _msg_dict(m: Message, tg_users: dict[int, int], tg: Optional[TelegramScanIds]) -> dict:
    d = {
        "message_id": m.id,
        "identity_id": m.sender_identity_id,
        "sender_name": m.sender_name,
        "text": m.text,
        "date": m.date.isoformat() if m.date else None,
    }
    if m.sender_identity_id in tg_users:
        d["telegram_user_id"] = tg_users[m.sender_identity_id]
    if tg is not None:
        d["telegram_message_id"] = tg.message_ids[m.id]
        d["telegram_sender_id"] = tg.senders[m.id]
    return d


def _context_limit() -> int:
    from backend.scam.pipeline import get_pipeline_manager

    mgr = get_pipeline_manager()
    if mgr is None:
        return _DEFAULT_CONTEXT
    try:
        return max(0, int(mgr.get_config().context_messages))
    except Exception:
        return _DEFAULT_CONTEXT


async def build_scan_request(conversation_id: int, message_ids: list[int]) -> Optional[ScanRequest]:
    """Build a request for ``message_ids`` from the DB. Context = each target
    sender's earlier messages in the conversation. Returns None if no target
    has text."""
    async with AsyncSessionLocal() as db:
        targets = (await db.execute(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.id.in_(list(message_ids)),
                Message.text.isnot(None),
                Message.text != "",
            ).order_by(Message.date.asc(), Message.id.asc())
        )).scalars().all()
        if not targets:
            return None

        conv = await db.get(Conversation, conversation_id)
        channel = conv.account.channel if conv is not None and conv.account is not None else None

        limit = _context_limit()
        context: list[Message] = []
        seen = {m.id for m in targets}
        tg_targets = await telegram_scan_ids(db, conv, targets) if channel == TELEGRAM else None
        if limit > 0:
            # Senders: identities, and on Telegram the chats posting as
            # themselves or as another channel (no identity, a Telegram sender)
            first_by_sender: dict = {}
            for m in targets:
                if m.sender_identity_id:
                    key: tuple = ("identity", m.sender_identity_id)
                elif tg_targets is not None and tg_targets.senders[m.id] is not None:
                    key = ("telegram", tg_targets.senders[m.id])
                else:
                    continue
                first_by_sender.setdefault(key, m)
            for (kind, sender), first in first_by_sender.items():
                earlier = select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.text.isnot(None),
                    Message.text != "",
                    or_(Message.date < first.date,
                        (Message.date == first.date) & (Message.seq < first.seq)),
                ).order_by(Message.date.desc(), Message.id.desc())
                who = (Message.sender_identity_id == sender if kind == "identity"
                       else telegram_sender_condition(conv, sender))
                rows = (await db.execute(earlier.where(who).limit(limit))).scalars().all()
                context.extend(m for m in rows if m.id not in seen)
        context.sort(key=lambda m: (m.date, m.id))

        sender_ids = list(dict.fromkeys(m.sender_identity_id for m in targets if m.sender_identity_id))
        identities = {
            i.id: i for i in (await db.execute(select(Identity).where(Identity.id.in_(sender_ids)))).scalars()
        } if sender_ids else {}
        tg_users: dict[int, int] = {}
        tg: Optional[TelegramScanIds] = None
        if channel == TELEGRAM:
            tg_users = await telegram_user_ids(db, {m.sender_identity_id for m in targets + context})
            tg = await telegram_scan_ids(db, conv, list(targets) + list(context))

    # One profile per target sender, in target order: identities, plus (on
    # Telegram) chats posting as themselves, which v1 described by their id.
    profiles = []
    seen_senders: set = set()
    for m in targets:
        sid = m.sender_identity_id
        tg_sender = tg.senders[m.id] if tg is not None else None
        key = ("identity", sid) if sid else ("telegram", tg_sender)
        if (not sid and tg_sender is None) or key in seen_senders:
            continue
        seen_senders.add(key)
        if sid:
            hint = _profile_hints.get(sid, {})
            ident = identities.get(sid)
            name = next((t.sender_name for t in targets if t.sender_identity_id == sid), None)
            profile = {
                "identity_id": sid,
                "person_id": ident.person_id if ident is not None else None,
                "name": name or (ident.display_name if ident is not None else None),
                "bio": hint.get("bio", (ident.bio if ident is not None else None) or ""),
                "is_contact": hint.get("is_contact", bool(ident.is_contact) if ident is not None else False),
            }
            if sid in tg_users:
                profile["telegram_user_id"] = tg_users[sid]
        else:
            profile = {"identity_id": None, "person_id": None, "name": m.sender_name, "bio": "", "is_contact": False}
        if tg is not None:
            profile["telegram_sender_id"] = tg_sender
        profiles.append(profile)

    metadata = {
        "conversation_id": conversation_id,
        "title": (conv.title if conv else None) or "Unknown",
        "kind": (conv.kind if conv else None) or "direct",
        "channel": channel,
    }
    if tg is not None:
        metadata["telegram_peer_id"] = tg.peer_id
    return ScanRequest(
        conversation_id=conversation_id,
        channel=channel,
        targets=[_msg_dict(m, tg_users, tg) for m in targets],
        context=[_msg_dict(m, tg_users, tg) for m in context],
        profiles=profiles,
        metadata=metadata,
    )
