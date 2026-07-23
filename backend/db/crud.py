from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, delete, desc, asc, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import (
    Chat, Message, Tag, ChatTag, BriefingRun, BriefingEntry, Conversation, ConversationMessage, Contact,
    Workspace, WorkspaceTag, WorkspaceColumn, WorkspaceChatOverride,
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
) -> Chat:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if chat:
        chat.name = name
        chat.type = chat_type
        chat.username = username
        if unread_count is not None:
            chat.unread_count = unread_count
        if members_count is not None:
            chat.members_count = members_count
        if is_forum is not None:
            chat.is_forum = is_forum
        if slow_mode_seconds is not None:
            chat.slow_mode_seconds = slow_mode_seconds
        if last_message_date:
            chat.last_activity_at = last_message_date
        if is_deactivated is not None:
            chat.is_deactivated = is_deactivated
    else:
        chat = Chat(
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
        )
        db.add(chat)
    await db.commit()
    await db.refresh(chat)
    return chat


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
        .order_by(desc(Message.date))
        .limit(limit)
        .offset(offset)
    )
    return list(result.all())


async def get_offset_for_anchor(
    db: AsyncSession,
    chat_id: int,
    anchor_telegram_id: int,
    half_window: int = 50,
) -> int:
    """
    Return the offset to pass to get_messages() so the anchor message
    lands roughly in the centre of the result window.
    Messages are ordered newest-first, so offset = (count of messages newer
    than anchor) - half_window, floored at 0.
    """
    # Get date of anchor message
    anchor_date_result = await db.execute(
        select(Message.date)
        .where(Message.chat_id == chat_id, Message.tg_message_id == anchor_telegram_id)
    )
    anchor_date = anchor_date_result.scalar_one_or_none()
    if anchor_date is None:
        return 0
    # Count messages strictly newer than the anchor
    count_result = await db.execute(
        select(func.count())
        .select_from(Message)
        .where(Message.chat_id == chat_id, Message.date > anchor_date)
    )
    newer_count = count_result.scalar() or 0
    return max(0, newer_count - half_window)


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


async def get_date_offset(
    db: AsyncSession,
    chat_id: int,
    target_date: datetime,
    half_window: int = 50,
) -> int:
    """Return offset so that messages around target_date appear in view."""
    count_result = await db.execute(
        select(func.count())
        .select_from(Message)
        .where(Message.chat_id == chat_id, Message.date > target_date)
    )
    newer_count = count_result.scalar() or 0
    return max(0, newer_count - half_window)


async def get_messages_since(
    db: AsyncSession,
    chat_id: int,
    since: datetime,
    limit: int = 200,
) -> list:
    """
    Return up to `limit` messages for a chat on or after `since`, oldest-first.
    Each element is a (Message, reply_sender_name, reply_text) tuple.
    reply_sender_name and reply_text are None when the message is not a reply.
    """
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
        .where(Message.chat_id == chat_id, Message.date >= since)
        .order_by(Message.date)
        .limit(limit)
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


