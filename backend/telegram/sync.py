from telethon import events
from telethon.tl.types import User, Channel

from backend.db.database import AsyncSessionLocal
from backend.db import crud
from backend.telegram.client import get_client
from backend.ws.manager import ws_manager


def _chat_type(entity) -> str:
    if isinstance(entity, User):
        return "private"
    if isinstance(entity, Channel) and entity.megagroup:
        return "group"
    if isinstance(entity, Channel):
        return "channel"
    return "group"


def _sender_name(sender) -> str | None:
    if sender is None:
        return None
    if isinstance(sender, User):
        return f"{sender.first_name or ''} {sender.last_name or ''}".strip() or None
    return getattr(sender, "title", None) or str(sender.id)


def _media_type(msg) -> str | None:
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "sticker", None):
        return "sticker"
    if getattr(msg, "document", None):
        return "document"
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "audio", None):
        return "audio"
    return None


async def sync_dialogs() -> None:
    """Fetch all dialogs and upsert into the chats table."""
    client = get_client()
    if not client:
        return

    async with AsyncSessionLocal() as db:
        async for dialog in client.iter_dialogs(limit=200):
            entity = dialog.entity
            await crud.upsert_chat(
                db,
                telegram_id=dialog.id,
                name=dialog.name or str(dialog.id),
                chat_type=_chat_type(entity),
                username=getattr(entity, "username", None),
            )


async def sync_messages(chat_telegram_id: int, limit: int = 50) -> None:
    """Fetch recent messages for a chat and store in DB."""
    client = get_client()
    if not client:
        return

    async with AsyncSessionLocal() as db:
        async for msg in client.iter_messages(chat_telegram_id, limit=limit):
            if not msg.text and not msg.media:
                continue

            sender = await msg.get_sender() if msg.sender_id else None
            await crud.save_message(
                db,
                telegram_id=msg.id,
                chat_id=chat_telegram_id,
                sender_id=msg.sender_id,
                sender_name=_sender_name(sender),
                text=msg.text or "",
                date=msg.date.replace(tzinfo=None),
                is_outgoing=msg.out,
                media_type=_media_type(msg),
            )

        await crud.update_chat_sync(db, chat_telegram_id)


async def register_event_handlers() -> None:
    """Register Telethon event handlers. Must be called after auth."""
    client = get_client()
    if not client:
        return

    @client.on(events.NewMessage)
    async def on_new_message(event):
        msg = event.message
        async with AsyncSessionLocal() as db:
            # Only persist if the chat is known
            chat = await crud.get_chat_by_telegram_id(db, event.chat_id)
            if not chat:
                return

            sender = await msg.get_sender() if msg.sender_id else None
            saved = await crud.save_message(
                db,
                telegram_id=msg.id,
                chat_id=event.chat_id,
                sender_id=msg.sender_id,
                sender_name=_sender_name(sender),
                text=msg.text or "",
                date=msg.date.replace(tzinfo=None),
                is_outgoing=msg.out,
                media_type=_media_type(msg),
            )

        await ws_manager.broadcast({
            "type": "new_message",
            "chat_id": event.chat_id,
            "message_id": saved.id,
        })
