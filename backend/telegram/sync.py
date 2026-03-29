import json
import logging
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

try:
    import Foundation
    import objc

    _NS_AVAILABLE = True
except ImportError:
    _NS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Notification delegate – handles clicks and navigates to the right chat
# ---------------------------------------------------------------------------
_webview_window = None  # set by main.py after webview.create_window()


def set_webview_window(window):
    """Register the pywebview window so notification clicks can navigate."""
    global _webview_window
    _webview_window = window


if _NS_AVAILABLE:
    NSUserNotificationCenter = objc.lookUpClass("NSUserNotificationCenter")
    NSUserNotification = objc.lookUpClass("NSUserNotification")

    class _NotificationDelegate(Foundation.NSObject):
        """Delegate that forces notification display and handles clicks."""

        def userNotificationCenter_shouldPresentNotification_(
            self, center, notification
        ):
            return True

        def userNotificationCenter_didActivateNotification_(
            self, center, notification
        ):
            # Bring app window to front
            try:
                from AppKit import NSApplication
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            except Exception:
                pass

            user_info = notification.userInfo()
            chat_id = user_info.get("chat_id") if user_info else None
            if chat_id and _webview_window:
                try:
                    _webview_window.evaluate_js(
                        f"window.history.pushState(null, '', '/chats?chatId={chat_id}');"
                        f"window.dispatchEvent(new PopStateEvent('popstate'));"
                    )
                except Exception:
                    pass

    _delegate = _NotificationDelegate.alloc().init()
    NSUserNotificationCenter.defaultUserNotificationCenter().setDelegate_(_delegate)


def _send_native_notification(
    title: str, body: str, chat_id: Optional[int] = None
) -> None:
    """Send a macOS native notification attributed to the Mingly app."""
    if _NS_AVAILABLE:
        try:
            n = NSUserNotification.alloc().init()
            n.setTitle_(title)
            n.setInformativeText_(body)
            if chat_id is not None:
                n.setUserInfo_({"chat_id": chat_id})
            NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(n)
            return
        except Exception:
            pass
    # Fallback: no-op if pyobjc is missing (dev without pyobjc)
    logger.debug("Native notification unavailable, skipping")

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
    if getattr(msg, "video_note", None):
        return "video_note"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "document", None):
        doc = msg.document
        mime = getattr(doc, "mime_type", "") or ""
        # Check if document is an animated GIF
        if mime == "image/gif":
            return "gif"
        # Check for video GIF (mp4 sent as GIF in Telegram)
        if doc and hasattr(doc, "attributes"):
            for attr in doc.attributes:
                if hasattr(attr, "round_message"):
                    continue
                if type(attr).__name__ == "DocumentAttributeAnimated":
                    return "gif"
                # Document with video attributes is a video
                if type(attr).__name__ == "DocumentAttributeVideo":
                    return "video"
        # Check MIME type for video
        if mime.startswith("video/"):
            return "video"
        # Check MIME type for audio
        if mime.startswith("audio/"):
            return "audio"
        return "document"
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "gif", None):
        return "gif"
    return None


def _media_filename(msg) -> Optional[str]:
    """Extract filename from a document message."""
    doc = getattr(msg, "document", None)
    if not doc:
        return None
    if hasattr(doc, "attributes"):
        for attr in doc.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                return attr.file_name
    return None


def _media_size(msg) -> Optional[int]:
    """Extract file size from media message."""
    doc = getattr(msg, "document", None)
    if doc and hasattr(doc, "size"):
        return doc.size
    photo = getattr(msg, "photo", None)
    if photo and hasattr(photo, "sizes"):
        sizes = [s for s in photo.sizes if hasattr(s, "size")]
        if sizes:
            return max(s.size for s in sizes)
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
    has_raw_markdown = bool(re.search(r'\*\*|\[\[|\]\s*\(https?://', text))

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


PROFILE_PHOTO_DIR = DATA_DIR / "profile_photos"
PROFILE_PHOTO_DIR.mkdir(exist_ok=True)