async def save_message(db: AsyncSession, **kwargs) -> Message:
    result = await db.execute(
        select(Message).where(
            Message.tg_message_id == kwargs["tg_message_id"],
            Message.chat_id == kwargs["chat_id"],
        ).limit(1)
    )
    msg = result.scalars().first()
    if msg:
        updated = False
        if kwargs.get("media_path") and not msg.media_path:
            msg.media_path = kwargs["media_path"]
            updated = True
        if kwargs.get("text_html") and kwargs["text_html"] != msg.text_html:
            msg.text_html = kwargs["text_html"]
            updated = True
        if kwargs.get("reactions_json"):
            msg.reactions_json = kwargs["reactions_json"]
            updated = True
        if "reply_to_tg_message_id" in kwargs and kwargs["reply_to_tg_message_id"] is not None and msg.reply_to_tg_message_id is None:
            msg.reply_to_tg_message_id = kwargs["reply_to_tg_message_id"]
            updated = True
        if kwargs.get("media_filename") and not msg.media_filename:
            msg.media_filename = kwargs["media_filename"]
            updated = True
        if kwargs.get("media_size") and not msg.media_size:
            msg.media_size = kwargs["media_size"]
            updated = True
        if kwargs.get("grouped_id") and not msg.grouped_id:
            msg.grouped_id = kwargs["grouped_id"]
            updated = True
        if "delivery_state" in kwargs and kwargs["delivery_state"]:
            msg.delivery_state = kwargs["delivery_state"]
            updated = True
        if "is_edited" in kwargs and kwargs["is_edited"] and not msg.is_edited:
            msg.is_edited = True
            updated = True
        if updated:
            await db.commit()
            await db.refresh(msg)
        return msg

    try:
        msg = Message(**kwargs)
        db.add(msg)
        # Update cached last message fields on the chat
        msg_date = kwargs.get("date")
        if msg_date and kwargs.get("chat_id"):
            chat = await get_chat_by_telegram_id(db, kwargs["chat_id"])
            if chat and (not chat.last_activity_at or msg_date > chat.last_activity_at):
                chat.last_activity_at = msg_date
                chat.last_message_text = kwargs.get("text")
                chat.last_message_sender = kwargs.get("sender_name")
                chat.last_message_date = msg_date
        await db.commit()
        await db.refresh(msg)
        return msg
    except Exception:
        await db.rollback()
        # Lost a race — fetch the row the other writer inserted
        result = await db.execute(
            select(Message).where(
                Message.tg_message_id == kwargs["tg_message_id"],
                Message.chat_id == kwargs["chat_id"],
            ).limit(1)
        )
        return result.scalars().first()


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

