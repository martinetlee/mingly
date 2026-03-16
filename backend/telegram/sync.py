import json
import logging
import subprocess
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from typing import Optional
from telethon import events
from telethon.tl.types import User, Channel
from sqlalchemy import select

from backend.config import DATA_DIR
from backend.db.database import AsyncSessionLocal
from backend.db import crud
from backend.db.models import Chat, CatchUpItem

logger = logging.getLogger(__name__)
from backend.telegram.client import get_client
from backend.ws.manager import ws_manager


def _send_native_notification(title: str, body: str) -> None:
    """Send a macOS native notification via osascript (fire-and-forget)."""
    try:
        t = title.replace("\\", "\\\\").replace('"', '\\"')
        b = body.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(
            ["osascript", "-e", f'display notification "{b}" with title "{t}"'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

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


_SAFE_URL_SCHEMES = frozenset(["http", "https", "ftp", "ftps", "mailto", "tel"])


def _safe_href(url: str) -> str:
    """Return the URL only if its scheme is in the allowlist, otherwise '#'.

    Also allows relative paths (starting with '/') for in-app navigation.
    """
    from urllib.parse import urlparse
    stripped = url.strip()
    # Allow relative paths for in-app links (e.g. /profile/123)
    if stripped.startswith("/"):
        return url
    try:
        parsed = urlparse(stripped)
        if parsed.scheme.lower() in _SAFE_URL_SCHEMES:
            return url
    except Exception:
        pass
    return "#"


def _sanitize_hrefs(html: str) -> str:
    """Replace any href with an unsafe scheme with '#'."""
    import re
    def _check(m):
        url = m.group(1)
        safe = _safe_href(url)
        return f'href="{safe}"'
    return re.sub(r'href="([^"]*)"', _check, html)


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
            lambda m: f'<a href="{_safe_href(m.group(2))}" target="_blank" rel="noopener noreferrer">[{m.group(1)}]</a>',
            text,
        )
        text = re.sub(
            r'\[([^\]]+)\]\s*\(([^)]+)\)',
            lambda m: f'<a href="{_safe_href(m.group(2))}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>',
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
            # Convert tg://user?id=X mention links to in-app profile links
            result = re.sub(
                r'href="tg://user\?id=(\d+)"',
                r'href="/profile/\1" data-mention="true"',
                result,
            )
            result = _markdown_pass(result)
            return _sanitize_hrefs(result)
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


async def sync_dialogs(limit: Optional[int] = None) -> None:
    """Fetch dialogs and upsert into the chats table.

    Args:
        limit: Max dialogs to fetch. None = all dialogs.
               Dialogs are returned most-recent-first by Telegram.
    """
    client = get_client()
    if not client:
        return

    async with AsyncSessionLocal() as db:
        async for dialog in client.iter_dialogs(limit=limit):
            entity = dialog.entity
            username = getattr(entity, "username", None)
            # dialog.name can be empty for old/inactive users → fall back
            name = dialog.name
            if not name or name == str(dialog.id):
                name = (
                    username
                    or getattr(entity, "title", None)
                    or str(dialog.id)
                )
            await crud.upsert_chat(
                db,
                telegram_id=dialog.id,
                name=name,
                chat_type=_chat_type(entity),
                username=username,
                unread_count=dialog.unread_count,
                members_count=getattr(entity, "participants_count", None),
            )


async def resolve_numeric_names() -> int:
    """Resolve chats that still have numeric telegram IDs as their name.

    Fetches the full entity from Telegram to get the real name.
    Returns the number of chats updated.
    """
    client = get_client()
    if not client:
        return 0

    updated = 0
    async with AsyncSessionLocal() as db:
        # Find private chats where name looks like a bare number
        # SQLite doesn't support regexp natively, so fetch and filter in Python
        all_private = (await db.execute(
            select(Chat).where(Chat.type == "private")
        )).scalars().all()
        rows = [c for c in all_private if c.name and c.name.isdigit()]

        for chat in rows:
            try:
                entity = await client.get_entity(chat.telegram_id)
                first = getattr(entity, "first_name", None) or ""
                last = getattr(entity, "last_name", None) or ""
                name = f"{first} {last}".strip()
                username = getattr(entity, "username", None)
                if not name:
                    name = username or str(chat.telegram_id)
                if name != chat.name:
                    chat.name = name
                    if username:
                        chat.username = username
                    updated += 1
            except Exception:
                # Entity not resolvable (deleted account, etc.) — skip
                continue

        if updated:
            await db.commit()

    logger.info("Resolved %d numeric chat names", updated)
    return updated


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
                unread_count=dialog.unread_count,
                members_count=getattr(entity, "participants_count", None),
            )
            if chat.is_hidden or chat.is_briefing:
                continue
            try:
                await sync_messages(dialog.id, limit=messages_per_chat)
            except ValueError:
                continue
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
                reply_to_telegram_id=msg.reply_to_msg_id,
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
                reply_to_telegram_id=msg.reply_to_msg_id,
            )

            # Auto-reopen handled catch-up items on new incoming messages
            if not saved.is_outgoing:
                catchup_item = (await db.execute(
                    select(CatchUpItem).where(
                        CatchUpItem.chat_telegram_id == event.chat_id,
                        CatchUpItem.status == "handled",
                    )
                )).scalar_one_or_none()
                if catchup_item:
                    catchup_item.status = "pending"
                    catchup_item.handled_at = None
                    catchup_item.updated_at = datetime.utcnow()
                    await db.commit()

        await ws_manager.broadcast({
            "type": "new_message",
            "chat_id": event.chat_id,
            "message_id": saved.id,
            "chat_name": chat.name,
            "sender_name": saved.sender_name,
            "text": (saved.text or "")[:120],
            "is_tracked": chat.is_tracked,
            "is_outgoing": saved.is_outgoing,
        })

        if not saved.is_outgoing:
            from backend.api.settings import get_notif_pref
            pref = get_notif_pref()
            if pref == "all" or (pref == "tracked" and chat.is_tracked):
                body_text = (saved.text or "")[:100]
                body = f"{saved.sender_name}: {body_text}" if saved.sender_name else body_text
                _send_native_notification(chat.name, body)

    @client.on(events.UserUpdate)
    async def on_user_update(event):
        """Handle typing indicators and online status changes."""
        if event.typing:
            # User is typing — broadcast to frontend
            user_id = event.user_id
            # Resolve user name for display
            try:
                entity = await client.get_entity(user_id)
                user_name = _sender_name(entity) or str(user_id)
            except Exception:
                user_name = str(user_id)
            await ws_manager.broadcast({
                "type": "user_typing",
                "user_id": user_id,
                "chat_id": event.chat_id or user_id,
                "user_name": user_name,
            })
        elif hasattr(event, "online") or hasattr(event, "status"):
            # Online status change
            user_id = event.user_id
            status_info = _extract_user_status(event)
            if status_info:
                await ws_manager.broadcast({
                    "type": "user_status",
                    "user_id": user_id,
                    **status_info,
                })


def _extract_user_status(event) -> Optional[dict]:
    """Extract online/offline/last-seen info from a UserUpdate event."""
    from telethon.tl.types import (
        UserStatusOnline,
        UserStatusOffline,
        UserStatusRecently,
        UserStatusLastWeek,
        UserStatusLastMonth,
    )
    status = getattr(event, "status", None)
    if status is None and hasattr(event, "online"):
        if event.online:
            return {"status": "online"}
        else:
            return {"status": "offline"}
    if isinstance(status, UserStatusOnline):
        return {"status": "online"}
    if isinstance(status, UserStatusOffline):
        last_seen = status.was_online.isoformat() if status.was_online else None
        return {"status": "offline", "last_seen": last_seen}
    if isinstance(status, UserStatusRecently):
        return {"status": "recently"}
    if isinstance(status, UserStatusLastWeek):
        return {"status": "last_week"}
    if isinstance(status, UserStatusLastMonth):
        return {"status": "last_month"}
    return None
