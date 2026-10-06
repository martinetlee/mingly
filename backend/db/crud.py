from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, delete, update, desc, asc, func, or_, and_, case, exists, true
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import (
    Chat, Message, Tag, ChatTag, BriefingRun, BriefingEntry, Conversation, ConversationMessage, Contact,
    Workspace, WorkspaceTag, WorkspaceColumn, WorkspaceChatOverride, WorkspaceBoard,
)


# --- Chats ---

async def get_chats(
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "recent",
    tag_id: Optional[int] = None,
    workspace_id: Optional[int] = None,
) -> list:
    query = (
        select(Chat)
        .where(Chat.is_hidden == False, Chat.is_briefing == False, Chat.is_archived == False, Chat.is_deactivated == False)  # noqa: E712
    )
    if tag_id is not None:
        query = query.join(ChatTag, Chat.telegram_id == ChatTag.chat_id).where(ChatTag.tag_id == tag_id)
    if workspace_id is not None:
        query = query.where(Chat.telegram_id.in_(_workspace_member_ids(workspace_id)))

    if sort_by == "unread":
        query = query.order_by(desc(Chat.is_pinned), desc(Chat.unread_count), desc(Chat.last_activity_at), Chat.name)
    elif sort_by == "alpha":
        query = query.order_by(desc(Chat.is_pinned), asc(Chat.name))
    else:  # recent
        query = query.order_by(desc(Chat.is_pinned), desc(Chat.last_activity_at), Chat.name)

    result = await db.execute(query.limit(limit).offset(offset))
    chats = result.scalars().all()
    return [(c, c.last_message_text, c.last_message_sender, c.last_message_date) for c in chats]


async def get_tracked_chats(db: AsyncSession) -> list:
    row_num = func.row_number().over(
        partition_by=Message.chat_id,
        order_by=desc(Message.date),
    ).label("rn")
    ranked = (
        select(
            Message.chat_id,
            Message.text.label("last_text"),
            Message.sender_name.label("last_sender"),
            Message.date.label("last_date"),
            row_num,
        )
        .subquery()
    )
    last_msg = (
        select(
            ranked.c.chat_id,
            ranked.c.last_text,
            ranked.c.last_sender,
            ranked.c.last_date,
        )
        .where(ranked.c.rn == 1)
        .subquery()
    )
    result = await db.execute(
        select(Chat, last_msg.c.last_text, last_msg.c.last_sender, last_msg.c.last_date)
        .where(Chat.is_tracked == True)  # noqa: E712
        .outerjoin(last_msg, Chat.telegram_id == last_msg.c.chat_id)
        .order_by(desc(Chat.last_activity_at), Chat.name)
    )
    return list(result.all())


async def get_tracked_chat_models(db: AsyncSession) -> list[Chat]:
    """Tracked chats as plain Chat models (no last-message preview columns)."""
    result = await db.execute(
        select(Chat)
        .where(Chat.is_tracked == True)  # noqa: E712
        .order_by(desc(Chat.last_activity_at), Chat.name)
    )
    return list(result.scalars().all())


async def set_tracked(db: AsyncSession, telegram_id: int, tracked: bool) -> Optional[Chat]:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        return None
    chat.is_tracked = tracked
    await db.commit()
    await db.refresh(chat)
    return chat


async def get_briefing_chats(db: AsyncSession) -> list:
    result = await db.execute(
        select(Chat)
        .where(Chat.is_briefing == True)  # noqa: E712
        .order_by(Chat.name)
    )
    return list(result.scalars().all())


async def set_briefing(db: AsyncSession, telegram_id: int, briefing: bool) -> Optional[Chat]:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        return None
    chat.is_briefing = briefing
    await db.commit()
    await db.refresh(chat)
    return chat


async def get_hidden_chats(db: AsyncSession) -> list:
    result = await db.execute(
        select(Chat)
        .where(Chat.is_hidden == True)  # noqa: E712
        .order_by(Chat.name)
    )
    return list(result.scalars().all())


async def set_hidden(db: AsyncSession, telegram_id: int, hidden: bool) -> Optional[Chat]:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        return None
    chat.is_hidden = hidden
    if hidden:
        chat.is_monitored = False
    await db.commit()
    await db.refresh(chat)
    return chat


