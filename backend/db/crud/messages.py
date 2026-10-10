"""Messages and their attachments.

Rows returned "with reply preview" are SQLAlchemy Rows shaped
``(Message, reply_id, reply_sender_name, reply_text, reply_media_type)``;
the reply columns are None when the message is not a reply or the replied-to
message is not stored. Message order inside a conversation is the keyset
``(date, seq, id)``.
"""

from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import and_, case, delete, desc, exists, func, or_, select, true, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.db.crud.conversations import one_to_one_filter, recompute_preview, refresh_preview
from backend.db.models import Conversation, Identity, Message, MessageAttachment, MessageParticipant, Person

__all__ = [
    "with_reply_preview",
    "get_message",
    "get_messages_by_ids",
    "get_message_by_external",
    "message_ids_by_external",
    "get_reply_previews",
    "search_messages_in_conversation",
    "get_messages_since",
    "get_messages_from_sender_since",
    "get_messages_around",
    "count_messages",
    "get_pinned_messages",
    "topic_filter",
    "get_message_window",
    "get_oldest_message",
    "save_messages",
    "save_message",
    "set_my_reaction",
    "set_message_pinned",
    "set_attachment_local_path",
    "delete_messages",
    "GENERAL_TOPIC_ID",
    "message_participants",
    "stored_topics",
    "count_relevant_incoming_since",
    "latest_messages",
]


# Columns the channel owns: an upsert always takes the new value.
_CHANNEL_OWNED = (
    "seq", "subject", "text", "text_html", "reactions_json", "is_pinned", "is_edited", "grouped_id",
    "reply_to_external_id", "is_outgoing", "kind", "meta_json", "fwd_from_name",
)
# Channel-owned but not always known by every writer (e.g. a just-sent
# message has no sender name, a search hit no forum flag): keep the old
# value on None.
_CHANNEL_OWNED_IF_KNOWN = ("sender_identity_id", "sender_name", "topic_id")
_ATTACHMENT_FIELDS = ("kind", "filename", "mime_type", "size", "external_ref")


def _delivery_rank(col):
    return case(
        (col == "read", 3), (col == "delivered", 2), (col == "sent", 1), (col == "sending", 0),
        else_=-1,
    )


# ── reads ────────────────────────────────────────────────────────────────────

def with_reply_preview():
    """SELECT Message + its reply preview columns (see module docstring)."""
    Reply = aliased(Message, name="reply_msg")
    reply_kind = (
        select(MessageAttachment.kind)
        .where(MessageAttachment.message_id == Reply.id)
        .order_by(MessageAttachment.position)
        .limit(1)
        .correlate(Reply)
        .scalar_subquery()
    )
    return select(
        Message,
        Reply.id.label("reply_id"),
        Reply.sender_name.label("reply_sender_name"),
        func.coalesce(Reply.text, Reply.subject).label("reply_text"),
        reply_kind.label("reply_media_type"),
    ).outerjoin(
        Reply,
        and_(Reply.conversation_id == Message.conversation_id, Reply.external_id == Message.reply_to_external_id),
    )


async def get_message(db: AsyncSession, message_id: int) -> Optional[Message]:
    return (await db.execute(select(Message).where(Message.id == message_id))).scalar_one_or_none()


async def get_messages_by_ids(db: AsyncSession, message_ids: Iterable[int]) -> dict[int, Message]:
    ids = list(set(message_ids))
    out: dict[int, Message] = {}
    for i in range(0, len(ids), 500):
        res = await db.execute(select(Message).where(Message.id.in_(ids[i:i + 500])))
        out.update({m.id: m for m in res.scalars().all()})
    return out


async def get_message_by_external(db: AsyncSession, conversation_id: int, external_id: str) -> Optional[Message]:
    return (await db.execute(
        select(Message).where(Message.conversation_id == conversation_id, Message.external_id == external_id)
    )).scalar_one_or_none()


async def message_ids_by_external(
    db: AsyncSession, conversation_id: int, external_ids: Iterable[str]
) -> dict[str, int]:
    ext = list(set(external_ids))
    out: dict[str, int] = {}
    for i in range(0, len(ext), 500):
        res = await db.execute(
            select(Message.external_id, Message.id).where(
                Message.conversation_id == conversation_id, Message.external_id.in_(ext[i:i + 500])
            )
        )
        out.update({e: mid for e, mid in res.all()})
    return out


