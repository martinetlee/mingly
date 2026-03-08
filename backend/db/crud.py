from datetime import datetime
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Chat, Message


# --- Chats ---

async def get_chats(db: AsyncSession) -> list[Chat]:
    result = await db.execute(select(Chat).order_by(Chat.name))
    return list(result.scalars().all())


async def get_chat_by_telegram_id(db: AsyncSession, telegram_id: int) -> Chat | None:
    result = await db.execute(
        select(Chat).where(Chat.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def upsert_chat(
    db: AsyncSession,
    telegram_id: int,
    name: str,
    chat_type: str,
    username: str | None = None,
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


async def set_monitored(db: AsyncSession, telegram_id: int, monitored: bool) -> Chat | None:
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
) -> list[Message]:
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
        )
    )
    return result.scalar_one_or_none() is not None


async def save_message(db: AsyncSession, **kwargs) -> Message:
    if await message_exists(db, kwargs["telegram_id"], kwargs["chat_id"]):
        result = await db.execute(
            select(Message).where(
                Message.telegram_id == kwargs["telegram_id"],
                Message.chat_id == kwargs["chat_id"],
            )
        )
        return result.scalar_one()

    msg = Message(**kwargs)
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


async def update_chat_sync(db: AsyncSession, telegram_id: int) -> None:
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if chat:
        chat.last_synced_at = datetime.utcnow()
        await db.commit()
