"""Telegram media downloads: atomic, single-flight per attachment, plus a
bounded background queue for photo thumbnails discovered by sync/live
updates, and profile photos stored as identity / conversation avatars.

Every download writes to a sibling temp file and is renamed into place, so an
interrupted or concurrent download can never leave a truncated file that is
then served forever.
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from sqlalchemy import select, update

from backend.config import DATA_DIR
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, Identity, Message, MessageAttachment
from backend.jobs import await_single
from backend.media import AVATAR_DIR, MEDIA_DIR, MediaIncomplete, MediaNotFound, MediaUnavailable, file_is_complete
from backend.serializers import media_url
from backend.tasks import spawn
from backend.telegram import ids
from backend.telegram.runtime import runtime
from backend.telegram.convert import media_type as _media_type

logger = logging.getLogger(__name__)


_EXTENSIONS = {
    "photo": ".jpg",
    "video": ".mp4",
    "video_note": ".mp4",
    "voice": ".ogg",
    "audio": ".mp3",
}


def _document_size(tg_msg) -> Optional[int]:
    doc = getattr(tg_msg, "document", None)
    return getattr(doc, "size", None) if doc is not None else None


def pick_thumb(msg):
    """Smallest photo size at least 600px wide (Telegram 'x'), else the largest."""
    from telethon.tl.types import PhotoSize
    photo = getattr(msg, "photo", None)
    if not photo or not hasattr(photo, "sizes"):
        return None
    sizes = sorted(
        (s for s in photo.sizes if isinstance(s, PhotoSize) and hasattr(s, "w")),
        key=lambda s: s.w,
    )
    if not sizes:
        return None
    for s in sizes:
        if s.w >= 600:
            return s
    return sizes[-1]


def _extension(media_type: str, tg_msg, media_filename: Optional[str]) -> str:
    if media_type in _EXTENSIONS:
        return _EXTENSIONS[media_type]
    if media_filename:
        suffix = Path(media_filename).suffix
        if suffix and len(suffix) <= 10 and "/" not in suffix:
            return suffix.lower()
    doc = getattr(tg_msg, "document", None)
    mime = getattr(doc, "mime_type", "") or ""
    if media_type == "gif":
        return ".gif" if mime == "image/gif" else ".mp4"
    try:
        from telethon import utils
        return utils.get_extension(tg_msg.media) or ""
    except Exception:
        return ""


def _final_path(conversation_id: int, attachment_id: int, ext: str) -> Path:
    return MEDIA_DIR / str(conversation_id) / f"{attachment_id}{ext}"


async def _attachment_context(attachment_id: int):
    """(attachment, message, conversation) or MediaNotFound."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(MessageAttachment, Message)
            .join(Message, Message.id == MessageAttachment.message_id)
            .where(MessageAttachment.id == attachment_id)
        )).first()
        if row is None:
            raise MediaNotFound("Attachment not found")
        att, msg = row
        conv = await crud.get_conversation(db, msg.conversation_id)
    if conv is None or conv.account is None or conv.account.channel != ids.CHANNEL:
        raise MediaNotFound("Not a Telegram attachment")
    return att, msg, conv


async def _download(attachment_id: int, tg_msg) -> str:
    att, msg, conv = await _attachment_context(attachment_id)
    client = await runtime.client(require=False)
    if client is None:
        raise MediaUnavailable("Telegram client not connected")
    if tg_msg is None:
        tg_msg = await client.get_messages(ids.peer_id_of(conv), ids=ids.tg_message_id_of(msg))
    if not tg_msg or not getattr(tg_msg, "media", None):
        raise MediaNotFound("Media not found on Telegram")
    media_type = att.kind or _media_type(tg_msg)
    ext = _extension(media_type or "", tg_msg, att.filename)
    final = _final_path(conv.id, att.id, ext)
    rel = str(final.relative_to(DATA_DIR))
    thumb = pick_thumb(tg_msg) if media_type == "photo" else None
    expected = getattr(thumb, "size", None) if thumb is not None else _document_size(tg_msg)
    if final.exists() and not file_is_complete(final, media_type, expected):
        logger.info("Re-downloading truncated media %s", rel)
        final.unlink()
    if not final.exists():
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f".{att.id}.{uuid.uuid4().hex[:8]}.part{ext}")
        out = None
        try:
            out = await client.download_media(tg_msg, file=str(tmp), thumb=thumb)
            if not out:
                raise MediaNotFound("Telegram returned no media")
            if not file_is_complete(Path(out), media_type, expected):
                raise MediaIncomplete(f"Download of {rel} is incomplete")
            os.replace(out, final)
        finally:
            for leftover in {str(tmp), str(out) if out else None} - {None}:
                if leftover != str(final) and os.path.exists(leftover):
                    try:
                        os.unlink(leftover)
                    except OSError:
                        pass
    async with AsyncSessionLocal() as db:
        await crud.set_attachment_local_path(db, att.id, rel)
    return rel