async def get_reply_previews(db: AsyncSession, message_ids: Iterable[int]) -> list:
    """Rows with reply preview for the given messages, in id order."""
    ids = list(set(message_ids))
    if not ids:
        return []
    return list((await db.execute(with_reply_preview().where(Message.id.in_(ids)).order_by(Message.id))).all())


async def search_messages_in_conversation(
    db: AsyncSession, conversation_id: int, keyword: str, limit: int = 50
) -> list:
    """Case-insensitive search in one conversation (text and subject), newest first."""
    like = f"%{keyword}%"
    return list((await db.execute(
        with_reply_preview()
        .where(Message.conversation_id == conversation_id, or_(Message.text.ilike(like), Message.subject.ilike(like)))
        .order_by(desc(Message.date), desc(Message.seq), desc(Message.id))
        .limit(limit)
    )).all())


async def get_messages_since(
    db: AsyncSession,
    conversation_id: int,
    since: datetime,
    limit: int = 200,
    until: Optional[datetime] = None,
) -> list:
    """The NEWEST ``limit`` messages of a conversation in [since, until],
    ordered oldest-first (a busy window yields its latest messages)."""
    window = [Message.conversation_id == conversation_id, Message.date >= since]
    if until is not None:
        window.append(Message.date <= until)
    latest_ids = (
        select(Message.id).where(*window)
        .order_by(desc(Message.date), desc(Message.seq), desc(Message.id))
        .limit(limit)
    )
    return list((await db.execute(
        with_reply_preview()
        .where(Message.id.in_(latest_ids.scalar_subquery()))
        .order_by(Message.date, Message.seq, Message.id)
    )).all())


async def get_messages_from_sender_since(
    db: AsyncSession,
    identity_ids: Iterable[int],
    since: datetime,
    limit: int = 100,
    until: Optional[datetime] = None,
) -> list:
    """The NEWEST ``limit`` text messages sent by any of ``identity_ids`` (one
    person may have several) in [since, until], oldest-first. Rows are
    ``(Message, Conversation, reply_id, reply_sender_name, reply_text, reply_media_type)``."""
    ids = list(identity_ids)
    if not ids:
        return []
    window = [Message.sender_identity_id.in_(ids), Message.date >= since, Message.text.isnot(None)]
    if until is not None:
        window.append(Message.date <= until)
    latest_ids = (
        select(Message.id).where(*window).order_by(desc(Message.date), desc(Message.id)).limit(limit)
    )
    q = with_reply_preview().add_columns(Conversation).join(Conversation, Conversation.id == Message.conversation_id)
    rows = (await db.execute(
        q.where(Message.id.in_(latest_ids.scalar_subquery())).order_by(Message.date, Message.id)
    )).all()
    # Reorder to (Message, Conversation, reply...) for readability at call sites.
    return [(r[0], r[5], r[1], r[2], r[3], r[4]) for r in rows]


async def get_messages_around(
    db: AsyncSession, message: Message, before: int = 2, after: int = 1
) -> dict:
    """Up to ``before`` older and ``after`` newer messages next to
    ``message`` in its conversation: ``{"before": [...], "after": [...]}``
    oldest-first."""
    key = (message.date, message.seq, message.id)
    scope = Message.conversation_id == message.conversation_id
    older = list((await db.execute(
        select(Message).where(scope, _older_than(key))
        .order_by(desc(Message.date), desc(Message.seq), desc(Message.id)).limit(before)
    )).scalars().all())
    newer = list((await db.execute(
        select(Message).where(scope, _newer_than(key))
        .order_by(Message.date, Message.seq, Message.id).limit(after)
    )).scalars().all())
    return {"before": list(reversed(older)), "after": newer}


async def count_messages(db: AsyncSession, conversation_id: int) -> int:
    return (await db.execute(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
    )).scalar() or 0


async def get_pinned_messages(db: AsyncSession, conversation_id: int) -> list:
    """All pinned messages of a conversation, newest first, with reply previews."""
    return list((await db.execute(
        with_reply_preview()
        .where(Message.conversation_id == conversation_id, Message.is_pinned == True)  # noqa: E712
        .order_by(Message.date.desc())
    )).all())


# ── keyset windows ───────────────────────────────────────────────────────────