async def _download_chat_photo(client, entity, dialog_id: int) -> None:
    """Download chat/user profile photo to disk (fire-and-forget)."""
    try:
        photo_path = PROFILE_PHOTO_DIR / f"{dialog_id}.jpg"
        if photo_path.exists():
            return
        downloaded = await client.download_profile_photo(
            entity, file=str(photo_path), download_big=False
        )
        if not downloaded and photo_path.exists():
            photo_path.unlink()
    except Exception:
        pass


async def sync_dialogs(limit: Optional[int] = None) -> None:
    """Fetch dialogs and upsert into the chats table.

    Args:
        limit: Max dialogs to fetch. None = all dialogs.
               Dialogs are returned most-recent-first by Telegram.
    """
    client = get_client()
    if not client:
        return

    entities_to_download = []
    chats_with_unread_mentions = []

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
            # dialog.date is the timestamp of the last message in this chat
            last_msg_date = getattr(dialog, "date", None)
            if last_msg_date and hasattr(last_msg_date, "replace"):
                last_msg_date = last_msg_date.replace(tzinfo=None)

            await crud.upsert_chat(
                db,
                telegram_id=dialog.id,
                name=name,
                chat_type=_chat_type(entity),
                username=username,
                unread_count=dialog.unread_count,
                members_count=getattr(entity, "participants_count", None),
                is_forum=getattr(entity, "forum", False) or False,
                slow_mode_seconds=getattr(entity, "slowmode_seconds", 0) or 0,
                last_message_date=last_msg_date,
            )

            # Track chats with unread mentions for priority sync
            unread_mentions = getattr(dialog, "unread_mentions_count", 0) or 0
            if unread_mentions > 0:
                chats_with_unread_mentions.append((dialog.id, name, unread_mentions))

            # Queue photo download (don't block dialog sync)
            if getattr(entity, "photo", None):
                entities_to_download.append((entity, dialog.id))

    # Priority-sync chats with unread mentions (deep sync to catch mention messages)
    if chats_with_unread_mentions:
        logger.info(
            "Syncing %d chats with unread mentions: %s",
            len(chats_with_unread_mentions),
            [(n, c) for cid, n, c in chats_with_unread_mentions],
        )
        for chat_id, name, count in chats_with_unread_mentions:
            try:
                await sync_messages(chat_id, limit=max(100, count * 5))
            except Exception as e:
                logger.warning("Failed to sync mention chat %s (%s): %s", name, chat_id, e)

    # Download profile photos in background
    for entity, did in entities_to_download:
        await _download_chat_photo(client, entity, did)


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


async def sync_global_mentions(days: int = 7) -> int:
    """Search Telegram globally for messages mentioning the current user.

    Uses Telegram's SearchGlobalRequest to find @username mentions across all
    chats without requiring a full message sync. Saves found messages to DB.

    Returns count of mention messages saved.
    """
    client = get_client()
    if not client:
        return 0

    try:
        me = await client.get_me()
    except Exception:
        return 0

    username = me.username if me else None
    if not username:
        return 0

    from telethon.tl.functions.messages import SearchGlobalRequest
    from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty
    from datetime import timedelta
    import time

    min_date = datetime.utcnow() - timedelta(days=days)
    saved_count = 0

    try:
        result = await client(SearchGlobalRequest(
            q=f"@{username}",
            filter=InputMessagesFilterEmpty(),
            min_date=min_date,
            max_date=None,
            offset_rate=0,
            offset_peer=InputPeerEmpty(),
            offset_id=0,
            limit=100,
        ))

        # Build a map of chat entities from the result
        chats_map = {}
        for c in getattr(result, "chats", []):
            chats_map[c.id] = c
        for u in getattr(result, "users", []):
            chats_map[u.id] = u

        async with AsyncSessionLocal() as db:
            for msg in getattr(result, "messages", []):
                if not msg.message and not msg.media:
                    continue
                # Determine chat_id using Telethon's utility for correct sign conversion
                peer = msg.peer_id
                if not peer:
                    continue
                from telethon import utils as tl_utils
                chat_id = tl_utils.get_peer_id(peer)

                sender = chats_map.get(msg.from_id.user_id) if msg.from_id and hasattr(msg.from_id, "user_id") else None
                text_html = _entities_to_html(msg.message or "", msg.entities)

                await crud.save_message(
                    db,
                    tg_message_id=msg.id,
                    chat_id=chat_id,
                    sender_id=msg.from_id.user_id if msg.from_id and hasattr(msg.from_id, "user_id") else None,
                    sender_name=_sender_name(sender),
                    text=msg.message or "",
                    text_html=text_html,
                    reactions_json=None,
                    date=msg.date.replace(tzinfo=None) if msg.date else datetime.utcnow(),
                    is_outgoing=msg.out or False,
                    media_type=_media_type(msg),
                    media_path=None,
                    reply_to_tg_message_id=getattr(msg.reply_to, "reply_to_msg_id", None) if msg.reply_to else None,
                    is_edited=bool(msg.edit_date),
                )
                saved_count += 1

        logger.info("Global mention search: found %d messages for @%s", saved_count, username)
    except Exception as e:
        logger.warning("Global mention search failed: %s", e)

    return saved_count


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
                is_forum=getattr(entity, "forum", False) or False,
                slow_mode_seconds=getattr(entity, "slowmode_seconds", 0) or 0,
            )
            if chat.is_hidden or chat.is_briefing:
                continue
            try:
                await sync_messages(dialog.id, limit=messages_per_chat)
                synced += 1
                await ws_manager.broadcast({"type": "chat_synced", "chat_id": dialog.id})
            except Exception as e:
                logger.warning("bulk_sync: failed to sync chat %s (%s): %s", dialog.name, dialog.id, e)
                continue

    # Notify frontend that bulk sync is done so pages can refresh
    await ws_manager.broadcast({"type": "bulk_sync_complete", "synced": synced})
    return synced


