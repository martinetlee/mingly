import json
from html import escape as html_escape
from pathlib import Path
from typing import Optional
from telethon import events
from telethon.tl.types import User, Channel

from backend.config import DATA_DIR
from backend.db.database import AsyncSessionLocal
from backend.db import crud
from backend.telegram.client import get_client
from backend.ws.manager import ws_manager

MEDIA_DIR = DATA_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)


def _chat_type(entity) -> str:
    if isinstance(entity, User):
        return "private"
    if isinstance(entity, Channel) and entity.megagroup:
        return "group"
    if isinstance(entity, Channel):
        return "channel"
    return "group"


def _sender_name(sender) -> Optional[str]:
    if sender is None:
        return None
    if isinstance(sender, User):
        return f"{sender.first_name or ''} {sender.last_name or ''}".strip() or None
    return getattr(sender, "title", None) or str(sender.id)


def _media_type(msg) -> Optional[str]:
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


def _text_nodes(html: str, fn) -> str:
    """Apply fn to every text node (content between HTML tags)."""
    import re
    parts = re.split(r'(<[^>]+>)', html)
    return ''.join(fn(p) if not p.startswith('<') else p for p in parts)


def _markdown_pass(html: str) -> str:
    """Three-pass markdown → HTML conversion, safe to run on existing HTML strings."""
    import re

    # Pass 1 – links.
    # [[display]](url) → [display] as link text (matching official Telegram display).
    # \s* allows the bot to put the url on the next line: [[link]]\n(url)
    def _links(text: str) -> str:
        text = re.sub(
            r'\[\[([^\]]+)\]\]\s*\(([^)]+)\)',
            lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">[{m.group(1)}]</a>',
            text,
        )
        text = re.sub(
            r'\[([^\]]+)\]\s*\(([^)]+)\)',
            lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>',
            text,
        )
        return text

    html = _text_nodes(html, _links)

    # Pass 2 – inline styles (re-split since pass 1 may have introduced <a> tags)
    def _inline(text: str) -> str:
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
        text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text, flags=re.DOTALL)
        text = re.sub(r'`(.+?)`', r'<code>\1</code>', text, flags=re.DOTALL)
        return text

    html = _text_nodes(html, _inline)

    # Pass 3 – newlines → <br> so line structure is preserved when rendered as HTML
    return _text_nodes(html, lambda t: t.replace('\n', '<br>'))


def _entities_to_html(text: str, entities) -> Optional[str]:
    """Convert Telethon message entities to HTML."""
    if not text:
        return None

    import re
    from html import escape

    # If the raw text contains markdown patterns (**bold**, [[link]]), the bot sent
    # plain text without parse_mode. Calling thtml.unparse() here produces garbled
    # HTML because Telegram's auto-detected URL entities are applied at byte offsets
    # that interleave with the markdown markers. Skip thtml for these messages.
    has_raw_markdown = bool(re.search(r'\*\*|\[\[', text))

    if entities and not has_raw_markdown:
        from telethon.extensions import html as thtml
        try:
            result = thtml.unparse(text, entities)
            return _markdown_pass(result)
        except Exception:
            pass

    # No entities, thtml failed, or text has raw markdown → regex-only path
    escaped = escape(text)
    result = _markdown_pass(escaped)
    # Only store text_html when it differs (i.e. formatting or line breaks were found)
    return result if result != escaped else None


def _extract_reactions(msg) -> Optional[str]:
    """Extract reactions from a Telethon message as JSON string."""
    reactions = getattr(msg, 'reactions', None)
    if not reactions:
        return None
    results = getattr(reactions, 'results', None)
    if not results:
        return None
    out = []
    for r in results:
        reaction = r.reaction
        emoji = getattr(reaction, 'emoticon', None) or getattr(reaction, 'document_id', None)
        if emoji is None:
            continue
        out.append({"emoji": str(emoji), "count": r.count})
    return json.dumps(out) if out else None


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


async def bulk_sync(n_chats: int = 50, messages_per_chat: int = 30) -> int:
    """Sync messages for the n most recent dialogs. Returns count of chats synced."""
    client = get_client()
    if not client:
        return 0

    synced = 0
    async with AsyncSessionLocal() as db:
        async for dialog in client.iter_dialogs(limit=n_chats):
            entity = dialog.entity
            chat = await crud.upsert_chat(
                db,
                telegram_id=dialog.id,
                name=dialog.name or str(dialog.id),
                chat_type=_chat_type(entity),
                username=getattr(entity, "username", None),
            )
            if chat.is_hidden or chat.is_briefing:
                continue
            await sync_messages(dialog.id, limit=messages_per_chat)
            synced += 1

    return synced


def _pick_thumb(msg):
    """Pick the smallest thumbnail that's at least 600px wide (Telegram 'x' size)."""
    from telethon.tl.types import PhotoSize
    photo = getattr(msg, 'photo', None)
    if not photo or not hasattr(photo, 'sizes'):
        return None
    sizes = [s for s in photo.sizes if isinstance(s, PhotoSize) and hasattr(s, 'w')]
    if not sizes:
        return None
    sizes.sort(key=lambda s: s.w)
    for s in sizes:
        if s.w >= 600:
            return s
    return sizes[-1]  # fallback: largest available


async def _download_photo(client, msg) -> Optional[str]:
    """Download a size-limited photo to disk, return relative path or None."""
    if _media_type(msg) != "photo":
        return None
    try:
        chat_dir = MEDIA_DIR / str(msg.chat_id)
        chat_dir.mkdir(exist_ok=True)
        path = chat_dir / f"{msg.id}.jpg"
        if not path.exists():
            thumb = _pick_thumb(msg)
            await client.download_media(msg, file=str(path), thumb=thumb)
        return str(path.relative_to(DATA_DIR))
    except Exception:
        return None


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
            media_path = await _download_photo(client, msg)
            text_html = _entities_to_html(msg.text or "", msg.entities)
            reactions_json = _extract_reactions(msg)
            await crud.save_message(
                db,
                telegram_id=msg.id,
                chat_id=chat_telegram_id,
                sender_id=msg.sender_id,
                sender_name=_sender_name(sender),
                text=msg.text or "",
                text_html=text_html,
                reactions_json=reactions_json,
                date=msg.date.replace(tzinfo=None),
                is_outgoing=msg.out,
                media_type=_media_type(msg),
                media_path=media_path,
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
            media_path = await _download_photo(client, msg)
            text_html = _entities_to_html(msg.text or "", msg.entities)
            reactions_json = _extract_reactions(msg)
            saved = await crud.save_message(
                db,
                telegram_id=msg.id,
                chat_id=event.chat_id,
                sender_id=msg.sender_id,
                sender_name=_sender_name(sender),
                text=msg.text or "",
                text_html=text_html,
                reactions_json=reactions_json,
                date=msg.date.replace(tzinfo=None),
                is_outgoing=msg.out,
                media_type=_media_type(msg),
                media_path=media_path,
            )

        await ws_manager.broadcast({
            "type": "new_message",
            "chat_id": event.chat_id,
            "message_id": saved.id,
        })