GENERAL_TOPIC_ID = 1


def topic_filter(topic_id: Optional[int]):
    """Messages of one forum topic. General (id 1) also owns rows whose topic
    is unknown (topic_id NULL)."""
    if topic_id is None:
        return true()
    if topic_id == GENERAL_TOPIC_ID:
        return or_(Message.topic_id == GENERAL_TOPIC_ID, Message.topic_id.is_(None))
    return Message.topic_id == topic_id


def _older_than(key: tuple):
    d, s, i = key
    return or_(
        Message.date < d,
        and_(Message.date == d, or_(Message.seq < s, and_(Message.seq == s, Message.id < i))),
    )


def _newer_than(key: tuple):
    d, s, i = key
    return or_(
        Message.date > d,
        and_(Message.date == d, or_(Message.seq > s, and_(Message.seq == s, Message.id > i))),
    )


_KEY_DESC = (Message.date.desc(), Message.seq.desc(), Message.id.desc())
_KEY_ASC = (Message.date.asc(), Message.seq.asc(), Message.id.asc())


def _key(m: Message) -> tuple:
    return (m.date, m.seq, m.id)


async def get_message_window(
    db: AsyncSession,
    conversation_id: int,
    *,
    limit: int = 50,
    before: Optional[int] = None,
    after: Optional[int] = None,
    anchor: Optional[int] = None,
    around_date: Optional[datetime] = None,
    topic_id: Optional[int] = None,
) -> dict:
    """Keyset page of a conversation's messages.

    ``before`` / ``after`` / ``anchor`` are message ids (exclusive cursors
    for before/after).
    - ``before``: the ``limit`` messages immediately older than it.
    - ``after``: the ``limit`` messages immediately newer than it.
    - ``anchor`` / ``around_date``: a window centred on that message (or the
      first message at/after the date).
    - none: the newest ``limit`` messages.

    Returns ``{"rows": [...oldest→newest], "has_older", "has_newer",
    "anchor_found"}``; rows carry reply previews. A cursor id that is not in
    this conversation yields no rows (``anchor_found`` False for anchors).
    """
    limit = max(1, min(limit, 500))
    scope = and_(Message.conversation_id == conversation_id, topic_filter(topic_id))
    base = with_reply_preview().where(scope)

    async def _key_of(message_id: int) -> Optional[tuple]:
        row = (await db.execute(
            select(Message.date, Message.seq, Message.id).where(
                Message.id == message_id, Message.conversation_id == conversation_id
            )
        )).first()
        return tuple(row) if row else None

    anchor_found = True
    anchor_key = None
    if anchor is not None:
        anchor_key = await _key_of(anchor)
        anchor_found = anchor_key is not None
    elif around_date is not None:
        row = (await db.execute(
            select(Message.date, Message.seq, Message.id)
            .where(scope, Message.date >= around_date).order_by(*_KEY_ASC).limit(1)
        )).first()
        anchor_key = tuple(row) if row else None
        anchor_found = anchor_key is not None

    after_key = await _key_of(after) if after is not None else None
    before_key = await _key_of(before) if before is not None else None

    if after is not None:
        rows = [] if after_key is None else list((await db.execute(
            base.where(_newer_than(after_key)).order_by(*_KEY_ASC).limit(limit)
        )).all())
    elif before is not None:
        rows = [] if before_key is None else list((await db.execute(
            base.where(_older_than(before_key)).order_by(*_KEY_DESC).limit(limit)
        )).all())
        rows.reverse()
    elif anchor_key is not None:
        half = limit // 2
        older = list((await db.execute(base.where(_older_than(anchor_key)).order_by(*_KEY_DESC).limit(half))).all())
        older.reverse()
        newer = list((await db.execute(
            base.where(or_(_newer_than(anchor_key), Message.id == anchor_key[2]))
            .order_by(*_KEY_ASC).limit(limit - half)
        )).all())
        rows = older + newer
    elif anchor is not None or around_date is not None:
        rows = []
    else:
        rows = list((await db.execute(base.order_by(*_KEY_DESC).limit(limit))).all())
        rows.reverse()

    async def _exists(cond) -> bool:
        return bool((await db.execute(select(exists().where(scope, cond)))).scalar())

    if rows:
        has_older = await _exists(_older_than(_key(rows[0][0])))
        has_newer = await _exists(_newer_than(_key(rows[-1][0])))
    else:
        has_older = await _exists(_older_than(after_key)) if after_key is not None else False
        has_newer = await _exists(_newer_than(before_key)) if before_key is not None else False
    return {"rows": rows, "has_older": has_older, "has_newer": has_newer, "anchor_found": anchor_found}


