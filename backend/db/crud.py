from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, desc, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Chat, Message, Tag, ChatTag


# --- Chats ---

async def get_chats(db: AsyncSession, limit: int = 50, offset: int = 0) -> list:
    last_msg = (
        select(Message.chat_id, func.max(Message.date).label("last_date"))
        .group_by(Message.chat_id)
        .subquery()
    )
    result = await db.execute(
        select(Chat)
        .where(Chat.is_hidden == False, Chat.is_briefing == False)  # noqa: E712
        .outerjoin(last_msg, Chat.telegram_id == last_msg.c.chat_id)
        .order_by(desc(last_msg.c.last_date), Chat.name)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def get_tracked_chats(db: AsyncSession) -> list:
    last_msg = (
        select(Message.chat_id, func.max(Message.date).label("last_date"))
        .group_by(Message.chat_id)
        .subquery()
    )
    result = await db.execute(
        select(Chat)
        .where(Chat.is_tracked == True)  # noqa: E712
        .outerjoin(last_msg, Chat.telegram_id == last_msg.c.chat_id)
        .order_by(desc(last_msg.c.last_date), Chat.name)
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
) -> Chat:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if chat:
        chat.name = name
        chat.type = chat_type
        chat.username = username
        if unread_count is not None:
            chat.unread_count = unread_count
    else:
        chat = Chat(
            telegram_id=telegram_id,
            name=name,
            type=chat_type,
            username=username,
            unread_count=unread_count if unread_count is not None else 0,
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
    result = await db.execute(
        select(Message)
        .where(Message.chat_id == chat_id)
        .order_by(desc(Message.date))
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


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
        if updated:
            await db.commit()
            await db.refresh(msg)
        return msg

    try:
        msg = Message(**kwargs)
        db.add(msg)
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
