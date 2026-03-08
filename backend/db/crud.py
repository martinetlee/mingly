from datetime import datetime
from typing import Optional
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Chat, Message


# --- Chats ---

async def get_chats(db: AsyncSession) -> list:
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
) -> Chat:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if chat:
        chat.name = name
        chat.type = chat_type
        chat.username = username
    else:
        chat = Chat(
            telegram_id=telegram_id,
            name=name,
            type=chat_type,
            username=username,
        )
        db.add(chat)
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