async def download_attachment(attachment_id: int, *, tg_msg=None) -> str:
    """Download a Telegram attachment (photos as a ~600px thumbnail) and
    store its ``local_path``. Concurrent callers for the same attachment
    share one download. Returns the path relative to DATA_DIR."""
    return await await_single(f"media:{attachment_id}", lambda: _download(attachment_id, tg_msg))


# ---------------------------------------------------------------------------
# Background photo queue
# ---------------------------------------------------------------------------

_QUEUE_SIZE = 300
_WORKERS = 2
_queue: Optional[asyncio.Queue] = None
_queue_loop = None
_worker_tasks: list[asyncio.Task] = []


def _ensure_queue() -> asyncio.Queue:
    global _queue, _queue_loop, _worker_tasks
    loop = asyncio.get_running_loop()
    if _queue is None or _queue_loop is not loop:
        _queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        _queue_loop = loop
        _worker_tasks = [spawn(_worker(_queue), name=f"media_worker_{i}") for i in range(_WORKERS)]
    return _queue


async def _worker(queue: asyncio.Queue) -> None:
    from backend.ws.manager import ws_manager
    while True:
        conversation_id, attachment_id, tg_msg = await queue.get()
        try:
            rel = await download_attachment(attachment_id, tg_msg=tg_msg)
            async with AsyncSessionLocal() as db:
                message_id = (await db.execute(
                    select(MessageAttachment.message_id).where(MessageAttachment.id == attachment_id)
                )).scalar()
            await ws_manager.broadcast({
                "type": "media_ready",
                "conversation_id": conversation_id,
                "message_id": message_id,
                "attachment_id": attachment_id,
                "kind": "photo",
                "url": media_url(rel),
            })
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Background photo download failed for attachment %s: %s", attachment_id, e)
        finally:
            queue.task_done()



def enqueue_photo(conversation_id: int, attachment_id: int, tg_msg) -> bool:
    """Queue a photo thumbnail download. Returns False when the queue is full
    (the on-demand download endpoint covers whatever is dropped)."""
    if _media_type(tg_msg) != "photo":
        return False
    try:
        _ensure_queue().put_nowait((conversation_id, attachment_id, tg_msg))
        return True
    except asyncio.QueueFull:
        return False


def stop_media_workers() -> None:
    global _queue, _queue_loop, _worker_tasks
    for t in _worker_tasks:
        t.cancel()
    _worker_tasks = []
    _queue = None
    _queue_loop = None


async def _download_avatar(client, entity, final: Path) -> bool:
    """Download a profile photo to ``final`` once, atomically. Returns
    whether the file exists afterwards."""
    if final.exists():
        return True
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f".{final.stem}.{uuid.uuid4().hex[:8]}.part.jpg")
    try:
        out = await client.download_profile_photo(entity, file=str(tmp), download_big=False)
        if out:
            os.replace(out, final)
    except Exception:
        logger.debug("Profile photo download failed for %s", final.name, exc_info=True)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return final.exists()


async def download_avatar(client, entity, *, conversation_id: Optional[int] = None,
                          identity_id: Optional[int] = None) -> Optional[str]:
    """Store ``entity``'s profile photo as the avatar of an identity (users)
    or a conversation (groups/channels). Returns the relative path."""
    if identity_id is not None:
        final = AVATAR_DIR / "identities" / f"{identity_id}.jpg"
        table, key = Identity, identity_id
    elif conversation_id is not None:
        final = AVATAR_DIR / "conversations" / f"{conversation_id}.jpg"
        table, key = Conversation, conversation_id
    else:
        raise ValueError("download_avatar needs conversation_id or identity_id")
    if not await _download_avatar(client, entity, final):
        return None
    rel = str(final.relative_to(DATA_DIR))
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(table).where(table.id == key, table.avatar_path.is_distinct_from(rel)).values(avatar_path=rel)
        )
        await db.commit()
    return rel
