from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, delete, desc, asc, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Chat, Message, Tag, ChatTag, BriefingRun, BriefingEntry, Conversation, ConversationMessage, Contact


# --- Chats ---

async def get_chats(db: AsyncSession, limit: int = 50, offset: int = 0, sort_by: str = "recent") -> list:
    # Subquery: latest message per chat using window function
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

    query = (
        select(Chat, last_msg.c.last_text, last_msg.c.last_sender, last_msg.c.last_date)
        .where(Chat.is_hidden == False, Chat.is_briefing == False, Chat.is_archived == False)  # noqa: E712
        .outerjoin(last_msg, Chat.telegram_id == last_msg.c.chat_id)
    )

    if sort_by == "unread":
        query = query.order_by(desc(Chat.is_pinned), desc(Chat.unread_count), desc(Chat.last_activity_at), Chat.name)
    elif sort_by == "alpha":
        query = query.order_by(desc(Chat.is_pinned), asc(Chat.name))
    else:  # recent
        query = query.order_by(desc(Chat.is_pinned), desc(Chat.last_activity_at), Chat.name)

    result = await db.execute(query.limit(limit).offset(offset))
    return list(result.all())


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
            Message.telegram_id == chat.pinned_message_id,
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
            (Reply.c.telegram_id == Message.reply_to_telegram_id)
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
        .where(Message.chat_id == chat_id, Message.telegram_id == anchor_telegram_id)
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
            (Reply.c.telegram_id == Message.reply_to_telegram_id)
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
            (Reply.c.telegram_id == Message.reply_to_telegram_id)
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
            Message.telegram_id == telegram_id,
            Message.chat_id == chat_id,
        ).limit(1)
    )
    return result.scalar() is not None


async def save_message(db: AsyncSession, **kwargs) -> Message:
    result = await db.execute(
        select(Message).where(
            Message.telegram_id == kwargs["telegram_id"],
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
        if "reply_to_telegram_id" in kwargs and kwargs["reply_to_telegram_id"] is not None and msg.reply_to_telegram_id is None:
            msg.reply_to_telegram_id = kwargs["reply_to_telegram_id"]
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
        # Update last_activity_at on the chat for sort order
        msg_date = kwargs.get("date")
        if msg_date and kwargs.get("chat_id"):
            chat = await get_chat_by_telegram_id(db, kwargs["chat_id"])
            if chat and (not chat.last_activity_at or msg_date > chat.last_activity_at):
                chat.last_activity_at = msg_date
        await db.commit()
        await db.refresh(msg)
        return msg
    except Exception:
        await db.rollback()
        # Lost a race — fetch the row the other writer inserted
        result = await db.execute(
            select(Message).where(
                Message.telegram_id == kwargs["telegram_id"],
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
            Message.telegram_id == telegram_msg_id,
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
            (Reply.c.telegram_id == Message.reply_to_telegram_id)
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
            Message.telegram_id == telegram_msg_id,
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
            (Reply.c.telegram_id == Message.reply_to_telegram_id)
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
    # Remove all chat associations first
    await db.execute(select(ChatTag).where(ChatTag.tag_id == tag_id))
    assocs = (await db.execute(select(ChatTag).where(ChatTag.tag_id == tag_id))).scalars().all()
    for assoc in assocs:
        await db.delete(assoc)
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

async def search_messages_by_keyword(db: AsyncSession, keyword: str, limit: int = 30) -> list:
    """Full-text search across all message text (case-insensitive LIKE)."""
    result = await db.execute(
        select(Message, Chat)
        .join(Chat, Message.chat_id == Chat.telegram_id)
        .where(Message.text.ilike(f"%{keyword}%"))
        .order_by(desc(Message.date))
        .limit(limit)
    )
    return result.all()


async def get_chat_by_name(db: AsyncSession, name: str) -> Optional[Chat]:
    """Find a chat whose name contains the given string (case-insensitive)."""
    result = await db.execute(
        select(Chat).where(Chat.name.ilike(f"%{name}%")).limit(1)
    )
    return result.scalar_one_or_none()


async def search_chats_by_title(db: AsyncSession, keyword: str, limit: int = 20) -> list:
    """Return all chats whose name contains the keyword (case-insensitive)."""
    result = await db.execute(
        select(Chat)
        .where(Chat.name.ilike(f"%{keyword}%"))
        .order_by(Chat.name)
        .limit(limit)
    )
    return list(result.scalars().all())


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
        .where(Tag.name.ilike(f"%{tag_name}%"))
        .order_by(Chat.name)
    )
    return list(result.scalars().all())


# --- Mentions ---

async def get_mentions(
    db: AsyncSession,
    username: str,
    limit: int = 50,
    days: int = 7,
    group_name_filter: str = "",
    tag_name_filter: str = "",
) -> list:
    """
    Return messages where the current user's @username appears in the text,
    limited to non-outgoing messages in group/channel chats.
    Each row is a (Message, Chat) tuple.
    """
    since = datetime.utcnow() - timedelta(days=days)

    q = (
        select(Message, Chat)
        .join(Chat, Message.chat_id == Chat.telegram_id)
        .where(
            Message.is_outgoing == False,  # noqa: E712
            Chat.type.in_(["group", "channel", "supergroup"]),
            Message.text.ilike(f"%@{username}%"),
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
            Message.telegram_id > after_telegram_id,
            Message.is_outgoing == True,  # noqa: E712
        )
        .order_by(Message.telegram_id)
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
            Message.telegram_id > from_telegram_id,
            Message.telegram_id < to_telegram_id,
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
            (Reply.c.telegram_id == Message.reply_to_telegram_id)
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
            Message.telegram_id < telegram_id,
        )
        .order_by(desc(Message.telegram_id))
        .limit(before)
    )
    before_msgs = list(reversed(before_result.scalars().all()))

    # Messages after (newer): higher telegram_id
    after_result = await db.execute(
        select(Message)
        .where(
            Message.chat_id == chat_id,
            Message.telegram_id > telegram_id,
        )
        .order_by(Message.telegram_id)
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
            Message.telegram_id == telegram_id,
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
            Message.telegram_id.in_(telegram_ids),
        )
    )
    await db.commit()