async def get_topic_messages(
    db: AsyncSession,
    chat_telegram_id: int,
    topic_id: int,
    limit: int = 100,
    offset: int = 0,
) -> list:
    """Get messages for a specific forum topic, with reply previews."""
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
            Message.topic_id == topic_id,
        )
        .order_by(Message.date.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.all()


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


# --- Tags ---

async def get_all_tags(db: AsyncSession) -> list[Tag]:
    result = await db.execute(select(Tag).order_by(Tag.is_default.desc(), Tag.name))
    return list(result.scalars().all())


async def get_all_tags_with_counts(db: AsyncSession) -> list[tuple[Tag, int]]:
    """All tags plus the number of chats each would show in the filtered chat list.

    Counts use the same exclusions as get_chats (hidden/briefing/archived).
    """
    result = await db.execute(
        select(Tag, func.count(Chat.telegram_id))
        .outerjoin(ChatTag, ChatTag.tag_id == Tag.id)
        .outerjoin(Chat, (Chat.telegram_id == ChatTag.chat_id)
                   & (Chat.is_hidden == False) & (Chat.is_briefing == False) & (Chat.is_archived == False)  # noqa: E712
                   & (Chat.is_deactivated == False))  # noqa: E712
        .group_by(Tag.id)
        .order_by(Tag.is_default.desc(), Tag.name)
    )
    return [(tag, count) for tag, count in result.all()]


async def create_tag(db: AsyncSession, name: str, color: str = "slate", category: Optional[str] = None) -> Tag:
    tag = Tag(name=name, color=color, category=category, is_default=False)
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return tag


async def update_tag(
    db: AsyncSession,
    tag_id: int,
    name: Optional[str] = None,
    color: Optional[str] = None,
    category: Optional[str] = None,
) -> Optional[Tag]:
    result = await db.execute(select(Tag).where(Tag.id == tag_id))
    tag = result.scalar_one_or_none()
    if not tag:
        return None
    if name is not None:
        tag.name = name.strip()
    if color is not None:
        tag.color = color
    if category is not None:
        tag.category = category.strip() or None
    await db.commit()
    await db.refresh(tag)
    return tag


async def delete_tag(db: AsyncSession, tag_id: int) -> bool:
    result = await db.execute(select(Tag).where(Tag.id == tag_id))
    tag = result.scalar_one_or_none()
    if not tag:
        return False
    # Remove all associations first (chats, workspace rules, board columns)
    await db.execute(delete(ChatTag).where(ChatTag.tag_id == tag_id))
    await db.execute(delete(WorkspaceTag).where(WorkspaceTag.tag_id == tag_id))
    await db.execute(delete(WorkspaceColumn).where(WorkspaceColumn.tag_id == tag_id))
    await db.delete(tag)
    await db.commit()
    return True


async def get_tags_for_chat(db: AsyncSession, chat_telegram_id: int) -> list[Tag]:
    result = await db.execute(
        select(Tag)
        .join(ChatTag, Tag.id == ChatTag.tag_id)
        .where(ChatTag.chat_id == chat_telegram_id)
        .order_by(Tag.name)
    )
    return list(result.scalars().all())


async def add_tag_to_chat(db: AsyncSession, chat_telegram_id: int, tag_id: int) -> Optional[ChatTag]:
    # Check tag exists
    tag = (await db.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if not tag:
        return None
    # Upsert
    existing = (await db.execute(
        select(ChatTag).where(ChatTag.chat_id == chat_telegram_id, ChatTag.tag_id == tag_id)
    )).scalar_one_or_none()
    if existing:
        return existing
    ct = ChatTag(chat_id=chat_telegram_id, tag_id=tag_id)
    db.add(ct)
    await db.commit()
    await db.refresh(ct)
    return ct


async def add_tag_to_chats(db: AsyncSession, chat_telegram_ids: list[int], tag_id: int) -> int:
    """Bulk-assign a tag to many chats. Returns the number of chats newly tagged."""
    if not chat_telegram_ids:
        return 0
    already = set((await db.execute(
        select(ChatTag.chat_id).where(ChatTag.chat_id.in_(chat_telegram_ids), ChatTag.tag_id == tag_id)
    )).scalars().all())
    missing = [cid for cid in chat_telegram_ids if cid not in already]
    db.add_all(ChatTag(chat_id=cid, tag_id=tag_id) for cid in missing)
    await db.commit()
    return len(missing)


async def remove_tag_from_chat(db: AsyncSession, chat_telegram_id: int, tag_id: int) -> bool:
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


async def get_workspaces_with_counts(db: AsyncSession) -> list[dict]:
    """All workspaces ordered by position, each with tag_ids, member and unallocated counts."""
    workspaces = list((await db.execute(
        select(Workspace).order_by(Workspace.position, Workspace.id)
    )).scalars().all())
    result = []
    for ws in workspaces:
        tag_ids = list((await db.execute(
            select(WorkspaceTag.tag_id).where(
                WorkspaceTag.workspace_id == ws.id,
                WorkspaceTag.is_exclusion == False,  # noqa: E712
            )
        )).scalars().all())
        member_count = (await db.execute(
            select(func.count()).select_from(Chat).where(
                Chat.telegram_id.in_(_workspace_member_ids(ws.id)), _live_chat_filter()
            )
        )).scalar() or 0
        unallocated_count = 0
        if ws.view_mode == "kanban":
            unallocated_count = (await db.execute(
                select(func.count()).select_from(Chat)
                .join(WorkspaceChatOverride, (WorkspaceChatOverride.chat_id == Chat.telegram_id)
                      & (WorkspaceChatOverride.workspace_id == ws.id))
                .where(
                    Chat.telegram_id.in_(_workspace_member_ids(ws.id)),
                    _live_chat_filter(),
                    WorkspaceChatOverride.needs_triage_since.isnot(None),
                    Chat.telegram_id.notin_(
                        select(ChatTag.chat_id).where(ChatTag.tag_id.in_(_workspace_stage_tag_ids(ws.id)))
                    ),
                )
            )).scalar() or 0
        result.append({"workspace": ws, "tag_ids": tag_ids,
                       "member_count": member_count, "unallocated_count": unallocated_count})
    return result


async def create_workspace(
    db: AsyncSession, name: str, emoji: Optional[str], color: str, tag_ids: list[int], view_mode: str = "list"
) -> Workspace:
    max_pos = (await db.execute(select(func.max(Workspace.position)))).scalar()
    ws = Workspace(
        name=name, emoji=emoji, color=color, view_mode=view_mode,
        position=(max_pos or 0) + 1,
        board_watermark=datetime.utcnow() if view_mode == "kanban" else None,
    )
    db.add(ws)
    await db.flush()
    for tid in tag_ids:
        db.add(WorkspaceTag(workspace_id=ws.id, tag_id=tid))
    await db.commit()
    await db.refresh(ws)
    return ws


async def update_workspace(
    db: AsyncSession, workspace_id: int,
    name: Optional[str] = None, emoji: Optional[str] = None, color: Optional[str] = None,
    tag_ids: Optional[list[int]] = None, view_mode: Optional[str] = None,
) -> Optional[Workspace]:
    ws = (await db.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one_or_none()
    if not ws:
        return None
    if name is not None:
        ws.name = name
    if emoji is not None:
        ws.emoji = emoji
    if color is not None:
        ws.color = color
    if view_mode is not None:
        if view_mode == "kanban" and ws.view_mode != "kanban" and ws.board_watermark is None:
            ws.board_watermark = datetime.utcnow()  # board starts clean; only future activity triages
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
    await db.execute(delete(WorkspaceTag).where(WorkspaceTag.workspace_id == workspace_id))
    await db.execute(delete(WorkspaceColumn).where(WorkspaceColumn.workspace_id == workspace_id))
    await db.execute(delete(WorkspaceChatOverride).where(WorkspaceChatOverride.workspace_id == workspace_id))
    await db.delete(ws)
    await db.commit()
    return True


async def get_board(db: AsyncSession, workspace_id: int) -> Optional[dict]:
    """Assemble the full board: stage columns, unallocated (triage), and activity-grouped backlog."""
    ws = (await db.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one_or_none()
    if not ws:
        return None
    columns = list((await db.execute(
        select(WorkspaceColumn, Tag)
        .join(Tag, Tag.id == WorkspaceColumn.tag_id)
        .where(WorkspaceColumn.workspace_id == workspace_id)
        .order_by(WorkspaceColumn.position, WorkspaceColumn.id)
    )).all())
    stage_tag_ids = [col.tag_id for col, _tag in columns]

    members = list((await db.execute(
        select(Chat).where(Chat.telegram_id.in_(_workspace_member_ids(workspace_id)), _live_chat_filter())
    )).scalars().all())
    member_ids = [c.telegram_id for c in members]

    # chat -> stage tags (within this board) in one query
    staged: dict[int, int] = {}
    if stage_tag_ids and member_ids:
        rows = (await db.execute(
            select(ChatTag.chat_id, ChatTag.tag_id).where(
                ChatTag.tag_id.in_(stage_tag_ids), ChatTag.chat_id.in_(member_ids)
            )
        )).all()
        for chat_id, tag_id in rows:
            staged.setdefault(chat_id, tag_id)  # leftmost-wins handled by column order at render

    # unallocated flags
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

    by_column: dict[int, list[Chat]] = {tid: [] for tid in stage_tag_ids}
    unallocated: list[Chat] = []
    backlog: list[Chat] = []
    for c in members:
        if c.telegram_id in staged:
            by_column[staged[c.telegram_id]].append(c)
        elif c.telegram_id in triage:
            unallocated.append(c)
        else:
            backlog.append(c)

    def _recent_first(chats: list[Chat]) -> list[Chat]:
        return sorted(chats, key=lambda c: c.last_activity_at or datetime.min, reverse=True)

    now = datetime.utcnow()
    active, cooling, dormant = [], [], []
    for c in _recent_first(backlog):
        age = (now - c.last_activity_at).days if c.last_activity_at else None
        if age is not None and age < 7:
            active.append(c)
        elif age is not None and age < 30:
            cooling.append(c)
        else:
            dormant.append(c)

    return {
        "workspace": ws,
        "columns": [
            {"tag_id": col.tag_id, "name": tag.name, "color": col.color or tag.color,
             "position": col.position, "chats": _recent_first(by_column[col.tag_id])}
            for col, tag in columns
        ],
        "unallocated": _recent_first(unallocated),
        "backlog": {"active": active, "cooling": cooling, "dormant": dormant},
    }


async def add_board_column(db: AsyncSession, workspace_id: int, name: str, color: Optional[str]) -> Optional[dict]:
    """Attach a column backed by a stage tag (reused case-insensitively or created)."""
    tag = (await db.execute(select(Tag).where(func.lower(Tag.name) == name.lower()))).scalar_one_or_none()
    if not tag:
        tag = Tag(name=name, color=color or "slate", category="stage", is_default=False)
        db.add(tag)
        await db.flush()
    existing = (await db.execute(select(WorkspaceColumn).where(
        WorkspaceColumn.workspace_id == workspace_id, WorkspaceColumn.tag_id == tag.id
    ))).scalar_one_or_none()
    if existing:
        await db.commit()
        return None  # already a column on this board
    max_pos = (await db.execute(
        select(func.max(WorkspaceColumn.position)).where(WorkspaceColumn.workspace_id == workspace_id)
    )).scalar()
    col = WorkspaceColumn(workspace_id=workspace_id, tag_id=tag.id, position=(max_pos or 0) + 1, color=color)
    db.add(col)
    await db.commit()
    return {"tag_id": tag.id, "name": tag.name, "color": col.color or tag.color, "position": col.position}


async def update_board_column(
    db: AsyncSession, workspace_id: int, tag_id: int,
    name: Optional[str] = None, color: Optional[str] = None, position: Optional[int] = None,
) -> str:
    """Returns 'ok', 'not_found', or 'shared' (rename refused: tag backs another board's column)."""
    col = (await db.execute(select(WorkspaceColumn).where(
        WorkspaceColumn.workspace_id == workspace_id, WorkspaceColumn.tag_id == tag_id
    ))).scalar_one_or_none()
    if not col:
        return "not_found"
    if name is not None:
        other = (await db.execute(select(func.count()).select_from(WorkspaceColumn).where(
            WorkspaceColumn.tag_id == tag_id, WorkspaceColumn.workspace_id != workspace_id
        ))).scalar() or 0
        if other:
            return "shared"
        tag = (await db.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
        if tag:
            tag.name = name
    if color is not None:
        col.color = color
    if position is not None:
        col.position = position
    await db.commit()
    return "ok"


async def delete_board_column(db: AsyncSession, workspace_id: int, tag_id: int, remove_tags: bool = True) -> bool:
    col = (await db.execute(select(WorkspaceColumn).where(
        WorkspaceColumn.workspace_id == workspace_id, WorkspaceColumn.tag_id == tag_id
    ))).scalar_one_or_none()
    if not col:
        return False
    if remove_tags:
        # Return this board's cards to backlog (only member chats; the tag may be shared)
        await db.execute(delete(ChatTag).where(
            ChatTag.tag_id == tag_id,
            ChatTag.chat_id.in_(_workspace_member_ids(workspace_id)),
        ))
    await db.delete(col)
    await db.commit()
    return True


async def move_board_chat(db: AsyncSession, workspace_id: int, chat_id: int, to_tag_id: Optional[int]) -> bool:
    """Move a chat between board columns (None = back to backlog / dismiss triage)."""
    stage_ids = list((await db.execute(_workspace_stage_tag_ids(workspace_id))).scalars().all())
    if to_tag_id is not None and to_tag_id not in stage_ids:
        return False
    if stage_ids:
        await db.execute(delete(ChatTag).where(ChatTag.chat_id == chat_id, ChatTag.tag_id.in_(stage_ids)))
    if to_tag_id is not None:
        db.add(ChatTag(chat_id=chat_id, tag_id=to_tag_id))
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


# --- Conversations ---

async def get_conversation_messages(db: AsyncSession, conversation_id: int) -> list:
    """Return all messages in a conversation, ordered oldest-first."""
    result = await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(ConversationMessage.created_at)
    )
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

async def create_briefing_run(db: AsyncSession, days: int = 1) -> BriefingRun:
    run = BriefingRun(days=days, generated_at=datetime.utcnow())
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


async def list_briefing_runs(db: AsyncSession) -> list[BriefingRun]:
    result = await db.execute(
        select(BriefingRun).order_by(desc(BriefingRun.generated_at))
    )
    return list(result.scalars().all())


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
) -> list:
    """
    Return up to `limit` messages sent by a specific Telegram user (matched via
    sender_id == sender_telegram_id) on or after `since`, with chat info.
    Each row is a (Message, Chat, reply_sender_name, reply_text) tuple.
    reply_sender_name and reply_text are None when the message is not a reply.
    """
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
        .where(
            Message.sender_id == sender_telegram_id,
            Message.date >= since,
            Message.text.isnot(None),
        )
        .order_by(Message.date)
        .limit(limit)
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