async def get_chat_by_telegram_id(db: AsyncSession, telegram_id: int) -> Optional[Chat]:
    result = await db.execute(
        select(Chat).where(Chat.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def upsert_chat(
    db: AsyncSession,
    telegram_id: int,
    name: str,
    chat_type: str,
    username: Optional[str] = None,
    unread_count: Optional[int] = None,
    members_count: Optional[int] = None,
    is_forum: Optional[bool] = None,
    slow_mode_seconds: Optional[int] = None,
    last_message_date: Optional[datetime] = None,
    is_deactivated: Optional[bool] = None,
    commit: bool = True,
) -> Chat:
    """Insert or update a chat in one statement (race-free).

    name/type/username always take the new value; the optional fields only
    when provided. ``last_activity_at`` never moves backwards. Pass
    ``commit=False`` to batch many upserts into one transaction.
    """
    values = dict(
        telegram_id=telegram_id,
        name=name,
        type=chat_type,
        username=username,
        unread_count=unread_count if unread_count is not None else 0,
        members_count=members_count,
        is_forum=is_forum if is_forum is not None else False,
        slow_mode_seconds=slow_mode_seconds if slow_mode_seconds is not None else 0,
        last_activity_at=last_message_date,
        is_deactivated=is_deactivated if is_deactivated is not None else False,
        created_at=datetime.utcnow(),
    )
    stmt = sqlite_insert(Chat).values(**values)
    ex = stmt.excluded
    set_ = {"name": ex.name, "type": ex.type, "username": ex.username}
    if unread_count is not None:
        set_["unread_count"] = ex.unread_count
    if members_count is not None:
        set_["members_count"] = ex.members_count
    if is_forum is not None:
        set_["is_forum"] = ex.is_forum
    if slow_mode_seconds is not None:
        set_["slow_mode_seconds"] = ex.slow_mode_seconds
    if is_deactivated is not None:
        set_["is_deactivated"] = ex.is_deactivated
    if last_message_date is not None:
        set_["last_activity_at"] = case(
            (or_(Chat.last_activity_at.is_(None), Chat.last_activity_at < ex.last_activity_at), ex.last_activity_at),
            else_=Chat.last_activity_at,
        )
    await db.execute(stmt.on_conflict_do_update(index_elements=[Chat.telegram_id], set_=set_))
    if commit:
        await db.commit()
    result = await db.execute(
        select(Chat).where(Chat.telegram_id == telegram_id).execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def get_existing_chat_ids(db: AsyncSession, telegram_ids: list[int]) -> set[int]:
    if not telegram_ids:
        return set()
    result = await db.execute(select(Chat.telegram_id).where(Chat.telegram_id.in_(telegram_ids)))
    return {row[0] for row in result.all()}


async def mark_chat_read(db: AsyncSession, telegram_id: int) -> Optional[Chat]:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        return None
    chat.unread_count = 0
    await db.commit()
    await db.refresh(chat)
    return chat


async def set_pinned(db: AsyncSession, telegram_id: int, pinned: bool) -> Optional[Chat]:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        return None
    chat.is_pinned = pinned
    await db.commit()
    await db.refresh(chat)
    return chat


async def set_chat_pinned_message(db: AsyncSession, telegram_id: int, pinned_message_id: int) -> Optional[Chat]:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        return None
    chat.pinned_message_id = pinned_message_id
    await db.commit()
    await db.refresh(chat)
    return chat


async def set_archived(db: AsyncSession, telegram_id: int, archived: bool) -> Optional[Chat]:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        return None
    chat.is_archived = archived
    await db.commit()
    await db.refresh(chat)
    return chat


async def get_archived_chats(db: AsyncSession) -> list:
    result = await db.execute(
        select(Chat)
        .where(Chat.is_archived == True)  # noqa: E712
        .order_by(Chat.name)
    )
    return list(result.scalars().all())


async def set_muted(
    db: AsyncSession,
    telegram_id: int,
    muted: bool,
    muted_until: Optional[datetime] = None,
) -> Optional[Chat]:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        return None
    chat.is_muted = muted
    chat.muted_until = muted_until if muted else None
    await db.commit()
    await db.refresh(chat)
    return chat


async def set_marked_unread(db: AsyncSession, telegram_id: int, marked_unread: bool) -> Optional[Chat]:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        return None
    chat.is_marked_unread = marked_unread
    await db.commit()
    await db.refresh(chat)
    return chat


async def get_pinned_message(db: AsyncSession, chat_telegram_id: int) -> Optional[Message]:
    """Get the pinned message for a chat, if any."""
    chat = await get_chat_by_telegram_id(db, chat_telegram_id)
    if not chat or not chat.pinned_message_id:
        return None
    result = await db.execute(
        select(Message).where(
            Message.chat_id == chat_telegram_id,
            Message.tg_message_id == chat.pinned_message_id,
        ).limit(1)
    )
    return result.scalars().first()


async def set_monitored(db: AsyncSession, telegram_id: int, monitored: bool) -> Optional[Chat]:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        return None
    chat.is_monitored = monitored
    await db.commit()
    await db.refresh(chat)
    return chat


# --- Messages ---

async def get_messages(
    db: AsyncSession,
    chat_id: int,
    limit: int = 50,
    offset: int = 0,
) -> list:
    """
    Return messages with optional reply-to preview.
    Each element is a (Message, reply_sender_name, reply_text, reply_media_type) tuple.
    """
    Reply = Message.__table__.alias("reply_msg")
    result = await db.execute(
        select(
            Message,
            Reply.c.sender_name.label("reply_sender_name"),
            Reply.c.text.label("reply_text"),
            Reply.c.media_type.label("reply_media_type"),
        )
        .outerjoin(
            Reply,
            (Reply.c.tg_message_id == Message.reply_to_tg_message_id)
            & (Reply.c.chat_id == Message.chat_id),
        )
        .where(Message.chat_id == chat_id)
        .order_by(desc(Message.date), desc(Message.tg_message_id))
        .limit(limit)
        .offset(offset)
    )
    return list(result.all())


async def search_messages_in_chat(
    db: AsyncSession,
    chat_id: int,
    keyword: str,
    limit: int = 50,
) -> list:
    """Search messages in a chat by keyword (case-insensitive)."""
    Reply = Message.__table__.alias("reply_msg")
    result = await db.execute(
        select(
            Message,
            Reply.c.sender_name.label("reply_sender_name"),
            Reply.c.text.label("reply_text"),
            Reply.c.media_type.label("reply_media_type"),
        )
        .outerjoin(
            Reply,
            (Reply.c.tg_message_id == Message.reply_to_tg_message_id)
            & (Reply.c.chat_id == Message.chat_id),
        )
        .where(Message.chat_id == chat_id, Message.text.ilike(f"%{keyword}%"))
        .order_by(desc(Message.date))
        .limit(limit)
    )
    return list(result.all())


async def get_messages_since(
    db: AsyncSession,
    chat_id: int,
    since: datetime,
    limit: int = 200,
    until: Optional[datetime] = None,
) -> list:
    """
    Return the NEWEST `limit` messages of a chat in [since, until], ordered
    oldest-first (so a busy window yields its latest messages, not its first).
    Each element is a (Message, reply_sender_name, reply_text) tuple.
    reply_sender_name and reply_text are None when the message is not a reply.
    """
    window = [Message.chat_id == chat_id, Message.date >= since]
    if until is not None:
        window.append(Message.date <= until)
    latest_ids = (
        select(Message.id)
        .where(*window)
        .order_by(desc(Message.date), desc(Message.tg_message_id))
        .limit(limit)
    )
    Reply = Message.__table__.alias("reply_msg")
    result = await db.execute(
        select(
            Message,
            Reply.c.sender_name.label("reply_sender_name"),
            Reply.c.text.label("reply_text"),
        )
        .outerjoin(
            Reply,
            (Reply.c.tg_message_id == Message.reply_to_tg_message_id)
            & (Reply.c.chat_id == Message.chat_id),
        )
        .where(Message.id.in_(latest_ids.scalar_subquery()))
        .order_by(Message.date, Message.tg_message_id)
    )
    return list(result.all())


async def message_exists(db: AsyncSession, telegram_id: int, chat_id: int) -> bool:
    result = await db.execute(
        select(Message.id).where(
            Message.tg_message_id == telegram_id,
            Message.chat_id == chat_id,
        ).limit(1)
    )
    return result.scalar() is not None


# Columns Telegram owns: an upsert always takes the new value.
_MSG_TG_OWNED = (
    "text", "text_html", "reactions_json", "is_pinned", "is_edited", "media_type",
    "media_filename", "media_size", "grouped_id", "reply_to_tg_message_id", "is_outgoing",
)
# Telegram-owned but not always known by every writer (e.g. compose has no
# sender name, mention search has no forum flag): keep the old value on None.
_MSG_TG_OWNED_IF_KNOWN = ("sender_id", "sender_name", "topic_id")
_DELIVERY_RANK = {"sending": 0, "sent": 1, "delivered": 2, "read": 3}


def _delivery_rank(col):
    return case(
        (col == "read", 3), (col == "delivered", 2), (col == "sent", 1), (col == "sending", 0),
        else_=-1,
    )


async def save_messages(db: AsyncSession, rows: list[dict], commit: bool = True) -> dict:
    """Upsert a batch of message rows (one transaction, one commit).

    ``INSERT … ON CONFLICT(tg_message_id, chat_id) DO UPDATE``: Telegram-owned
    columns take the new value, local-only columns (scam_*, my_reaction,
    importance, user_flagged) are never touched, ``media_path`` is kept once
    set, ``topic_id`` is back-filled when known and ``delivery_state`` only
    moves forward. Returns ``{"created": [tg ids], "updated": [tg ids]}``;
    a row whose Telegram content is unchanged appears in neither list.
    """
    created: list[int] = []
    updated: list[int] = []
    if not rows:
        return {"created": created, "updated": updated}

    by_chat: dict[int, list[int]] = {}
    for r in rows:
        by_chat.setdefault(r["chat_id"], []).append(r["tg_message_id"])
    existing: set[tuple[int, int]] = set()
    for chat_id, ids in by_chat.items():
        res = await db.execute(
            select(Message.tg_message_id).where(Message.chat_id == chat_id, Message.tg_message_id.in_(ids))
        )
        existing.update((chat_id, tid) for (tid,) in res.all())

    newest_created: dict[int, dict] = {}
    for row in rows:
        row = dict(row)
        if row.get("date") is None:
            row["date"] = datetime.utcnow()
        row.setdefault("delivery_state", "sent")
        stmt = sqlite_insert(Message).values(**row)
        ex = stmt.excluded
        set_ = {}
        for col in _MSG_TG_OWNED:
            if col in row:
                set_[col] = getattr(ex, col)
        for col in _MSG_TG_OWNED_IF_KNOWN:
            if row.get(col) is not None:
                set_[col] = getattr(ex, col)
        if row.get("media_path"):
            set_["media_path"] = func.coalesce(Message.media_path, ex.media_path)
        if row.get("delivery_state"):
            set_["delivery_state"] = case(
                (_delivery_rank(ex.delivery_state) > _delivery_rank(Message.delivery_state), ex.delivery_state),
                else_=Message.delivery_state,
            )
        if not set_:
            stmt = stmt.on_conflict_do_nothing(index_elements=[Message.tg_message_id, Message.chat_id])
        else:
            changed = or_(*[getattr(Message, col).is_distinct_from(expr) for col, expr in set_.items()])
            stmt = stmt.on_conflict_do_update(
                index_elements=[Message.tg_message_id, Message.chat_id], set_=set_, where=changed,
            )
        result = await db.execute(stmt.returning(Message.id))
        if result.first() is None:
            continue
        key = (row["chat_id"], row["tg_message_id"])
        if key in existing:
            updated.append(row["tg_message_id"])
        else:
            existing.add(key)
            created.append(row["tg_message_id"])
            cur = newest_created.get(row["chat_id"])
            if cur is None or row["date"] > cur["date"]:
                newest_created[row["chat_id"]] = row

    # Cached chat preview. Conditional UPDATE so an older message can never
    # replace a newer preview.
    for chat_id, row in newest_created.items():
        await db.execute(
            update(Chat)
            .where(
                Chat.telegram_id == chat_id,
                or_(Chat.last_activity_at.is_(None), Chat.last_activity_at <= row["date"]),
            )
            .values(
                last_activity_at=row["date"],
                last_message_text=row.get("text"),
                last_message_sender=row.get("sender_name"),
                last_message_date=row["date"],
            )
        )
    # An edit (or a text repair) of the message the preview shows must update
    # the preview too.
    updated_by_chat: dict[int, set] = {}
    for row in rows:
        if row["tg_message_id"] in updated:
            updated_by_chat.setdefault(row["chat_id"], set()).add(row["tg_message_id"])
    for chat_id, tg_ids in updated_by_chat.items():
        await refresh_chat_preview(db, chat_id, only_if_tg_ids=tg_ids)
    if commit:
        await db.commit()
    return {"created": created, "updated": updated}


async def refresh_chat_preview(db: AsyncSession, chat_id: int, only_if_tg_ids: Optional[set] = None) -> None:
    """Re-derive the chat list's cached last-message preview from the newest
    stored message (when it is the previewed one, or among ``only_if_tg_ids``)."""
    newest = (await db.execute(
        select(Message.tg_message_id, Message.text, Message.sender_name, Message.date)
        .where(Message.chat_id == chat_id)
        .order_by(Message.date.desc(), Message.tg_message_id.desc())
        .limit(1)
    )).first()
    if newest is None or (only_if_tg_ids is not None and newest.tg_message_id not in only_if_tg_ids):
        return
    await db.execute(
        update(Chat)
        .where(Chat.telegram_id == chat_id, Chat.last_message_date == newest.date)
        .values(last_message_text=newest.text, last_message_sender=newest.sender_name)
    )


async def save_message(db: AsyncSession, **kwargs) -> Optional[Message]:
    """Upsert one message (see ``save_messages``) and return the stored row.

    The returned row carries a transient ``_change`` marker: "created",
    "updated" or None (nothing changed).
    """
    res = await save_messages(db, [kwargs])
    result = await db.execute(
        select(Message)
        .where(Message.tg_message_id == kwargs["tg_message_id"], Message.chat_id == kwargs["chat_id"])
        .limit(1)
        .execution_options(populate_existing=True)
    )
    msg = result.scalars().first()
    if msg is not None:
        msg._change = "created" if res["created"] else ("updated" if res["updated"] else None)
    return msg


async def update_chat_sync(db: AsyncSession, telegram_id: int) -> None:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if chat:
        chat.last_synced_at = datetime.utcnow()
        await db.commit()


async def set_my_reaction(
    db: AsyncSession,
    chat_id: int,
    telegram_msg_id: int,
    emoji: Optional[str],
) -> Optional[Message]:
    """Set or clear the current user's reaction on a message."""
    result = await db.execute(
        select(Message).where(
            Message.chat_id == chat_id,
            Message.tg_message_id == telegram_msg_id,
        ).limit(1)
    )
    msg = result.scalars().first()
    if not msg:
        return None
    msg.my_reaction = emoji
    await db.commit()
    await db.refresh(msg)
    return msg


# --- Forum topics & pinned messages ---

async def set_message_pinned(
    db: AsyncSession,
    chat_id: int,
    telegram_msg_id: int,
    pinned: bool,
) -> Optional[Message]:
    """Set or clear the pinned flag on a message."""
    result = await db.execute(
        select(Message).where(
            Message.chat_id == chat_id,
            Message.tg_message_id == telegram_msg_id,
        ).limit(1)
    )
    msg = result.scalars().first()
    if not msg:
        return None
    msg.is_pinned = pinned
    await db.commit()
    await db.refresh(msg)
    return msg


async def get_pinned_messages(db: AsyncSession, chat_telegram_id: int) -> list:
    """Get all pinned messages in a chat, with reply previews."""
    Reply = Message.__table__.alias("reply_msg")
    result = await db.execute(
        select(
            Message,
            Reply.c.sender_name.label("reply_sender"),
            Reply.c.text.label("reply_text"),
            Reply.c.media_type.label("reply_media_type"),
        )
        .outerjoin(
            Reply,
            (Reply.c.tg_message_id == Message.reply_to_tg_message_id)
            & (Reply.c.chat_id == Message.chat_id),
        )
        .where(
            Message.chat_id == chat_telegram_id,
            Message.is_pinned == True,  # noqa: E712
        )
        .order_by(Message.date.desc())
    )
    return result.all()


# --- Keyset message windows ---

GENERAL_TOPIC_ID = 1


def topic_filter(topic_id: Optional[int]):
    """Messages of one forum topic. General (id 1) also owns rows stored
    before topic ids were back-filled (topic_id NULL)."""
    if topic_id is None:
        return true()
    if topic_id == GENERAL_TOPIC_ID:
        return or_(Message.topic_id == GENERAL_TOPIC_ID, Message.topic_id.is_(None))
    return Message.topic_id == topic_id


def _older_than(key: tuple):
    d, t = key
    return or_(Message.date < d, and_(Message.date == d, Message.tg_message_id < t))


def _newer_than(key: tuple):
    d, t = key
    return or_(Message.date > d, and_(Message.date == d, Message.tg_message_id > t))


def _with_reply_preview():
    Reply = Message.__table__.alias("reply_msg")
    return select(
        Message,
        Reply.c.sender_name.label("reply_sender_name"),
        Reply.c.text.label("reply_text"),
        Reply.c.media_type.label("reply_media_type"),
    ).outerjoin(
        Reply,
        (Reply.c.tg_message_id == Message.reply_to_tg_message_id)
        & (Reply.c.chat_id == Message.chat_id),
    )


async def get_message_window(
    db: AsyncSession,
    chat_id: int,
    *,
    limit: int = 50,
    before: Optional[tuple] = None,
    after: Optional[tuple] = None,
    anchor_tg_id: Optional[int] = None,
    around_date: Optional[datetime] = None,
    topic_id: Optional[int] = None,
) -> dict:
    """Keyset page of messages ordered by (date, tg_message_id).

    Cursors are ``(date, tg_message_id)`` tuples and are exclusive.
    - ``before``: the ``limit`` messages immediately older than the cursor.
    - ``after``: the ``limit`` messages immediately newer than the cursor.
    - ``anchor_tg_id`` / ``around_date``: a window centred on that message
      (or the first message at/after the date).
    - none: the newest ``limit`` messages.

    Returns ``{"rows": [...oldest→newest], "has_older": bool, "has_newer": bool,
    "anchor_found": bool}`` where each row is (Message, reply_sender, reply_text,
    reply_media_type).
    """
    limit = max(1, min(limit, 500))
    scope = and_(Message.chat_id == chat_id, topic_filter(topic_id))
    key_desc = (Message.date.desc(), Message.tg_message_id.desc())
    key_asc = (Message.date.asc(), Message.tg_message_id.asc())
    base = _with_reply_preview().where(scope)
    anchor_found = True

    anchor_key = None
    if anchor_tg_id is not None:
        res = await db.execute(
            select(Message.date, Message.tg_message_id).where(scope, Message.tg_message_id == anchor_tg_id)
        )
        row = res.first()
        anchor_key = tuple(row) if row else None
        anchor_found = anchor_key is not None
    elif around_date is not None:
        res = await db.execute(
            select(Message.date, Message.tg_message_id)
            .where(scope, Message.date >= around_date)
            .order_by(*key_asc)
            .limit(1)
        )
        row = res.first()
        anchor_key = tuple(row) if row else None
        anchor_found = anchor_key is not None

    if after is not None:
        rows = list((await db.execute(base.where(_newer_than(after)).order_by(*key_asc).limit(limit))).all())
    elif before is not None:
        rows = list((await db.execute(base.where(_older_than(before)).order_by(*key_desc).limit(limit))).all())
        rows.reverse()
    elif anchor_key is not None:
        half = limit // 2
        older = list((await db.execute(base.where(_older_than(anchor_key)).order_by(*key_desc).limit(half))).all())
        older.reverse()
        newer = list((await db.execute(
            base.where(or_(_newer_than(anchor_key), and_(Message.date == anchor_key[0], Message.tg_message_id == anchor_key[1])))
            .order_by(*key_asc)
            .limit(limit - half)
        )).all())
        rows = older + newer
    else:
        rows = list((await db.execute(base.order_by(*key_desc).limit(limit))).all())
        rows.reverse()

    async def _exists(cond) -> bool:
        return bool((await db.execute(select(exists().where(scope, cond)))).scalar())

    if rows:
        oldest = (rows[0][0].date, rows[0][0].tg_message_id)
        newest = (rows[-1][0].date, rows[-1][0].tg_message_id)
        has_older = await _exists(_older_than(oldest))
        has_newer = await _exists(_newer_than(newest))
    else:
        has_older = await _exists(_older_than(after)) if after is not None else False
        has_newer = await _exists(_newer_than(before)) if before is not None else False
    return {"rows": rows, "has_older": has_older, "has_newer": has_newer, "anchor_found": anchor_found}


async def get_oldest_message_id(db: AsyncSession, chat_id: int, topic_id: Optional[int] = None) -> Optional[int]:
    res = await db.execute(
        select(Message.tg_message_id)
        .where(Message.chat_id == chat_id, topic_filter(topic_id))
        .order_by(Message.date.asc(), Message.tg_message_id.asc())
        .limit(1)
    )
    return res.scalar()


# --- Tags ---

async def get_all_tags(db: AsyncSession) -> list[Tag]:
    result = await db.execute(select(Tag).order_by(Tag.is_default.desc(), Tag.name))
    return list(result.scalars().all())


async def get_all_tags_with_counts(db: AsyncSession, include_stage: bool = True) -> list[tuple[Tag, int]]:
    """All tags plus the number of chats each would show in the filtered chat list.

    Counts use the same exclusions as get_chats (hidden/briefing/archived).
    `include_stage=False` hides board-column stage tags (user-facing lists).
    """
    q = (
        select(Tag, func.count(Chat.telegram_id))
        .outerjoin(ChatTag, ChatTag.tag_id == Tag.id)
        .outerjoin(Chat, (Chat.telegram_id == ChatTag.chat_id)
                   & (Chat.is_hidden == False) & (Chat.is_briefing == False) & (Chat.is_archived == False)  # noqa: E712
                   & (Chat.is_deactivated == False))  # noqa: E712
        .group_by(Tag.id)
        .order_by(Tag.is_default.desc(), Tag.name)
    )
    if not include_stage:
        q = q.where(_not_stage())
    result = await db.execute(q)
    return [(tag, count) for tag, count in result.all()]


STAGE_CATEGORY = "stage"  # board-column-owned tags; internal, never user-assigned


def _not_stage():
    return or_(Tag.category.is_(None), Tag.category != STAGE_CATEGORY)


class StageTagLocked(ValueError):
    """A board-column stage tag was targeted through a generic tag API; it can
    only be changed through its column (rename/delete column, move cards)."""

    code = "stage_tag_locked"

    def __init__(self, message: str = "This tag belongs to a board column; change it from the board"):
        super().__init__(message)


class NameConflict(ValueError):
    """A tag/organization with the same case/whitespace-insensitive name exists."""

    def __init__(self, message: str, existing_id: Optional[int] = None):
        super().__init__(message)
        self.existing_id = existing_id


def name_key(value):
    """Case/whitespace-insensitive name key. Mirrors the uq_*_name_ci indexes
    (SQLite lower() is ASCII-only, so always compare in SQL, never in Python)."""
    return func.lower(func.trim(value))


def contains_ci(column, text: str):
    """Case-insensitive substring match with LIKE wildcards in `text` escaped."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(f"%{escaped}%", escape="\\")


async def find_tag_by_name(db: AsyncSession, name: str, include_stage: bool = False) -> Optional[Tag]:
    """User-facing tag lookup by name. Stage tags are board internals and are
    skipped unless asked for, so a column can never be resolved by name."""
    q = select(Tag).where(name_key(Tag.name) == name_key(name.strip()))
    if not include_stage:
        q = q.where(or_(Tag.category.is_(None), Tag.category != STAGE_CATEGORY))
    return (await db.execute(q.order_by(Tag.id).limit(1))).scalar_one_or_none()


async def _free_name_from_stage_tag(db: AsyncSession, name: str) -> None:
    """Stage tags may hold plain names ("Done"); rename one out of the way so a
    user/stream tag can take the name. Column labels are unaffected."""
    holder = (await db.execute(select(Tag).where(
        name_key(Tag.name) == name_key(name.strip()), Tag.category == STAGE_CATEGORY,
    ).limit(1))).scalar_one_or_none()
    if holder is None:
        return
    candidate = f"{holder.name.strip()} · stage {holder.id}"
    n = 1
    while (await db.execute(select(func.count()).select_from(Tag).where(
        name_key(Tag.name) == name_key(candidate)
    ))).scalar():
        n += 1
        candidate = f"{holder.name.strip()} · stage {holder.id}.{n}"
    holder.name = candidate
    await db.flush()


async def create_tag(db: AsyncSession, name: str, color: str = "slate", category: Optional[str] = None) -> Tag:
    """Create a user/stream tag. Raises ValueError on an empty name or the
    reserved stage category, NameConflict if the name is taken."""
    name = name.strip()
    if not name:
        raise ValueError("Tag name cannot be empty")
    if category == STAGE_CATEGORY:
        raise ValueError("Stage tags are managed by board columns")
    existing = await find_tag_by_name(db, name)
    if existing is not None:
        raise NameConflict(f'A tag named "{existing.name}" already exists', existing.id)
    await _free_name_from_stage_tag(db, name)
    tag = Tag(name=name, color=color, category=category, is_default=False)
    db.add(tag)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise NameConflict(f'A tag named "{name}" already exists')
    await db.refresh(tag)
    return tag


async def update_tag(
    db: AsyncSession,
    tag_id: int,
    name: Optional[str] = None,
    color: Optional[str] = None,
    category: Optional[str] = None,
) -> Optional[Tag]:
    """Raises ValueError / NameConflict like create_tag, StageTagLocked for a
    board-column stage tag (rename the column instead)."""
    result = await db.execute(select(Tag).where(Tag.id == tag_id))
    tag = result.scalar_one_or_none()
    if not tag:
        return None
    if tag.category == STAGE_CATEGORY:
        raise StageTagLocked()
    if category is not None:
        new_category = category.strip() or None
        if new_category == STAGE_CATEGORY:
            raise ValueError("Stage tags are managed by board columns")
        tag.category = new_category
    if name is not None:
        new_name = name.strip()
        if not new_name:
            raise ValueError("Tag name cannot be empty")
        clash = (await db.execute(select(Tag).where(
            name_key(Tag.name) == name_key(new_name), Tag.id != tag_id,
            or_(Tag.category.is_(None), Tag.category != STAGE_CATEGORY),
        ).limit(1))).scalar_one_or_none()
        if clash is not None:
            raise NameConflict(f'A tag named "{clash.name}" already exists', clash.id)
        if tag.category != STAGE_CATEGORY:
            await _free_name_from_stage_tag(db, new_name)
        tag.name = new_name
    if color is not None:
        tag.color = color
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise NameConflict("A tag with that name already exists")
    await db.refresh(tag)
    return tag


async def delete_tag(db: AsyncSession, tag_id: int) -> bool:
    """Raises StageTagLocked for a board-column stage tag."""
    result = await db.execute(select(Tag).where(Tag.id == tag_id))
    tag = result.scalar_one_or_none()
    if not tag:
        return False
    if tag.category == STAGE_CATEGORY:
        raise StageTagLocked()
    # Remove all associations first (chats, workspace rules, board columns, board scopes)
    await db.execute(delete(ChatTag).where(ChatTag.tag_id == tag_id))
    await db.execute(delete(WorkspaceTag).where(WorkspaceTag.tag_id == tag_id))
    await db.execute(delete(WorkspaceColumn).where(WorkspaceColumn.tag_id == tag_id))
    await db.execute(update(WorkspaceBoard).where(WorkspaceBoard.scope_tag_id == tag_id).values(scope_tag_id=None))
    await db.delete(tag)
    await db.commit()
    return True


async def get_tags_for_chat(db: AsyncSession, chat_telegram_id: int, include_stage: bool = True) -> list[Tag]:
    q = (
        select(Tag)
        .join(ChatTag, Tag.id == ChatTag.tag_id)
        .where(ChatTag.chat_id == chat_telegram_id)
        .order_by(Tag.name)
    )
    if not include_stage:
        q = q.where(_not_stage())
    return list((await db.execute(q)).scalars().all())


async def add_tag_to_chat(db: AsyncSession, chat_telegram_id: int, tag_id: int) -> Optional[ChatTag]:
    """None if the tag doesn't exist; StageTagLocked for a stage tag (cards are
    placed with move_board_chat)."""
    tag = (await db.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if not tag:
        return None
    if tag.category == STAGE_CATEGORY:
        raise StageTagLocked()
    # Upsert
    existing = (await db.execute(
        select(ChatTag).where(ChatTag.chat_id == chat_telegram_id, ChatTag.tag_id == tag_id)
    )).scalar_one_or_none()
    if existing:
        return existing
    ct = ChatTag(chat_id=chat_telegram_id, tag_id=tag_id)
    db.add(ct)
    try:
        await db.commit()
    except IntegrityError:
        # Concurrent add of the same tag: the row exists, which is the goal
        await db.rollback()
        return (await db.execute(
            select(ChatTag).where(ChatTag.chat_id == chat_telegram_id, ChatTag.tag_id == tag_id)
        )).scalar_one_or_none()
    await db.refresh(ct)
    return ct


async def add_tag_to_chats(db: AsyncSession, chat_telegram_ids: list[int], tag_id: int) -> int:
    """Bulk-assign a tag to many chats. Returns the number of chats newly tagged.
    Raises StageTagLocked for a stage tag."""
    if not chat_telegram_ids:
        return 0
    category = (await db.execute(select(Tag.category).where(Tag.id == tag_id))).scalar()
    if category == STAGE_CATEGORY:
        raise StageTagLocked()
    already = set((await db.execute(
        select(ChatTag.chat_id).where(ChatTag.chat_id.in_(chat_telegram_ids), ChatTag.tag_id == tag_id)
    )).scalars().all())
    missing = [cid for cid in chat_telegram_ids if cid not in already]
    db.add_all(ChatTag(chat_id=cid, tag_id=tag_id) for cid in missing)
    await db.commit()
    return len(missing)


async def remove_tag_from_chat(db: AsyncSession, chat_telegram_id: int, tag_id: int) -> bool:
    """Raises StageTagLocked for a stage tag (use move_board_chat)."""
    category = (await db.execute(select(Tag.category).where(Tag.id == tag_id))).scalar()
    if category == STAGE_CATEGORY:
        raise StageTagLocked()
    result = await db.execute(
        select(ChatTag).where(ChatTag.chat_id == chat_telegram_id, ChatTag.tag_id == tag_id)
    )
    ct = result.scalar_one_or_none()
    if not ct:
        return False
    await db.delete(ct)
    await db.commit()
    return True


# --- Intelligence tools ---

async def search_messages_by_keyword(db: AsyncSession, keyword: str, limit: int = 30) -> tuple[list, int]:
    """Full-text search across all message text (case-insensitive LIKE).

    Returns (rows, total) — rows holds at most `limit` most-recent matches,
    total is the full match count.
    """
    total = (await db.execute(
        select(func.count()).select_from(Message).where(Message.text.ilike(f"%{keyword}%"))
    )).scalar() or 0
    result = await db.execute(
        select(Message, Chat)
        .join(Chat, Message.chat_id == Chat.telegram_id)
        .where(Message.text.ilike(f"%{keyword}%"))
        .order_by(desc(Message.date))
        .limit(limit)
    )
    return result.all(), total


async def get_chat_by_name(db: AsyncSession, name: str) -> Optional[Chat]:
    """Find a chat whose name contains the given string (case-insensitive).

    Prefers the most recently active live chat so name collisions (e.g. a
    migrated group and its supergroup twin) resolve deterministically.
    """
    result = await db.execute(
        select(Chat)
        .where(Chat.name.ilike(f"%{name}%"), Chat.is_deactivated == False)  # noqa: E712
        .order_by(desc(Chat.last_activity_at).nullslast())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _title_match(keyword: str):
    """Space- and case-insensitive title filter: 'ETHTaipei' matches 'ETH Taipei'."""
    kw = keyword.lower().replace(" ", "")
    return func.replace(func.lower(Chat.name), " ", "").like(f"%{kw}%")


async def search_chats_by_title(db: AsyncSession, keyword: str, limit: int = 20) -> tuple[list, int]:
    """Return (chats, total) whose name contains the keyword, ignoring spaces and case.

    `chats` holds at most `limit` rows (alphabetical); `total` is the full match count.
    """
    total = (await db.execute(
        select(func.count()).select_from(Chat).where(_title_match(keyword), Chat.is_deactivated == False)  # noqa: E712
    )).scalar() or 0
    result = await db.execute(
        select(Chat)
        .where(_title_match(keyword), Chat.is_deactivated == False)  # noqa: E712
        .order_by(Chat.name)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def find_chats_by_member_count(
    db: AsyncSession,
    count: int,
    operator: str = "more_than",
    limit: int = 50,
) -> list:
    """Return chats filtered by member count.

    operator: 'more_than' | 'exactly' | 'less_than'
    """
    if operator == "more_than":
        condition = Chat.members_count > count
    elif operator == "exactly":
        condition = Chat.members_count == count
    elif operator == "less_than":
        condition = Chat.members_count < count
    else:
        return []
    result = await db.execute(
        select(Chat)
        .where(Chat.members_count.isnot(None), condition)
        .order_by(Chat.members_count.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_chats_by_tag_name(db: AsyncSession, tag_name: str) -> list:
    """Return all chats that have a tag matching tag_name (case-insensitive)."""
    result = await db.execute(
        select(Chat)
        .join(ChatTag, Chat.telegram_id == ChatTag.chat_id)
        .join(Tag, ChatTag.tag_id == Tag.id)
        .where(Tag.name.ilike(f"%{tag_name}%"), Chat.is_deactivated == False)  # noqa: E712
        .order_by(Chat.name)
    )
    return list(result.scalars().all())


# --- Workspaces ---

def _live_chat_filter():
    """Standard visibility exclusions — must match get_chats so counts never lie."""
    return (
        (Chat.is_hidden == False)  # noqa: E712
        & (Chat.is_briefing == False)  # noqa: E712
        & (Chat.is_deactivated == False)  # noqa: E712
    )


def _workspace_member_ids(workspace_id: int):
    """Scalar subquery of chat telegram_ids matching a workspace's include tags (ANY mode)."""
    include_tags = select(WorkspaceTag.tag_id).where(
        WorkspaceTag.workspace_id == workspace_id,
        WorkspaceTag.is_exclusion == False,  # noqa: E712
    )
    return select(ChatTag.chat_id).where(ChatTag.tag_id.in_(include_tags))


def _workspace_stage_tag_ids(workspace_id: int):
    return select(WorkspaceColumn.tag_id).where(WorkspaceColumn.workspace_id == workspace_id)


def _board_stage_tag_ids(board_id: int):
    return select(WorkspaceColumn.tag_id).where(WorkspaceColumn.board_id == board_id)


def _workspace_scope_tag_ids(workspace_id: int):
    """Stream tags scoping any of this workspace's boards."""
    return select(WorkspaceBoard.scope_tag_id).where(
        WorkspaceBoard.workspace_id == workspace_id,
        WorkspaceBoard.scope_tag_id.isnot(None),
    )


def _protected_tag_ids():
    """Tags with meaning beyond one board column — workspace membership rules
    and board stream scopes. Board mutations never strip or delete these."""
    return select(WorkspaceTag.tag_id).union(
        select(WorkspaceBoard.scope_tag_id).where(WorkspaceBoard.scope_tag_id.isnot(None))
    )


async def _drop_unused_stage_tags(db: AsyncSession, tag_ids: list[int]) -> None:
    """A column's stage tag lives and dies with its last column: delete it and
    its chat assignments once no column uses it. Never touches protected or
    non-stage tags (pre-migration shared state stays intact)."""
    if not tag_ids:
        return
    doomed = list((await db.execute(select(Tag.id).where(
        Tag.id.in_(tag_ids),
        Tag.category == STAGE_CATEGORY,
        Tag.id.notin_(select(WorkspaceColumn.tag_id)),
        Tag.id.notin_(_protected_tag_ids()),
    ))).scalars().all())
    if doomed:
        await db.execute(delete(ChatTag).where(ChatTag.tag_id.in_(doomed)))
        await db.execute(delete(Tag).where(Tag.id.in_(doomed)))


async def _validate_membership_tags(db: AsyncSession, tag_ids: list[int]) -> list[int]:
    """De-duplicated include tags; stage tags (board internals) are rejected."""
    unique_ids = list(dict.fromkeys(tag_ids))
    if not unique_ids:
        return []
    rows = (await db.execute(select(Tag.id, Tag.category).where(Tag.id.in_(unique_ids)))).all()
    found = {tid: cat for tid, cat in rows}
    missing = [tid for tid in unique_ids if tid not in found]
    if missing:
        raise ValueError(f"Unknown tag id(s): {', '.join(map(str, missing))}")
    if any(cat == STAGE_CATEGORY for cat in found.values()):
        raise ValueError("Board column tags cannot be used as workspace tags")
    return unique_ids


async def get_workspaces_with_counts(db: AsyncSession) -> list[dict]:
    """All workspaces ordered by position, each with tag_ids, member and unallocated counts.

    Constant number of queries regardless of workspace count."""
    from sqlalchemy.orm import aliased

    workspaces = list((await db.execute(
        select(Workspace).order_by(Workspace.position, Workspace.id)
    )).scalars().all())
    if not workspaces:
        return []

    tag_ids_by_ws: dict[int, list[int]] = {}
    for ws_id, tag_id in (await db.execute(
        select(WorkspaceTag.workspace_id, WorkspaceTag.tag_id)
        .where(WorkspaceTag.is_exclusion == False)  # noqa: E712
        .order_by(WorkspaceTag.id)
    )).all():
        tag_ids_by_ws.setdefault(ws_id, []).append(tag_id)

    member_counts = dict((await db.execute(
        select(WorkspaceTag.workspace_id, func.count(func.distinct(Chat.telegram_id)))
        .join(ChatTag, ChatTag.tag_id == WorkspaceTag.tag_id)
        .join(Chat, Chat.telegram_id == ChatTag.chat_id)
        .where(WorkspaceTag.is_exclusion == False, _live_chat_filter())  # noqa: E712
        .group_by(WorkspaceTag.workspace_id)
    )).all())

    member_ct = aliased(ChatTag)
    stage_ct = aliased(ChatTag)
    is_member = (
        select(member_ct.id)
        .join(WorkspaceTag, WorkspaceTag.tag_id == member_ct.tag_id)
        .where(
            member_ct.chat_id == Chat.telegram_id,
            WorkspaceTag.workspace_id == WorkspaceChatOverride.workspace_id,
            WorkspaceTag.is_exclusion == False,  # noqa: E712
        )
        .exists()
    )
    is_staged = (
        select(stage_ct.id)
        .join(WorkspaceColumn, WorkspaceColumn.tag_id == stage_ct.tag_id)
        .where(
            stage_ct.chat_id == Chat.telegram_id,
            WorkspaceColumn.workspace_id == WorkspaceChatOverride.workspace_id,
        )
        .exists()
    )
    unallocated_counts = dict((await db.execute(
        select(WorkspaceChatOverride.workspace_id, func.count(func.distinct(Chat.telegram_id)))
        .join(Chat, Chat.telegram_id == WorkspaceChatOverride.chat_id)
        .join(Workspace, Workspace.id == WorkspaceChatOverride.workspace_id)
        .where(
            Workspace.view_mode == "kanban",
            WorkspaceChatOverride.needs_triage_since.isnot(None),
            _live_chat_filter(),
            is_member,
            ~is_staged,
        )
        .group_by(WorkspaceChatOverride.workspace_id)
    )).all())

    return [
        {"workspace": ws, "tag_ids": tag_ids_by_ws.get(ws.id, []),
         "member_count": member_counts.get(ws.id, 0),
         "unallocated_count": unallocated_counts.get(ws.id, 0)}
        for ws in workspaces
    ]


async def create_workspace(
    db: AsyncSession, name: str, emoji: Optional[str], color: str, tag_ids: list[int], view_mode: str = "list"
) -> Workspace:
    """Raises ValueError for unknown or stage tag_ids."""
    tag_ids = await _validate_membership_tags(db, tag_ids)
    max_pos = (await db.execute(select(func.max(Workspace.position)))).scalar()
    ws = Workspace(
        name=name, emoji=emoji or None, color=color, view_mode=view_mode,
        position=(max_pos or 0) + 1,
        board_watermark=datetime.utcnow() if view_mode == "kanban" else None,
    )
    db.add(ws)
    await db.flush()
    for tid in tag_ids:
        db.add(WorkspaceTag(workspace_id=ws.id, tag_id=tid))
    if view_mode == "kanban":
        db.add(WorkspaceBoard(workspace_id=ws.id, name="Main", position=0))
    await db.commit()
    await db.refresh(ws)
    return ws


async def update_workspace(
    db: AsyncSession, workspace_id: int,
    name: Optional[str] = None, emoji: Optional[str] = None, color: Optional[str] = None,
    tag_ids: Optional[list[int]] = None, view_mode: Optional[str] = None,
    clear_emoji: bool = False,
) -> Optional[Workspace]:
    """None = unchanged; `clear_emoji` removes the emoji. Raises ValueError for
    unknown or stage tag_ids."""
    ws = (await db.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one_or_none()
    if not ws:
        return None
    if tag_ids is not None:
        tag_ids = await _validate_membership_tags(db, tag_ids)
    if name is not None:
        ws.name = name
    if clear_emoji:
        ws.emoji = None
    elif emoji is not None:
        ws.emoji = emoji
    if color is not None:
        ws.color = color
    if view_mode is not None:
        if view_mode == "kanban" and ws.view_mode != "kanban":
            if ws.board_watermark is None:
                ws.board_watermark = datetime.utcnow()  # board starts clean; only future activity triages
            has_board = (await db.execute(
                select(func.count()).select_from(WorkspaceBoard).where(WorkspaceBoard.workspace_id == workspace_id)
            )).scalar() or 0
            if not has_board:
                db.add(WorkspaceBoard(workspace_id=workspace_id, name="Main", position=0))
        ws.view_mode = view_mode
    if tag_ids is not None:
        await db.execute(delete(WorkspaceTag).where(
            WorkspaceTag.workspace_id == workspace_id,
            WorkspaceTag.is_exclusion == False,  # noqa: E712
        ))
        for tid in tag_ids:
            db.add(WorkspaceTag(workspace_id=workspace_id, tag_id=tid))
    await db.commit()
    await db.refresh(ws)
    return ws


async def delete_workspace(db: AsyncSession, workspace_id: int) -> bool:
    ws = (await db.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one_or_none()
    if not ws:
        return False
    stage_ids = list((await db.execute(_workspace_stage_tag_ids(workspace_id))).scalars().all())
    await db.execute(delete(WorkspaceTag).where(WorkspaceTag.workspace_id == workspace_id))
    await db.execute(delete(WorkspaceColumn).where(WorkspaceColumn.workspace_id == workspace_id))
    await db.execute(delete(WorkspaceBoard).where(WorkspaceBoard.workspace_id == workspace_id))
    await db.execute(delete(WorkspaceChatOverride).where(WorkspaceChatOverride.workspace_id == workspace_id))
    # The columns' stage tags (and their card placements) die with the workspace
    await _drop_unused_stage_tags(db, stage_ids)
    await db.delete(ws)
    await db.commit()
    return True


async def get_workspace_boards(db: AsyncSession, workspace_id: int) -> list[tuple]:
    """(WorkspaceBoard, scope_tag_name|None) ordered by position."""
    return list((await db.execute(
        select(WorkspaceBoard, Tag.name)
        .outerjoin(Tag, Tag.id == WorkspaceBoard.scope_tag_id)
        .where(WorkspaceBoard.workspace_id == workspace_id)
        .order_by(WorkspaceBoard.position, WorkspaceBoard.id)
    )).all())


async def _resolve_stream_tag(db: AsyncSession, name: str) -> Tag:
    """Stream (scope) tag by name: reuses a user/stream tag (oldest wins on
    legacy case-variants), never a stage tag; creates a 'stream' tag otherwise."""
    name = name.strip()
    tag = await find_tag_by_name(db, name)
    if not tag:
        await _free_name_from_stage_tag(db, name)
        tag = Tag(name=name, color="slate", category="stream", is_default=False)
        db.add(tag)
        await db.flush()
    return tag


async def create_workspace_board(
    db: AsyncSession, workspace_id: int, name: str, scope_tag_name: Optional[str] = None
) -> Optional[WorkspaceBoard]:
    """Returns None on duplicate name within the workspace."""
    dup = (await db.execute(select(func.count()).select_from(WorkspaceBoard).where(
        WorkspaceBoard.workspace_id == workspace_id, name_key(WorkspaceBoard.name) == name_key(name)
    ))).scalar() or 0
    if dup:
        return None
    scope_tag_id = None
    if scope_tag_name:
        scope_tag_id = (await _resolve_stream_tag(db, scope_tag_name)).id
    max_pos = (await db.execute(
        select(func.max(WorkspaceBoard.position)).where(WorkspaceBoard.workspace_id == workspace_id)
    )).scalar()
    board = WorkspaceBoard(workspace_id=workspace_id, name=name, scope_tag_id=scope_tag_id,
                           position=(max_pos or 0) + 1)
    db.add(board)
    await db.commit()
    await db.refresh(board)
    return board


async def update_workspace_board(
    db: AsyncSession, workspace_id: int, board_id: int,
    name: Optional[str] = None, scope_tag_name: Optional[str] = None,
    clear_scope: bool = False, position: Optional[int] = None,
) -> str:
    """Returns 'ok', 'not_found' or 'conflict' (another board has that name)."""
    board = (await db.execute(select(WorkspaceBoard).where(
        WorkspaceBoard.id == board_id, WorkspaceBoard.workspace_id == workspace_id
    ))).scalar_one_or_none()
    if not board:
        return "not_found"
    if name is not None:
        dup = (await db.execute(select(func.count()).select_from(WorkspaceBoard).where(
            WorkspaceBoard.workspace_id == workspace_id, WorkspaceBoard.id != board_id,
            name_key(WorkspaceBoard.name) == name_key(name),
        ))).scalar() or 0
        if dup:
            return "conflict"
        board.name = name
    if clear_scope:
        board.scope_tag_id = None
    elif scope_tag_name and scope_tag_name.strip():
        board.scope_tag_id = (await _resolve_stream_tag(db, scope_tag_name)).id
    if position is not None:
        board.position = position
    await db.commit()
    return "ok"


async def delete_workspace_board(
    db: AsyncSession, workspace_id: int, board_id: int, remove_tags: bool = True
) -> bool:
    """Deletes the board, its columns and their stage tags (cards return to
    backlog). `remove_tags` is accepted for API compatibility and ignored: a
    column's stage tag has no meaning once its column is gone."""
    board = (await db.execute(select(WorkspaceBoard).where(
        WorkspaceBoard.id == board_id, WorkspaceBoard.workspace_id == workspace_id
    ))).scalar_one_or_none()
    if not board:
        return False
    stage_ids = list((await db.execute(_board_stage_tag_ids(board_id))).scalars().all())
    await db.execute(delete(WorkspaceColumn).where(WorkspaceColumn.board_id == board_id))
    await db.execute(delete(WorkspaceBoard).where(WorkspaceBoard.id == board_id))
    await _drop_unused_stage_tags(db, stage_ids)
    await db.commit()
    return True


async def get_board(db: AsyncSession, workspace_id: int, board_id: Optional[int] = None) -> Optional[dict]:
    """Assemble one board of a workspace: its columns, unallocated (triage), backlog —
    plus the boards list for tab rendering (with per-tab unallocated counts)."""
    ws = (await db.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one_or_none()
    if not ws:
        return None
    board_rows = await get_workspace_boards(db, workspace_id)
    active = None
    if board_rows:
        active = next((b for b, _n in board_rows if b.id == board_id), None) or board_rows[0][0]

    members = list((await db.execute(
        select(Chat).where(Chat.telegram_id.in_(_workspace_member_ids(workspace_id)), _live_chat_filter())
    )).scalars().all())
    member_ids = [c.telegram_id for c in members]

    # triage flags (workspace-level)
    triage: dict[int, datetime] = {}
    if member_ids:
        rows = (await db.execute(
            select(WorkspaceChatOverride.chat_id, WorkspaceChatOverride.needs_triage_since).where(
                WorkspaceChatOverride.workspace_id == workspace_id,
                WorkspaceChatOverride.needs_triage_since.isnot(None),
                WorkspaceChatOverride.chat_id.in_(member_ids),
            )
        )).all()
        triage = {cid: ts for cid, ts in rows}

    # which chats carry which of this workspace's stream (scope) tags
    scope_tag_ids = [b.scope_tag_id for b, _n in board_rows if b.scope_tag_id is not None]
    scope_carriers: dict[int, set[int]] = {tid: set() for tid in scope_tag_ids}  # tag_id -> chat_ids
    if scope_tag_ids and member_ids:
        rows = (await db.execute(
            select(ChatTag.chat_id, ChatTag.tag_id).where(
                ChatTag.tag_id.in_(scope_tag_ids), ChatTag.chat_id.in_(member_ids)
            )
        )).all()
        for cid, tid in rows:
            scope_carriers[tid].add(cid)
    has_any_stream: set[int] = set().union(*scope_carriers.values()) if scope_carriers else set()

    # Chats staged on ANY board of this workspace (dragging onto a board is
    # itself a classification decision, even before stream tags are set up)
    staged_anywhere: set[int] = set()
    if member_ids:
        staged_anywhere = set((await db.execute(
            select(ChatTag.chat_id).where(
                ChatTag.tag_id.in_(_workspace_stage_tag_ids(workspace_id)),
                ChatTag.chat_id.in_(member_ids),
            )
        )).scalars().all())

    def _unallocated_ids_for(board) -> set[int]:
        # stream-less triage chats surface on every tab; streamed ones only on their own
        eligible = set()
        for cid in triage:
            if cid not in has_any_stream:
                eligible.add(cid)
            elif board.scope_tag_id is not None and cid in scope_carriers.get(board.scope_tag_id, set()):
                eligible.add(cid)
        return eligible

    boards_out = [
        {"id": b.id, "name": b.name, "scope_tag_id": b.scope_tag_id, "scope_tag_name": scope_name,
         "position": b.position, "unallocated_count": len(_unallocated_ids_for(b))}
        for b, scope_name in board_rows
    ]

    if active is None:
        return {"workspace": ws, "boards": boards_out, "board_id": None,
                "columns": [], "unallocated": [], "backlog": {"active": [], "cooling": [], "dormant": []}}

    columns = list((await db.execute(
        select(WorkspaceColumn, Tag)
        .join(Tag, Tag.id == WorkspaceColumn.tag_id)
        .where(WorkspaceColumn.board_id == active.id)
        .order_by(WorkspaceColumn.position, WorkspaceColumn.id)
    )).all())
    stage_tag_ids = [col.tag_id for col, _tag in columns]

    # chat -> stage tag (within this board)
    staged: dict[int, int] = {}
    if stage_tag_ids and member_ids:
        rows = (await db.execute(
            select(ChatTag.chat_id, ChatTag.tag_id).where(
                ChatTag.tag_id.in_(stage_tag_ids), ChatTag.chat_id.in_(member_ids)
            )
        )).all()
        for chat_id, tag_id in rows:
            staged.setdefault(chat_id, tag_id)  # leftmost-wins handled by column order at render

    unallocated_ids = _unallocated_ids_for(active)
    in_scope = (
        (lambda c: c.telegram_id in scope_carriers.get(active.scope_tag_id, set()))
        if active.scope_tag_id is not None else (lambda c: True)
    )

    by_column: dict[int, list[Chat]] = {tid: [] for tid in stage_tag_ids}
    unallocated: list[Chat] = []
    backlog: list[Chat] = []
    for c in members:
        if c.telegram_id in staged:
            by_column[staged[c.telegram_id]].append(c)
        elif c.telegram_id in unallocated_ids:
            unallocated.append(c)
        elif in_scope(c):
            backlog.append(c)

    def _recent_first(chats: list[Chat]) -> list[Chat]:
        return sorted(chats, key=lambda c: c.last_activity_at or datetime.min, reverse=True)

    now = datetime.utcnow()
    b_active, b_cooling, b_dormant = [], [], []
    for c in _recent_first(backlog):
        age = (now - c.last_activity_at).days if c.last_activity_at else None
        if age is not None and age < 7:
            b_active.append(c)
        elif age is not None and age < 30:
            b_cooling.append(c)
        else:
            b_dormant.append(c)

    return {
        "workspace": ws,
        "boards": boards_out,
        "board_id": active.id,
        "columns": [
            {"tag_id": col.tag_id, "name": col.label or tag.name, "color": col.color or tag.color,
             "position": col.position, "chats": _recent_first(by_column[col.tag_id])}
            for col, tag in columns
        ],
        "unallocated": _recent_first(unallocated),
        "backlog": {"active": b_active, "cooling": b_cooling, "dormant": b_dormant},
        # Members already classified — carrying a stream tag OR staged on any of
        # this workspace's boards. Lets the UI offer an "unclassified only"
        # backlog view for triage.
        "classified_chat_ids": sorted(has_any_stream | staged_anywhere),
    }


async def _get_board_row(db: AsyncSession, workspace_id: int, board_id: int) -> Optional[WorkspaceBoard]:
    return (await db.execute(select(WorkspaceBoard).where(
        WorkspaceBoard.id == board_id, WorkspaceBoard.workspace_id == workspace_id
    ))).scalar_one_or_none()


async def _label_taken(db: AsyncSession, board_id: int, label: str, except_col_id: Optional[int] = None) -> bool:
    """One label per board, case/whitespace-insensitively."""
    q = (
        select(func.count()).select_from(WorkspaceColumn)
        .join(Tag, Tag.id == WorkspaceColumn.tag_id)
        .where(WorkspaceColumn.board_id == board_id,
               name_key(func.coalesce(WorkspaceColumn.label, Tag.name)) == name_key(label))
    )
    if except_col_id is not None:
        q = q.where(WorkspaceColumn.id != except_col_id)
    return bool((await db.execute(q)).scalar())


async def add_board_column(
    db: AsyncSession, workspace_id: int, board_id: int, name: str, color: Optional[str]
) -> tuple[str, Optional[dict]]:
    """Attach a column with display label `name`. Returns (status, column) with
    status 'ok', 'not_found' or 'conflict' (label already on this board).

    Every column owns a freshly minted category='stage' tag — never one
    resolved by name — so dragging cards or deleting the column can only ever
    affect this column's own placements. The tag's internal name is unique
    ("Done", else "Done · <board>", ...); the label is what the board shows.
    """
    board = await _get_board_row(db, workspace_id, board_id)
    if not board:
        return "not_found", None
    if await _label_taken(db, board_id, name):
        return "conflict", None

    candidate = name
    n = 1
    while (await db.execute(select(func.count()).select_from(Tag).where(
        name_key(Tag.name) == name_key(candidate)
    ))).scalar():
        n += 1
        candidate = f"{name} · {board.name}" if n == 2 else f"{name} · {board.name} {n - 1}"
    tag = Tag(name=candidate, color=color or "slate", category=STAGE_CATEGORY, is_default=False)
    db.add(tag)
    await db.flush()

    max_pos = (await db.execute(
        select(func.max(WorkspaceColumn.position)).where(WorkspaceColumn.board_id == board_id)
    )).scalar()
    col = WorkspaceColumn(workspace_id=workspace_id, tag_id=tag.id, board_id=board_id,
                          label=name, position=(max_pos or 0) + 1, color=color)
    db.add(col)
    await db.commit()
    return "ok", {"tag_id": tag.id, "name": name, "color": col.color or tag.color, "position": col.position}


async def update_board_column(
    db: AsyncSession, workspace_id: int, board_id: int, tag_id: int,
    name: Optional[str] = None, color: Optional[str] = None, position: Optional[int] = None,
) -> str:
    """Returns 'ok', 'not_found' or 'conflict' (label already on this board).
    Rename changes the display label; the backing stage tag follows when the
    name is free (it is internal, so a clash just leaves the old tag name)."""
    col = (await db.execute(select(WorkspaceColumn).where(
        WorkspaceColumn.workspace_id == workspace_id, WorkspaceColumn.tag_id == tag_id,
        WorkspaceColumn.board_id == board_id,
    ))).scalar_one_or_none()
    if not col:
        return "not_found"
    if name is not None:
        if await _label_taken(db, board_id, name, except_col_id=col.id):
            return "conflict"
        col.label = name
        tag = (await db.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
        name_free = ((await db.execute(select(func.count()).select_from(Tag).where(
            name_key(Tag.name) == name_key(name), Tag.id != tag_id
        ))).scalar() or 0) == 0
        sole_user = ((await db.execute(select(func.count()).select_from(WorkspaceColumn).where(
            WorkspaceColumn.tag_id == tag_id, WorkspaceColumn.id != col.id
        ))).scalar() or 0) == 0
        if tag is not None and tag.category == STAGE_CATEGORY and sole_user and name_free:
            tag.name = name
    if color is not None:
        col.color = color
    if position is not None:
        col.position = position
    await db.commit()
    return "ok"


async def reorder_board_columns(
    db: AsyncSession, workspace_id: int, board_id: int, tag_ids: list[int]
) -> str:
    """Set column order in one transaction. `tag_ids` must list exactly the
    board's columns. Returns 'ok', 'not_found' or 'mismatch'."""
    if not await _get_board_row(db, workspace_id, board_id):
        return "not_found"
    cols = list((await db.execute(select(WorkspaceColumn).where(
        WorkspaceColumn.workspace_id == workspace_id, WorkspaceColumn.board_id == board_id,
    ))).scalars().all())
    by_tag = {c.tag_id: c for c in cols}
    if len(tag_ids) != len(set(tag_ids)) or set(tag_ids) != set(by_tag):
        return "mismatch"
    for pos, tid in enumerate(tag_ids):
        by_tag[tid].position = pos
    await db.commit()
    return "ok"


async def delete_board_column(
    db: AsyncSession, workspace_id: int, board_id: int, tag_id: int, remove_tags: bool = True
) -> bool:
    """Delete a column; its stage tag and card placements go with it (cards
    return to backlog). `remove_tags` is accepted for API compatibility and
    ignored. A protected or still-shared tag is never stripped."""
    col = (await db.execute(select(WorkspaceColumn).where(
        WorkspaceColumn.workspace_id == workspace_id, WorkspaceColumn.tag_id == tag_id,
        WorkspaceColumn.board_id == board_id,
    ))).scalar_one_or_none()
    if not col:
        return False
    await db.execute(delete(WorkspaceColumn).where(WorkspaceColumn.id == col.id))
    await _drop_unused_stage_tags(db, [tag_id])
    await db.commit()
    return True


async def move_board_chat(
    db: AsyncSession, workspace_id: int, board_id: int, chat_id: int, to_tag_id: Optional[int]
) -> bool:
    """Move a chat between one board's columns (None = back to backlog / dismiss triage).

    Only THIS board's stage tags are touched — a chat can sit in independent
    pipelines on different stream boards. Allocating on a stream-scoped board
    also assigns the stream tag (one-gesture stream + stage). Protected tags
    (workspace membership / stream scopes) are never stripped, so a move can
    never remove a chat from a workspace.
    """
    board = await _get_board_row(db, workspace_id, board_id)
    if not board:
        return False
    stage_ids = list((await db.execute(_board_stage_tag_ids(board_id))).scalars().all())
    if to_tag_id is not None and to_tag_id not in stage_ids:
        return False
    strip_ids = [tid for tid in stage_ids if tid != to_tag_id]
    if strip_ids:
        await db.execute(delete(ChatTag).where(
            ChatTag.chat_id == chat_id,
            ChatTag.tag_id.in_(strip_ids),
            ChatTag.tag_id.notin_(_protected_tag_ids()),
        ))
    add_ids = [tid for tid in (to_tag_id, board.scope_tag_id if to_tag_id is not None else None)
               if tid is not None]
    if add_ids:
        have = set((await db.execute(select(ChatTag.tag_id).where(
            ChatTag.chat_id == chat_id, ChatTag.tag_id.in_(add_ids)
        ))).scalars().all())
        for tid in add_ids:
            if tid not in have:
                db.add(ChatTag(chat_id=chat_id, tag_id=tid))
    override = (await db.execute(select(WorkspaceChatOverride).where(
        WorkspaceChatOverride.workspace_id == workspace_id, WorkspaceChatOverride.chat_id == chat_id
    ))).scalar_one_or_none()
    if not override:
        override = WorkspaceChatOverride(workspace_id=workspace_id, chat_id=chat_id)
        db.add(override)
    override.needs_triage_since = None
    override.triaged_at = datetime.utcnow()
    await db.commit()
    return True


async def mark_workspace_triage(db: AsyncSession, chat_id: int, msg_date: datetime) -> None:
    """Surface a board-less workspace member in the Unallocated column after incoming activity.

    Called from live message events and post-sync (both paths, or wake-ups
    discovered while the app was closed would be silently missed).
    """
    workspaces = list((await db.execute(
        select(Workspace).where(Workspace.view_mode == "kanban", Workspace.board_watermark.isnot(None))
    )).scalars().all())
    for ws in workspaces:
        if ws.board_watermark and msg_date <= ws.board_watermark:
            continue
        is_member = (await db.execute(
            select(func.count()).select_from(ChatTag).where(
                ChatTag.chat_id == chat_id,
                ChatTag.tag_id.in_(select(WorkspaceTag.tag_id).where(
                    WorkspaceTag.workspace_id == ws.id,
                    WorkspaceTag.is_exclusion == False,  # noqa: E712
                )),
            )
        )).scalar() or 0
        if not is_member:
            continue
        is_staged = (await db.execute(
            select(func.count()).select_from(ChatTag).where(
                ChatTag.chat_id == chat_id,
                ChatTag.tag_id.in_(_workspace_stage_tag_ids(ws.id)),
            )
        )).scalar() or 0
        if is_staged:
            continue
        override = (await db.execute(select(WorkspaceChatOverride).where(
            WorkspaceChatOverride.workspace_id == ws.id, WorkspaceChatOverride.chat_id == chat_id
        ))).scalar_one_or_none()
        if override is None:
            db.add(WorkspaceChatOverride(workspace_id=ws.id, chat_id=chat_id, needs_triage_since=msg_date))
        elif override.needs_triage_since is None:
            # Dismissed episodes resurface only on activity newer than the dismissal
            if override.triaged_at is None or msg_date > override.triaged_at:
                override.needs_triage_since = msg_date
    await db.commit()


async def search_messages_in_tagged_chats(db: AsyncSession, tag_name: str, keyword: str) -> list:
    """Per-chat rollup of keyword matches within chats carrying a tag.

    Returns [(chat_name, match_count, latest_snippet, latest_date)], most matches first.
    """
    tagged_chat_ids = (
        select(ChatTag.chat_id)
        .join(Tag, ChatTag.tag_id == Tag.id)
        .where(Tag.name.ilike(f"%{tag_name}%"))
    )
    row_num = func.row_number().over(
        partition_by=Message.chat_id,
        order_by=desc(Message.date),
    ).label("rn")
    matches = (
        select(
            Message.chat_id,
            Message.text.label("snippet"),
            Message.date.label("msg_date"),
            row_num,
        )
        .where(Message.text.ilike(f"%{keyword}%"), Message.chat_id.in_(tagged_chat_ids))
        .subquery()
    )
    result = await db.execute(
        select(
            Chat.name,
            func.count().label("match_count"),
            func.max(matches.c.snippet).filter(matches.c.rn == 1).label("latest_snippet"),
            func.max(matches.c.msg_date).label("latest_date"),
        )
        .select_from(matches)
        .join(Chat, Chat.telegram_id == matches.c.chat_id)
        .group_by(matches.c.chat_id)
        .order_by(desc("match_count"))
    )
    return result.all()


async def get_tag_sync_coverage(db: AsyncSession, tag_name: str) -> tuple[int, int, list[int]]:
    """Sync coverage for chats carrying a tag.

    Returns (total_tagged, with_messages, unsynced_chat_ids).
    """
    tagged = (
        select(Chat)
        .join(ChatTag, Chat.telegram_id == ChatTag.chat_id)
        .join(Tag, ChatTag.tag_id == Tag.id)
        .where(Tag.name.ilike(f"%{tag_name}%"), Chat.is_deactivated == False)  # noqa: E712
        .subquery()
    )
    total = (await db.execute(select(func.count()).select_from(tagged))).scalar() or 0
    with_messages = (await db.execute(
        select(func.count(func.distinct(Message.chat_id)))
        .where(Message.chat_id.in_(select(tagged.c.telegram_id)))
    )).scalar() or 0
    unsynced_ids = list((await db.execute(
        select(tagged.c.telegram_id).where(tagged.c.last_synced_at.is_(None))
    )).scalars().all())
    return total, with_messages, unsynced_ids


# --- Mentions ---

async def get_mentions(
    db: AsyncSession,
    username: str,
    limit: int = 50,
    days: int = 7,
    group_name_filter: str = "",
    tag_name_filter: str = "",
    user_id: Optional[int] = None,
) -> list:
    """
    Return messages where the current user is mentioned,
    limited to non-outgoing messages in group/channel chats.
    Detects both @username text mentions and entity-based inline mentions
    (where user_id appears in text_html mention links).
    Each row is a (Message, Chat) tuple.
    """
    since = datetime.utcnow() - timedelta(days=days)

    # Match @username in text OR user_id in entity mention links
    mention_conditions = [Message.text.ilike(f"%@{username}%")]
    if user_id:
        mention_conditions.append(Message.text_html.like(f'%/profile/{user_id}"%'))

    q = (
        select(Message, Chat)
        .join(Chat, Message.chat_id == Chat.telegram_id)
        .where(
            Message.is_outgoing == False,  # noqa: E712
            Chat.type.in_(["group", "channel", "supergroup"]),
            or_(*mention_conditions),
            Message.date >= since,
        )
    )
    if group_name_filter:
        q = q.where(Chat.name.ilike(f"%{group_name_filter}%"))
    if tag_name_filter:
        q = (
            q.join(ChatTag, Chat.telegram_id == ChatTag.chat_id)
            .join(Tag, ChatTag.tag_id == Tag.id)
            .where(Tag.name.ilike(f"%{tag_name_filter}%"))
        )
    q = q.order_by(desc(Message.date)).limit(limit)
    result = await db.execute(q)
    return result.all()


async def check_replied_after(db: AsyncSession, chat_id: int, after_date: datetime) -> bool:
    """Return True if there is at least one outgoing message in the chat after the given date."""
    result = await db.execute(
        select(Message)
        .where(
            Message.chat_id == chat_id,
            Message.is_outgoing == True,  # noqa: E712
            Message.date > after_date,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def get_first_reply_after(
    db: AsyncSession,
    chat_id: int,
    after_telegram_id: int,
) -> Optional[Message]:
    """Return the first outgoing message with telegram_id > after_telegram_id in the chat."""
    result = await db.execute(
        select(Message)
        .where(
            Message.chat_id == chat_id,
            Message.tg_message_id > after_telegram_id,
            Message.is_outgoing == True,  # noqa: E712
        )
        .order_by(Message.tg_message_id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def count_messages_between(
    db: AsyncSession,
    chat_id: int,
    from_telegram_id: int,
    to_telegram_id: int,
) -> int:
    """Count messages strictly between two telegram_ids (exclusive) in a chat."""
    result = await db.execute(
        select(func.count()).where(
            Message.chat_id == chat_id,
            Message.tg_message_id > from_telegram_id,
            Message.tg_message_id < to_telegram_id,
        )
    )
    return result.scalar() or 0


async def get_mention_contexts(
    db: AsyncSession,
    mentions: list[tuple[int, int]],
    before: int = 2,
    after: int = 2,
) -> dict[tuple[int, int], dict]:
    """Batch version of get_messages_around + get_first_reply_after for many
    (chat_id, tg_message_id) anchors, in one compound query.

    Returns {(chat_id, tg_id): {"before": [...], "after": [...], "reply": Message|None}}
    with before/after oldest-first. Callers needing N "after" messages for
    display plus gap detection should request N+1.
    """
    from sqlalchemy import union_all

    out = {key: {"before": [], "after": [], "reply": None} for key in mentions}
    if not mentions:
        return out
    parts = []
    for chat_id, tg_id in mentions:
        b = (
            select(Message.id.label("mid"))
            .where(Message.chat_id == chat_id, Message.tg_message_id < tg_id)
            .order_by(desc(Message.tg_message_id)).limit(before).subquery()
        )
        a = (
            select(Message.id.label("mid"))
            .where(Message.chat_id == chat_id, Message.tg_message_id > tg_id)
            .order_by(Message.tg_message_id).limit(after).subquery()
        )
        r = (
            select(Message.id.label("mid"))
            .where(
                Message.chat_id == chat_id,
                Message.tg_message_id > tg_id,
                Message.is_outgoing == True,  # noqa: E712
            )
            .order_by(Message.tg_message_id).limit(1).subquery()
        )
        parts.extend([select(b.c.mid), select(a.c.mid), select(r.c.mid)])
    # SQLite caps compound SELECTs at 500 members; chunk to stay well below.
    ids: set = set()
    for i in range(0, len(parts), 300):
        chunk = parts[i:i + 300]
        stmt = chunk[0] if len(chunk) == 1 else union_all(*chunk)
        ids.update((await db.execute(stmt)).scalars().all())
    if not ids:
        return out
    rows = (await db.execute(
        select(Message).where(Message.id.in_(ids)).order_by(Message.chat_id, Message.tg_message_id)
    )).scalars().all()
    by_chat: dict[int, list] = {}
    for m in rows:
        by_chat.setdefault(m.chat_id, []).append(m)
    for (chat_id, tg_id), ctx in out.items():
        msgs = by_chat.get(chat_id, [])
        ctx["before"] = [m for m in msgs if m.tg_message_id < tg_id][-before:] if before else []
        ctx["after"] = [m for m in msgs if m.tg_message_id > tg_id][:after]
        ctx["reply"] = next((m for m in msgs if m.tg_message_id > tg_id and m.is_outgoing), None)
    return out


async def get_tags_for_chats(db: AsyncSession, chat_telegram_ids: list[int]) -> dict[int, list[Tag]]:
    """Tags of many chats in one query, keyed by chat telegram_id."""
    out: dict[int, list[Tag]] = {cid: [] for cid in chat_telegram_ids}
    if not chat_telegram_ids:
        return out
    result = await db.execute(
        select(ChatTag.chat_id, Tag)
        .join(Tag, ChatTag.tag_id == Tag.id)
        .where(ChatTag.chat_id.in_(chat_telegram_ids))
        .order_by(Tag.name)
    )
    for chat_id, tag in result.all():
        out.setdefault(chat_id, []).append(tag)
    return out


# --- Needs reply (dashboard + catch-up) ---

async def get_needs_reply(
    db: AsyncSession,
    limit: int = 10,
    now: Optional[datetime] = None,
    handled_limit: int = 10,
) -> tuple[list, list]:
    """Chats waiting on the user, with catch-up triage applied BEFORE the limit.

    A chat needs a reply when its latest message is incoming and un-reacted,
    and it is tracked or a private chat with an important contact (hidden,
    briefing-only and deactivated chats excluded). Triage then removes:
      - handled items, unless a new message arrived after handled_at;
      - items snoozed until a future time.

    Returns (active, recently_handled); each row is
    (Chat, last_sender_name, last_text, last_date, CatchUpItem | None).
    """
    from backend.db.models import CatchUpItem

    now = now or datetime.utcnow()
    important_ids = select(Contact.telegram_id).where(Contact.is_important == True)  # noqa: E712
    relevant = or_(
        Chat.is_tracked == True,  # noqa: E712
        and_(Chat.type == "private", Chat.telegram_id.in_(important_ids)),
    )
    last_msg = (
        select(
            Message.chat_id,
            Message.sender_name,
            func.substr(Message.text, 1, 200).label("text"),
            Message.date,
            Message.is_outgoing,
            Message.my_reaction,
            func.row_number().over(
                partition_by=Message.chat_id,
                order_by=(desc(Message.date), desc(Message.tg_message_id)),
            ).label("rn"),
        )
        .subquery()
    )
    base = (
        select(Chat, last_msg.c.sender_name, last_msg.c.text, last_msg.c.date, CatchUpItem)
        .join(last_msg, and_(Chat.telegram_id == last_msg.c.chat_id, last_msg.c.rn == 1))
        .outerjoin(CatchUpItem, CatchUpItem.chat_telegram_id == Chat.telegram_id)
        .where(
            last_msg.c.is_outgoing == False,  # noqa: E712
            last_msg.c.my_reaction == None,  # noqa: E711
            Chat.is_hidden == False,  # noqa: E712
            Chat.is_briefing == False,  # noqa: E712
            Chat.is_deactivated == False,  # noqa: E712
            relevant,
        )
    )
    handled_current = and_(
        CatchUpItem.status == "handled",
        or_(CatchUpItem.handled_at == None, CatchUpItem.handled_at >= last_msg.c.date),  # noqa: E711
    )
    snoozed_future = and_(
        CatchUpItem.status == "snoozed",
        CatchUpItem.snoozed_until != None,  # noqa: E711
        CatchUpItem.snoozed_until > now,
    )
    active = (await db.execute(
        base.where(or_(CatchUpItem.id == None, and_(~handled_current, ~snoozed_future)))  # noqa: E711
        .order_by(desc(last_msg.c.date))
        .limit(limit)
    )).all()
    handled = []
    if handled_limit:
        handled = (await db.execute(
            base.where(handled_current).order_by(desc(CatchUpItem.handled_at)).limit(handled_limit)
        )).all()
    return list(active), list(handled)


# --- Conversations ---

async def get_conversation_messages(
    db: AsyncSession, conversation_id: int, limit: Optional[int] = None
) -> list:
    """Return messages in a conversation, ordered oldest-first. With `limit`,
    only the newest `limit` messages (still oldest-first)."""
    q = select(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id)
    if limit is not None:
        newest = (
            q.with_only_columns(ConversationMessage.id)
            .order_by(desc(ConversationMessage.created_at), desc(ConversationMessage.id))
            .limit(limit)
        )
        q = select(ConversationMessage).where(ConversationMessage.id.in_(newest.scalar_subquery()))
    result = await db.execute(q.order_by(ConversationMessage.created_at, ConversationMessage.id))
    return list(result.scalars().all())


async def add_conversation_message(
    db: AsyncSession, conversation_id: int, role: str, content: str
) -> ConversationMessage:
    msg = ConversationMessage(conversation_id=conversation_id, role=role, content=content)
    db.add(msg)
    conv = (await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )).scalar_one_or_none()
    if conv:
        conv.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(msg)
    return msg


# --- Briefing History ---

async def create_briefing_run(db: AsyncSession, days: int = 1, status: str = "done") -> BriefingRun:
    run = BriefingRun(days=days, generated_at=datetime.utcnow(), status=status)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def add_briefing_entry(
    db: AsyncSession,
    run_id: int,
    chat_id: int,
    chat_name: str,
    category: str,
    summary: str,
    sources_json: Optional[str] = None,
) -> BriefingEntry:
    entry = BriefingEntry(
        run_id=run_id,
        chat_id=chat_id,
        chat_name=chat_name,
        category=category,
        summary=summary,
        sources_json=sources_json,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


async def finish_briefing_run(
    db: AsyncSession, run_id: int, status: str, error: Optional[str] = None
) -> None:
    await db.execute(
        update(BriefingRun)
        .where(BriefingRun.id == run_id)
        .values(status=status, error=error, finished_at=datetime.utcnow())
    )
    await db.commit()


async def count_briefing_entries(db: AsyncSession, run_id: int) -> int:
    return (await db.execute(
        select(func.count()).select_from(BriefingEntry).where(BriefingEntry.run_id == run_id)
    )).scalar() or 0


async def list_briefing_runs(db: AsyncSession, include_running: bool = True) -> list[BriefingRun]:
    q = select(BriefingRun)
    if not include_running:
        q = q.where(BriefingRun.status != "running")
    result = await db.execute(q.order_by(desc(BriefingRun.generated_at)))
    return list(result.scalars().all())


async def get_entries_for_runs(db: AsyncSession, run_ids: list[int]) -> dict[int, list[BriefingEntry]]:
    """Entries of several runs in one query, grouped by run id (ordered by id)."""
    out: dict[int, list[BriefingEntry]] = {rid: [] for rid in run_ids}
    if not run_ids:
        return out
    result = await db.execute(
        select(BriefingEntry).where(BriefingEntry.run_id.in_(run_ids)).order_by(BriefingEntry.id)
    )
    for e in result.scalars().all():
        out.setdefault(e.run_id, []).append(e)
    return out


async def get_briefing_run_entries(db: AsyncSession, run_id: int) -> list[BriefingEntry]:
    result = await db.execute(
        select(BriefingEntry)
        .where(BriefingEntry.run_id == run_id)
        .order_by(BriefingEntry.id)
    )
    return list(result.scalars().all())


async def delete_briefing_run(db: AsyncSession, run_id: int) -> bool:
    result = await db.execute(
        select(BriefingRun).where(BriefingRun.id == run_id)
    )
    run = result.scalar_one_or_none()
    if not run:
        return False
    # Delete entries first (cascade may not work on SQLite without PRAGMA foreign_keys=ON)
    await db.execute(delete(BriefingEntry).where(BriefingEntry.run_id == run_id))
    await db.delete(run)
    await db.commit()
    return True


async def get_important_contacts(db: AsyncSession) -> list:
    """Return all contacts marked as important."""
    result = await db.execute(
        select(Contact)
        .where(Contact.is_important == True)  # noqa: E712
        .order_by(Contact.first_name, Contact.last_name)
    )
    return list(result.scalars().all())


VALID_STATUSES = {"new", "active", "nurturing", "stalled", "cold", "needs_reply"}


def explain_relationship_status(
    status: Optional[str],
    is_manual: bool,
    last_interaction: Optional[datetime],
    last_outgoing: Optional[datetime],
    last_incoming: Optional[datetime],
    is_important: bool = False,
) -> dict:
    """Return a dict with human-readable explanation, recommended action, and metadata
    about why a contact has its current relationship status."""
    now = datetime.utcnow()

    def _days_ago(dt: Optional[datetime]) -> Optional[int]:
        if dt is None:
            return None
        return (now - dt).days

    days_since_interaction = _days_ago(last_interaction)
    days_since_outgoing = _days_ago(last_outgoing)
    days_since_incoming = _days_ago(last_incoming)

    source = "manual" if is_manual else "auto"

    explanation = ""
    recommended_action = ""

    if status == "new":
        if not last_interaction:
            explanation = "No message history found with this contact."
            recommended_action = "Send an introductory message to start the conversation."
        elif not last_outgoing:
            explanation = "They have messaged you, but you have never replied."
            recommended_action = "Send a first reply to establish the relationship."
        else:
            explanation = "This contact was recently added."
            recommended_action = "Reach out to start building the relationship."
    elif status == "needs_reply":
        wait_days = days_since_incoming if days_since_incoming is not None else 0
        if wait_days == 0:
            explanation = "They sent a message today that you haven't replied to yet."
        elif wait_days == 1:
            explanation = "They sent a message yesterday that you haven't replied to yet."
        else:
            explanation = f"They sent a message {wait_days} days ago that you haven't replied to yet."
        recommended_action = "Reply to their latest message to keep the conversation going."
    elif status == "active":
        explanation = f"You exchanged messages within the last week ({days_since_interaction} days ago)."
        recommended_action = "Relationship is healthy. Continue engaging naturally."
    elif status == "nurturing":
        explanation = f"Last interaction was {days_since_interaction} days ago (1-4 weeks)."
        recommended_action = "Consider reaching out soon to maintain the connection."
    elif status == "stalled":
        explanation = f"Last interaction was {days_since_interaction} days ago (1-3 months). The relationship is losing momentum."
        recommended_action = "Send a check-in message to re-engage before the relationship goes cold."
    elif status == "cold":
        explanation = f"Last interaction was {days_since_interaction} days ago (over 3 months). The relationship has gone quiet."
        recommended_action = "Reach out with a personal note or relevant update to reconnect."
    else:
        explanation = "Status has not been determined yet."
        recommended_action = "Sync messages to compute the relationship status."

    if is_manual:
        explanation = f"Manually set. {explanation}"

    # Build attention_reason for important contacts
    attention_reason = ""
    if is_important:
        if status == "needs_reply":
            attention_reason = "Important contact is waiting for your reply."
        elif status == "stalled":
            attention_reason = "Important contact relationship is losing momentum."
        elif status == "cold":
            attention_reason = "Important contact has gone quiet -- consider reconnecting."
        elif status == "nurturing":
            attention_reason = "Important contact may need a check-in soon."
        else:
            attention_reason = "Marked as an important contact."

    return {
        "status_explanation": explanation,
        "recommended_action": recommended_action,
        "status_source": source,
        "attention_reason": attention_reason,
        "days_since_interaction": days_since_interaction,
        "days_since_outgoing": days_since_outgoing,
        "days_since_incoming": days_since_incoming,
    }


async def compute_relationship_status(db: AsyncSession, contact: Contact) -> tuple:
    """Compute relationship status for a contact based on message history."""
    # Find the private chat matching this contact
    chat = await db.execute(
        select(Chat).where(Chat.telegram_id == contact.telegram_id, Chat.type == "private")
    )
    chat_obj = chat.scalar_one_or_none()
    if not chat_obj:
        return ("new", None, None, None)

    # Get last outgoing and incoming message dates
    last_out_result = await db.execute(
        select(func.max(Message.date))
        .where(Message.chat_id == contact.telegram_id, Message.is_outgoing == True)  # noqa: E712
    )
    last_outgoing = last_out_result.scalar()

    last_in_result = await db.execute(
        select(func.max(Message.date))
        .where(Message.chat_id == contact.telegram_id, Message.is_outgoing == False)  # noqa: E712
    )
    last_incoming = last_in_result.scalar()

    if not last_outgoing and not last_incoming:
        return ("new", None, None, None)

    last_interaction = max(filter(None, [last_outgoing, last_incoming]))

    # No outgoing messages at all
    if not last_outgoing:
        return ("new", last_interaction, None, last_incoming)

    # They messaged more recently than we replied — but not if we reacted
    if last_incoming and last_incoming > last_outgoing:
        # Check if we reacted to the latest incoming message
        last_incoming_msg = (await db.execute(
            select(Message)
            .where(
                Message.chat_id == contact.telegram_id,
                Message.is_outgoing == False,  # noqa: E712
            )
            .order_by(desc(Message.date))
            .limit(1)
        )).scalar_one_or_none()
        if last_incoming_msg and last_incoming_msg.my_reaction:
            # Reacted — treat as acknowledged, not needs_reply
            pass
        else:
            return ("needs_reply", last_interaction, last_outgoing, last_incoming)

    # Time-based status
    from datetime import datetime
    days_since = (datetime.utcnow() - last_interaction).days
    if days_since <= 7:
        status = "active"
    elif days_since <= 30:
        status = "nurturing"
    elif days_since <= 90:
        status = "stalled"
    else:
        status = "cold"

    return (status, last_interaction, last_outgoing, last_incoming)


async def refresh_contact_status(db: AsyncSession, contact_id: int) -> Optional[Contact]:
    """Recompute and save relationship status for a contact (skip manual overrides)."""
    result = await db.execute(select(Contact).where(Contact.id == contact_id))
    contact = result.scalar_one_or_none()
    if not contact:
        return None
    if contact.relationship_status_manual:
        return contact

    status, last_interaction, last_outgoing, last_incoming = await compute_relationship_status(db, contact)
    contact.relationship_status = status
    contact.last_interaction_at = last_interaction
    contact.last_outgoing_at = last_outgoing
    contact.last_incoming_at = last_incoming
    await db.commit()
    await db.refresh(contact)
    return contact


async def refresh_all_contact_statuses(db: AsyncSession) -> int:
    """Batch refresh relationship statuses for all non-manual contacts."""
    result = await db.execute(
        select(Contact).where(Contact.relationship_status_manual == False)  # noqa: E712
    )
    contacts = list(result.scalars().all())
    count = 0
    for contact in contacts:
        status, last_interaction, last_outgoing, last_incoming = await compute_relationship_status(db, contact)
        contact.relationship_status = status
        contact.last_interaction_at = last_interaction
        contact.last_outgoing_at = last_outgoing
        contact.last_incoming_at = last_incoming
        count += 1
    await db.commit()
    return count


async def get_messages_from_sender_since(
    db: AsyncSession,
    sender_telegram_id: int,
    since: datetime,
    limit: int = 100,
    until: Optional[datetime] = None,
) -> list:
    """
    Return the NEWEST `limit` messages sent by a specific Telegram user (matched
    via sender_id == sender_telegram_id) in [since, until], oldest-first, with
    chat info.
    Each row is a (Message, Chat, reply_sender_name, reply_text) tuple.
    reply_sender_name and reply_text are None when the message is not a reply.
    """
    window = [
        Message.sender_id == sender_telegram_id,
        Message.date >= since,
        Message.text.isnot(None),
    ]
    if until is not None:
        window.append(Message.date <= until)
    latest_ids = (
        select(Message.id)
        .where(*window)
        .order_by(desc(Message.date), desc(Message.id))
        .limit(limit)
    )
    Reply = Message.__table__.alias("reply_msg")
    result = await db.execute(
        select(
            Message,
            Chat,
            Reply.c.sender_name.label("reply_sender_name"),
            Reply.c.text.label("reply_text"),
        )
        .join(Chat, Message.chat_id == Chat.telegram_id)
        .outerjoin(
            Reply,
            (Reply.c.tg_message_id == Message.reply_to_tg_message_id)
            & (Reply.c.chat_id == Message.chat_id),
        )
        .where(Message.id.in_(latest_ids.scalar_subquery()))
        .order_by(Message.date, Message.id)
    )
    return result.all()


async def get_messages_around(
    db: AsyncSession,
    chat_id: int,
    telegram_id: int,
    before: int = 2,
    after: int = 1,
) -> dict:
    """
    Return messages before and after a given telegram_id within the same chat.
    Returns {"before": [...], "after": [...]} sorted oldest-first.
    """
    # Messages before (older): lower telegram_id
    before_result = await db.execute(
        select(Message)
        .where(
            Message.chat_id == chat_id,
            Message.tg_message_id < telegram_id,
        )
        .order_by(desc(Message.tg_message_id))
        .limit(before)
    )
    before_msgs = list(reversed(before_result.scalars().all()))

    # Messages after (newer): higher telegram_id
    after_result = await db.execute(
        select(Message)
        .where(
            Message.chat_id == chat_id,
            Message.tg_message_id > telegram_id,
        )
        .order_by(Message.tg_message_id)
        .limit(after)
    )
    after_msgs = list(after_result.scalars().all())

    return {"before": before_msgs, "after": after_msgs}


async def update_message_text(
    db: AsyncSession, chat_id: int, telegram_id: int, text: str
) -> None:
    """Update the text of a message in the local DB after an edit."""
    result = await db.execute(
        select(Message).where(
            Message.tg_message_id == telegram_id,
            Message.chat_id == chat_id,
        ).limit(1)
    )
    msg = result.scalars().first()
    if msg:
        msg.text = text
        msg.text_html = None  # clear cached HTML; will be re-rendered
        await db.commit()


async def delete_messages_by_telegram_ids(
    db: AsyncSession, chat_id: int, telegram_ids: list[int]
) -> None:
    """Delete messages from the local DB by their Telegram IDs."""
    from sqlalchemy import delete as sa_delete

    await db.execute(
        sa_delete(Message).where(
            Message.chat_id == chat_id,
            Message.tg_message_id.in_(telegram_ids),
        )
    )
    await db.commit()


# --- Organizations ---
# Positions belong to an organization via contact_positions.organization_id;
# the `organization` string mirrors the org name while attached and keeps the
# historical name once detached (organization_id NULL).

from backend.db.models import ContactPosition, Organization  # noqa: E402
from backend.db.results import affected_rows


async def find_organization_by_name(db: AsyncSession, name: str) -> Optional[Organization]:
    return (await db.execute(
        select(Organization).where(name_key(Organization.name) == name_key(name.strip()))
        .order_by(Organization.id).limit(1)
    )).scalar_one_or_none()


async def attach_detached_positions(db: AsyncSession, org: Organization) -> int:
    """Re-attach detached positions carrying `org`'s name (at most one per
    person; the rest stay detached history). Flushes, doesn't commit."""
    candidates = list((await db.execute(
        select(ContactPosition).where(
            ContactPosition.organization_id.is_(None),
            name_key(ContactPosition.organization) == name_key(org.name),
        ).order_by(ContactPosition.is_active.desc(), ContactPosition.id)
    )).scalars().all())
    if not candidates:
        return 0
    taken = set((await db.execute(
        select(ContactPosition.telegram_id).where(ContactPosition.organization_id == org.id)
    )).scalars().all())
    attached = 0
    for p in candidates:
        if p.telegram_id in taken:
            continue
        taken.add(p.telegram_id)
        p.organization_id = org.id
        p.organization = org.name
        attached += 1
    await db.flush()
    return attached


async def get_or_create_organization(db: AsyncSession, name: str) -> tuple[Organization, bool]:
    """(org, created). Matches case/whitespace-insensitively; a new org adopts
    detached positions with its name. Flushes, doesn't commit. Raises
    ValueError on an empty name."""
    name = name.strip()
    if not name:
        raise ValueError("Organization name cannot be empty")
    org = await find_organization_by_name(db, name)
    if org is not None:
        return org, False
    org = Organization(name=name)
    db.add(org)
    await db.flush()
    await attach_detached_positions(db, org)
    return org, True


def _position_rank(p: ContactPosition) -> tuple:
    """Lower sorts first = the row to keep when two positions collide."""
    return (not p.is_active, not (p.title or "").strip(), p.id)


async def merge_organizations(db: AsyncSession, source: Organization, target: Organization) -> dict:
    """Fold `source` into `target` and delete `source`. A person with positions
    at both keeps one (current over former, then titled, then oldest; a missing
    title is filled from the other). Flushes, doesn't commit."""
    if source.id == target.id:
        return {"moved_positions": 0, "merged_duplicate_positions": 0}
    target_by_person = {
        p.telegram_id: p for p in (await db.execute(
            select(ContactPosition).where(ContactPosition.organization_id == target.id)
        )).scalars().all()
    }
    source_positions = list((await db.execute(
        select(ContactPosition).where(ContactPosition.organization_id == source.id)
    )).scalars().all())
    moved = merged = 0
    for p in source_positions:
        other = target_by_person.get(p.telegram_id)
        if other is None:
            p.organization_id = target.id
            p.organization = target.name
            moved += 1
            continue
        keep, drop = (p, other) if _position_rank(p) < _position_rank(other) else (other, p)
        if not (keep.title or "").strip() and (drop.title or "").strip():
            keep.title = drop.title
        await db.delete(drop)
        await db.flush()  # free the (telegram_id, organization_id) slot first
        keep.organization_id = target.id
        keep.organization = target.name
        merged += 1
    if not (target.context or "").strip() and (source.context or "").strip():
        target.context = source.context
    await db.flush()
    await db.delete(source)
    await db.flush()
    return {"moved_positions": moved, "merged_duplicate_positions": merged}


async def rename_organization(db: AsyncSession, org: Organization, new_name: str) -> None:
    """Rename in place (caller has ruled out a clash with another org).
    Attached positions follow; detached history keeps its old name."""
    org.name = new_name
    await db.execute(
        update(ContactPosition).where(ContactPosition.organization_id == org.id)
        .values(organization=new_name)
    )
    await db.flush()


async def delete_organization(db: AsyncSession, org: Organization, mode: str = "detach") -> dict:
    """mode='detach' keeps members' positions as history (organization_id NULL,
    name kept); mode='delete' removes them. Flushes, doesn't commit."""
    if mode not in ("detach", "delete"):
        raise ValueError("mode must be 'detach' or 'delete'")
    if mode == "detach":
        res = await db.execute(
            update(ContactPosition).where(ContactPosition.organization_id == org.id)
            .values(organization_id=None)
        )
        counts = {"positions_detached": affected_rows(res), "positions_deleted": 0}
    else:
        res = await db.execute(delete(ContactPosition).where(ContactPosition.organization_id == org.id))
        counts = {"positions_detached": 0, "positions_deleted": affected_rows(res)}
    await db.delete(org)
    await db.flush()
    return counts