async def scan_unscanned_messages(max_messages: int = 200, max_age_hours: int = 48) -> int:
    """Scan recent messages that haven't been scam-checked yet.

    Called after bulk_sync to catch messages that arrived while the app was closed.
    Skips first-time sync (when the DB had no messages before sync started).
    Groups messages by chat to reduce LLM calls.
    """
    from datetime import timedelta
    from backend.db.models import Message, Chat, Contact
    from backend.scam.service import get_scam_service

    scam_svc = get_scam_service()
    if not scam_svc:
        return 0

    cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
    scanned = 0

    async with AsyncSessionLocal() as db:
        # Check if this is a first-time sync by looking for any message older than the cutoff
        # If no old messages exist, this is likely initial onboarding — skip bulk scanning
        old_msg_result = await db.execute(
            select(Message.id).where(Message.date < cutoff).limit(1)
        )
        if old_msg_result.scalar() is None:
            logger.info("scan_unscanned_messages: no messages older than %dh — likely first sync, skipping", max_age_hours)
            return 0

        # Find recent incoming messages with no scam_score
        result = await db.execute(
            select(Message).where(
                Message.scam_score.is_(None),
                Message.is_outgoing == False,
                Message.text.isnot(None),
                Message.text != "",
                Message.date >= cutoff,
            ).order_by(Message.date.desc()).limit(max_messages)
        )
        unscanned = result.scalars().all()

        if not unscanned:
            logger.info("scan_unscanned_messages: no unscanned messages found")
            return 0

        logger.info("scan_unscanned_messages: found %d unscanned messages to check", len(unscanned))

        # Group by chat for batch processing
        by_chat: dict[int, list] = {}
        for m in unscanned:
            by_chat.setdefault(m.chat_id, []).append(m)

        # Resolve chat names
        chat_ids = list(by_chat.keys())
        chat_result = await db.execute(
            select(Chat).where(Chat.telegram_id.in_(chat_ids))
        )
        chat_map = {c.telegram_id: c for c in chat_result.scalars().all()}

        # Resolve contact status for senders
        sender_ids = list({m.sender_id for m in unscanned if m.sender_id})
        contact_ids: set[int] = set()
        if sender_ids:
            contact_result = await db.execute(
                select(Contact.telegram_id).where(Contact.telegram_id.in_(sender_ids))
            )
            contact_ids = {row[0] for row in contact_result.fetchall()}

    # Process each chat group (outside the DB session to avoid long transactions)
    import asyncio as _asyncio
    for chat_id, msgs in by_chat.items():
        chat = chat_map.get(chat_id)
        chat_metadata = {
            "chat_id": chat_id,
            "name": chat.name if chat else "Unknown",
            "type": chat.type if chat else "private",
        }

        # Build profiles from the messages' senders
        seen_senders: set[int] = set()
        profiles = []
        for m in msgs:
            if m.sender_id and m.sender_id not in seen_senders:
                seen_senders.add(m.sender_id)
                profiles.append({
                    "telegram_id": m.sender_id,
                    "name": m.sender_name,
                    "bio": "",
                    "is_contact": m.sender_id in contact_ids,
                })

        messages_payload = [
            {
                "message_id": m.tg_message_id,
                "sender_id": m.sender_id,
                "sender_name": m.sender_name,
                "text": m.text,
                "date": str(m.date) if m.date else None,
            }
            for m in msgs
        ]

        # Fire-and-forget per chat (don't block other chats)
        async def _check(cid, mp, pr, cm):
            try:
                await scam_svc.check_messages_for_scam(
                    chat_id=cid, messages=mp, user_telegram_id=0,
                    profiles=pr, chat_metadata=cm,
                )
            except Exception:
                logger.exception("scan_unscanned_messages: failed for chat %s", cid)

        _asyncio.create_task(_check(chat_id, messages_payload, profiles, chat_metadata))
        scanned += len(msgs)

    logger.info("scan_unscanned_messages: queued %d messages across %d chats for scanning", scanned, len(by_chat))
    return scanned


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

    synced_tg_ids: set[int] = set()
    oldest_synced_date = None

    async with AsyncSessionLocal() as db:
        async for msg in client.iter_messages(chat_telegram_id, limit=limit):
            if not msg.text and not msg.media:
                continue

            synced_tg_ids.add(msg.id)
            msg_date = msg.date.replace(tzinfo=None)
            if oldest_synced_date is None or msg_date < oldest_synced_date:
                oldest_synced_date = msg_date

            sender = await msg.get_sender() if msg.sender_id else None
            media_path = await _download_photo(client, msg)
            text_html = _entities_to_html(msg.text or "", msg.entities)
            reactions_json = _extract_reactions(msg)
            # Extract forum topic id from reply_to header
            topic_id = None
            reply_to = getattr(msg, "reply_to", None)
            if reply_to and getattr(reply_to, "forum_topic", False):
                topic_id = getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)
            # Determine delivery state for outgoing messages
            delivery_state = "sent"
            if msg.out:
                delivery_state = "sent"

            await crud.save_message(
                db,
                tg_message_id=msg.id,
                chat_id=chat_telegram_id,
                sender_id=msg.sender_id,
                sender_name=_sender_name(sender),
                text=msg.text or "",
                text_html=text_html,
                reactions_json=reactions_json,
                date=msg_date,
                is_outgoing=msg.out,
                media_type=_media_type(msg),
                media_path=media_path,
                reply_to_tg_message_id=msg.reply_to_msg_id,
                topic_id=topic_id,
                is_pinned=getattr(msg, "pinned", False) or False,
                media_filename=_media_filename(msg),
                media_size=_media_size(msg),
                grouped_id=getattr(msg, "grouped_id", None),
                delivery_state=delivery_state,
                is_edited=bool(msg.edit_date),
            )

        # Delete locally stored messages that were deleted on Telegram
        # Only within the synced date range to avoid removing older unsynced messages
        if synced_tg_ids and oldest_synced_date:
            from backend.db.models import Message as MsgModel
            from sqlalchemy import delete as sa_delete

            local_result = await db.execute(
                select(MsgModel.tg_message_id).where(
                    MsgModel.chat_id == chat_telegram_id,
                    MsgModel.date >= oldest_synced_date,
                )
            )
            local_ids = {row[0] for row in local_result.fetchall()}
            deleted_ids = local_ids - synced_tg_ids

            if deleted_ids:
                await db.execute(
                    sa_delete(MsgModel).where(
                        MsgModel.chat_id == chat_telegram_id,
                        MsgModel.tg_message_id.in_(list(deleted_ids)),
                    )
                )
                await db.commit()
                logger.info("sync_messages: removed %d deleted messages from chat %s", len(deleted_ids), chat_telegram_id)

        # Fetch and store pinned message id + content for the chat
        try:
            entity = await client.get_entity(chat_telegram_id)
            # get_entity returns the basic entity; pinned_msg_id may be there
            # or we may need the full chat info
            pinned_msg_id = getattr(entity, "pinned_msg_id", None)
            if pinned_msg_id is None:
                # Try getting full chat info which has pinned_msg_id for channels/groups
                try:
                    full = await client(
                        __import__('telethon.tl.functions.channels', fromlist=['GetFullChannelRequest']).GetFullChannelRequest(entity)
                    )
                    pinned_msg_id = getattr(full.full_chat, "pinned_msg_id", None)
                except Exception:
                    try:
                        full = await client(
                            __import__('telethon.tl.functions.messages', fromlist=['GetFullChatRequest']).GetFullChatRequest(chat_id=abs(chat_telegram_id))
                        )
                        pinned_msg_id = getattr(full.full_chat, "pinned_msg_id", None)
                    except Exception:
                        pass
            if pinned_msg_id:
                await crud.set_chat_pinned_message(db, chat_telegram_id, pinned_msg_id)
                # Fetch the pinned message itself so it's available for display
                try:
                    msgs = await client.get_messages(chat_telegram_id, ids=[pinned_msg_id])
                    if msgs and msgs[0]:
                        pmsg = msgs[0]
                        sender = await pmsg.get_sender() if pmsg.sender_id else None
                        text_html = _entities_to_html(pmsg.text or "", pmsg.entities)
                        saved = await crud.save_message(
                            db,
                            tg_message_id=pmsg.id,
                            chat_id=chat_telegram_id,
                            sender_id=pmsg.sender_id,
                            sender_name=_sender_name(sender),
                            text=pmsg.text or "",
                            text_html=text_html,
                            reactions_json=_extract_reactions(pmsg),
                            date=pmsg.date.replace(tzinfo=None),
                            is_outgoing=pmsg.out,
                            media_type=_media_type(pmsg),
                            media_path=await _download_photo(client, pmsg),
                            reply_to_tg_message_id=pmsg.reply_to_msg_id if hasattr(pmsg, 'reply_to_msg_id') else None,
                            is_pinned=True,
                            media_filename=_media_filename(pmsg),
                            media_size=_media_size(pmsg),
                            is_edited=bool(pmsg.edit_date),
                        )
                except Exception:
                    pass
        except Exception:
            pass

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
            chat = await crud.get_chat_by_telegram_id(db, event.chat_id)
            if not chat:
                # Auto-create the chat for new conversations
                try:
                    entity = await client.get_entity(event.chat_id)
                    chat_type = _chat_type(entity)
                    name = getattr(entity, "title", None) or (
                        f"{getattr(entity, 'first_name', '') or ''} {getattr(entity, 'last_name', '') or ''}".strip()
                    ) or str(event.chat_id)
                    username = getattr(entity, "username", None)
                    chat = await crud.upsert_chat(
                        db, event.chat_id, name, chat_type,
                        username=username,
                        last_message_date=msg.date.replace(tzinfo=None) if msg.date else None,
                    )
                    await db.commit()
                    logger.info("Auto-created chat %s (%s) from incoming message", name, event.chat_id)
                except Exception:
                    logger.warning("Failed to auto-create chat %s", event.chat_id, exc_info=True)
                    return

            sender = await msg.get_sender() if msg.sender_id else None
            media_path = await _download_photo(client, msg)
            text_html = _entities_to_html(msg.text or "", msg.entities)
            reactions_json = _extract_reactions(msg)
            # Extract forum topic id from reply_to header
            topic_id = None
            reply_to = getattr(msg, "reply_to", None)
            if reply_to and getattr(reply_to, "forum_topic", False):
                topic_id = getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)
            saved = await crud.save_message(
                db,
                tg_message_id=msg.id,
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
                reply_to_tg_message_id=msg.reply_to_msg_id,
                topic_id=topic_id,
                is_pinned=getattr(msg, "pinned", False) or False,
                media_filename=_media_filename(msg),
                media_size=_media_size(msg),
                grouped_id=getattr(msg, "grouped_id", None),
                delivery_state="sending" if msg.out else "sent",
                is_edited=bool(msg.edit_date),
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

        # Run scam detection on incoming messages (fire-and-forget)
        if not saved.is_outgoing and saved.text:
            from backend.scam.service import get_scam_service
            scam_svc = get_scam_service()
            if scam_svc:
                import asyncio as _asyncio
                from backend.scam.pipeline import get_pipeline_manager
                from backend.db.models import Message as MsgModel
                # Build sender profile for scam context
                _sender_profile = []
                if sender:
                    _sender_profile = [{
                        "telegram_id": saved.sender_id,
                        "name": saved.sender_name,
                        "bio": getattr(sender, "about", None) or "",
                        "is_contact": getattr(sender, "contact", False) or False,
                    }]

                # Fetch recent messages from the same sender for context
                context_limit = 5
                mgr = get_pipeline_manager()
                if mgr:
                    context_limit = mgr.get_config().context_messages

                _saved_msg = saved  # capture for closure
                _chat_id = event.chat_id
                _chat_name = chat.name
                _chat_type = chat.type

                async def _run_scam_check():
                    try:
                        # Fetch recent messages from same sender in same chat
                        scan_messages = [{
                            "message_id": _saved_msg.tg_message_id,
                            "sender_id": _saved_msg.sender_id,
                            "sender_name": _saved_msg.sender_name,
                            "text": _saved_msg.text,
                            "date": str(_saved_msg.date),
                        }]
                        if context_limit > 0 and _saved_msg.sender_id:
                            async with AsyncSessionLocal() as ctx_db:
                                ctx_result = await ctx_db.execute(
                                    select(MsgModel).where(
                                        MsgModel.chat_id == _chat_id,
                                        MsgModel.sender_id == _saved_msg.sender_id,
                                        MsgModel.tg_message_id != _saved_msg.tg_message_id,
                                        MsgModel.text.isnot(None),
                                        MsgModel.text != "",
                                    ).order_by(MsgModel.date.desc()).limit(context_limit)
                                )
                                ctx_msgs = ctx_result.scalars().all()
                                # Prepend context messages (oldest first)
                                for m in reversed(ctx_msgs):
                                    scan_messages.insert(0, {
                                        "message_id": m.tg_message_id,
                                        "sender_id": m.sender_id,
                                        "sender_name": m.sender_name,
                                        "text": m.text,
                                        "date": str(m.date) if m.date else None,
                                    })

                        await scam_svc.check_messages_for_scam(
                            chat_id=_chat_id,
                            messages=scan_messages,
                            user_telegram_id=0,
                            profiles=_sender_profile,
                            chat_metadata={"chat_id": _chat_id, "name": _chat_name, "type": _chat_type},
                        )
                    except Exception:
                        logger.exception("Scam check failed for chat %s msg %s", _chat_id, _saved_msg.tg_message_id)
                _asyncio.create_task(_run_scam_check())

        if not saved.is_outgoing:
            from backend.api.settings import get_notif_pref
            pref = get_notif_pref()
            if pref == "all" or (pref == "tracked" and chat.is_tracked):
                body_text = (saved.text or "")[:100]
                body = f"{saved.sender_name}: {body_text}" if saved.sender_name else body_text
                _send_native_notification(chat.name, body, chat_id=event.chat_id)

    @client.on(events.MessageDeleted)
    async def on_message_deleted(event):
        """Handle message deletions — remove from local DB and notify frontend."""
        deleted_ids = event.deleted_ids
        chat_id = getattr(event, 'chat_id', None)

        if not deleted_ids:
            return

        from backend.db.models import Message as MsgModel
        from sqlalchemy import delete as sa_delete

        async with AsyncSessionLocal() as db:
            if chat_id:
                # Channel/group deletion — we know the chat
                await db.execute(
                    sa_delete(MsgModel).where(
                        MsgModel.chat_id == chat_id,
                        MsgModel.tg_message_id.in_(deleted_ids),
                    )
                )
            else:
                # Private chat deletion — no chat_id, delete by message ID across all chats
                await db.execute(
                    sa_delete(MsgModel).where(
                        MsgModel.tg_message_id.in_(deleted_ids),
                    )
                )
            await db.commit()

        logger.info("Deleted %d messages (chat_id=%s)", len(deleted_ids), chat_id)

        await ws_manager.broadcast({
            "type": "messages_deleted",
            "chat_id": chat_id,
            "message_ids": deleted_ids,
        })

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