async def get_oldest_message(
    db: AsyncSession, conversation_id: int, topic_id: Optional[int] = None
) -> Optional[Message]:
    return (await db.execute(
        select(Message).where(Message.conversation_id == conversation_id, topic_filter(topic_id))
        .order_by(*_KEY_ASC).limit(1)
    )).scalar_one_or_none()


# ── writes ───────────────────────────────────────────────────────────────────

async def _stored_attachments(db: AsyncSession, message_ids: list) -> dict[int, dict[int, MessageAttachment]]:
    """``{message_id: {position: attachment}}`` for a batch, one query per 500."""
    out: dict[int, dict[int, MessageAttachment]] = {}
    for i in range(0, len(message_ids), 500):
        for a in (await db.execute(
            select(MessageAttachment).where(MessageAttachment.message_id.in_(message_ids[i:i + 500]))
        )).scalars().all():
            out.setdefault(a.message_id, {})[a.position] = a
    return out


async def _sync_attachments(
    db: AsyncSession, message_id: int, attachments: list[dict],
    current: Optional[dict[int, MessageAttachment]] = None,
) -> bool:
    """Make the message's attachments (``current`` by position, read when
    not given) match ``attachments``. ``local_path`` is kept once set.
    Returns whether anything changed."""
    if current is None:
        current = (await _stored_attachments(db, [message_id])).get(message_id, {})
    current = dict(current)
    changed = False
    for position, att in enumerate(attachments):
        fields = {f: att.get(f) for f in _ATTACHMENT_FIELDS}
        existing = current.pop(position, None)
        if existing is None:
            db.add(MessageAttachment(
                message_id=message_id, position=position, local_path=att.get("local_path"), **fields,
            ))
            changed = True
            continue
        for f, v in fields.items():
            if v is not None and getattr(existing, f) != v:
                setattr(existing, f, v)
                changed = True
        if existing.local_path is None and att.get("local_path"):
            existing.local_path = att["local_path"]
            changed = True
    for stale in current.values():
        await db.delete(stale)
        changed = True
    if changed:
        await db.flush()
    return changed


async def save_messages(db: AsyncSession, rows: list[dict], commit: bool = True) -> dict:
    """Upsert a batch of message rows (one transaction, one commit).

    Rows are keyed by ``(conversation_id, external_id)``. Channel-owned
    columns take the new value, local-only columns (scam_*, my_reaction,
    importance, user_flagged) are never touched, ``topic_id`` /
    ``sender_*`` are back-filled when known and ``delivery_state`` only moves
    forward. An optional ``attachments`` list (dicts with kind, filename,
    mime_type, size, external_ref, local_path) replaces the message's
    attachments by position; absent = leave them alone.

    Returns ``{"created": [message ids], "updated": [message ids]}``; a row
    whose content is unchanged appears in neither list.
    """
    created: list[int] = []
    updated: list[int] = []
    if not rows:
        return {"created": created, "updated": updated}

    by_conv: dict[int, list[str]] = {}
    for r in rows:
        by_conv.setdefault(r["conversation_id"], []).append(r["external_id"])
    existing: dict[tuple[int, str], int] = {}
    for conv_id, exts in by_conv.items():
        for ext, mid in (await message_ids_by_external(db, conv_id, exts)).items():
            existing[(conv_id, ext)] = mid

    # Stored files of the rows that carry an attachment list, in one query
    stored_files = await _stored_attachments(db, sorted({
        existing[(r["conversation_id"], r["external_id"])] for r in rows
        if r.get("attachments") is not None and (r["conversation_id"], r["external_id"]) in existing
    }))

    newest_created: dict[int, dict] = {}
    for raw in rows:
        row = {k: v for k, v in raw.items() if k != "attachments"}
        attachments = raw.get("attachments")
        if row.get("date") is None:
            row["date"] = datetime.utcnow()
        row.setdefault("delivery_state", "sent")
        conv_id, ext = row["conversation_id"], row["external_id"]
        existing_id = existing.get((conv_id, ext))

        stmt = sqlite_insert(Message).values(**row)
        ex = stmt.excluded
        set_: dict[str, Any] = {}
        for col in _CHANNEL_OWNED:
            if col in row:
                set_[col] = getattr(ex, col)
        for col in _CHANNEL_OWNED_IF_KNOWN:
            if row.get(col) is not None:
                set_[col] = getattr(ex, col)
        if row.get("delivery_state"):
            set_["delivery_state"] = case(
                (_delivery_rank(ex.delivery_state) > _delivery_rank(Message.delivery_state), ex.delivery_state),
                else_=Message.delivery_state,
            )
        keys = [Message.conversation_id, Message.external_id]
        if not set_:
            stmt = stmt.on_conflict_do_nothing(index_elements=keys)
        else:
            changed = or_(*[getattr(Message, col).is_distinct_from(expr) for col, expr in set_.items()])
            stmt = stmt.on_conflict_do_update(index_elements=keys, set_=set_, where=changed)
        written = (await db.execute(stmt.returning(Message.id))).scalar()
        message_id = written if written is not None else existing_id

        attachments_changed = False
        if attachments is not None and message_id is not None:
            if existing_id is None and not attachments:
                pass  # a new message without files: nothing stored, nothing to add
            else:
                if existing_id is None:
                    current: Optional[dict] = {}
                elif message_id in created:  # repeated in this batch: read what we just added
                    current = None
                else:
                    current = stored_files.get(message_id, {})
                attachments_changed = await _sync_attachments(db, message_id, attachments, current)

        if existing_id is None and written is not None:
            existing[(conv_id, ext)] = written  # a repeated row in the batch is an update
            created.append(written)
            cur = newest_created.get(conv_id)
            if cur is None or row["date"] > cur["date"]:
                newest_created[conv_id] = row
        elif written is not None or attachments_changed:
            updated.append(message_id)

    # Cached inbox preview. Conditional UPDATE so an older message can never
    # replace a newer preview.
    for conv_id, row in newest_created.items():
        await db.execute(
            update(Conversation)
            .where(
                Conversation.id == conv_id,
                or_(Conversation.last_activity_at.is_(None), Conversation.last_activity_at <= row["date"]),
            )
            .values(
                last_activity_at=row["date"],
                last_message_text=row.get("text") or row.get("subject"),
                last_message_sender=row.get("sender_name"),
                last_message_at=row["date"],
            )
        )
    # An edit of the message the preview shows must update the preview too.
    if updated:
        updated_by_conv: dict[int, set] = {}
        for mid, conv_id in (await db.execute(
            select(Message.id, Message.conversation_id).where(Message.id.in_(updated))
        )).all():
            updated_by_conv.setdefault(conv_id, set()).add(mid)
        for conv_id, ids in updated_by_conv.items():
            await refresh_preview(db, conv_id, only_if_message_ids=ids)
    if commit:
        await db.commit()
    return {"created": created, "updated": updated}


async def save_message(db: AsyncSession, **row) -> Optional[Message]:
    """Upsert one message (see ``save_messages``) and return the stored row,
    with a transient ``_change`` marker: "created", "updated" or None."""
    res = await save_messages(db, [row])
    msg = (await db.execute(
        select(Message)
        .where(Message.conversation_id == row["conversation_id"], Message.external_id == row["external_id"])
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if msg is not None:
        msg._change = "created" if res["created"] else ("updated" if res["updated"] else None)
    return msg


async def set_my_reaction(db: AsyncSession, message_id: int, emoji: Optional[str]) -> Optional[Message]:
    msg = await get_message(db, message_id)
    if msg is None:
        return None
    msg.my_reaction = emoji
    await db.commit()
    await db.refresh(msg)
    return msg


async def set_message_pinned(db: AsyncSession, message_id: int, pinned: bool) -> Optional[Message]:
    msg = await get_message(db, message_id)
    if msg is None:
        return None
    msg.is_pinned = pinned
    await db.commit()
    await db.refresh(msg)
    return msg


async def set_attachment_local_path(db: AsyncSession, attachment_id: int, local_path: Optional[str]) -> None:
    await db.execute(
        update(MessageAttachment).where(MessageAttachment.id == attachment_id).values(local_path=local_path)
    )
    await db.commit()


async def delete_messages(db: AsyncSession, message_ids: Iterable[int], commit: bool = True) -> dict[int, list[int]]:
    """Delete stored messages (attachments / participants cascade) and
    re-derive each affected conversation's inbox preview. The one delete
    path for every channel. Returns ``{conversation_id: [deleted message
    ids]}`` (sorted) for the ``messages_deleted`` events."""
    ids = list(set(message_ids))
    found: list = []
    for i in range(0, len(ids), 500):
        found.extend((await db.execute(
            select(Message.id, Message.conversation_id, Message.date).where(Message.id.in_(ids[i:i + 500]))
        )).all())
    if not found:
        return {}
    by_conv: dict[int, list[int]] = {}
    newest_removed: dict[int, datetime] = {}
    for mid, conv_id, date in found:
        by_conv.setdefault(conv_id, []).append(mid)
        if date is not None and (conv_id not in newest_removed or date > newest_removed[conv_id]):
            newest_removed[conv_id] = date
    gone = [mid for mid, _, _ in found]
    for i in range(0, len(gone), 500):
        await db.execute(delete(Message).where(Message.id.in_(gone[i:i + 500])))
    for conv_id in by_conv:
        await recompute_preview(db, conv_id, newest_removed.get(conv_id))
    if commit:
        await db.commit()
    return {conv_id: sorted(mids) for conv_id, mids in by_conv.items()}


async def message_participants(db: AsyncSession, message_ids: Iterable[int]) -> dict[int, list[dict]]:
    """``{message_id: [{identity_id, person_id, role, display_name, external_id}]}``."""
    ids = list(set(message_ids))
    out: dict[int, list[dict]] = {}
    if not ids:
        return out
    rows = (await db.execute(
        select(MessageParticipant.message_id, MessageParticipant.role, Identity)
        .join(Identity, Identity.id == MessageParticipant.identity_id)
        .where(MessageParticipant.message_id.in_(ids))
        .order_by(MessageParticipant.id)
    )).all()
    for mid, role, ident in rows:
        out.setdefault(mid, []).append({
            "identity_id": ident.id, "person_id": ident.person_id, "role": role,
            "display_name": ident.display_name, "external_id": ident.external_id,
        })
    return out


async def stored_topics(db: AsyncSession, conversation_id: int) -> list[tuple[int, datetime]]:
    """``(topic_id, first message date)`` of the forum topics seen in stored
    messages (a fallback when the channel can't list them)."""
    rows = (await db.execute(
        select(Message.topic_id, func.min(Message.date))
        .where(Message.conversation_id == conversation_id, Message.topic_id.isnot(None))
        .group_by(Message.topic_id)
    )).all()
    return [(tid, first) for tid, first in rows]


async def count_relevant_incoming_since(db: AsyncSession, since: datetime) -> int:
    """Incoming messages since ``since`` in tracked conversations or direct
    conversations with important people (visible conversations only)."""
    important_peers = (
        select(Identity.id).join(Person, Person.id == Identity.person_id)
        .where(Person.is_important == True)  # noqa: E712
    )
    relevant = or_(
        Conversation.is_tracked == True,  # noqa: E712
        and_(one_to_one_filter(), Conversation.peer_identity_id.in_(important_peers)),
    )
    return (await db.execute(
        select(func.count()).select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.date >= since,
            Message.is_outgoing == False,  # noqa: E712
            Message.kind == "message",
            Conversation.is_hidden == False,  # noqa: E712
            Conversation.is_deactivated == False,  # noqa: E712
            relevant,
        )
    )).scalar() or 0


async def latest_messages(db: AsyncSession, conversation_ids: Iterable[int]) -> dict[int, Message]:
    """The newest message of each conversation (one query)."""
    ids = list(conversation_ids)
    if not ids:
        return {}
    ranked = (
        select(
            Message.id,
            func.row_number().over(
                partition_by=Message.conversation_id, order_by=(desc(Message.date), desc(Message.id)),
            ).label("rn"),
        )
        .where(Message.conversation_id.in_(ids))
        .subquery()
    )
    rows = (await db.execute(select(Message).join(ranked, ranked.c.id == Message.id).where(ranked.c.rn == 1))).scalars()
    return {m.conversation_id: m for m in rows.all()}
