"""Media downloads: atomic, single-flight per message, plus a bounded
background queue for photo thumbnails discovered by sync/live updates.

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

from sqlalchemy import update

from backend.config import DATA_DIR
from backend.db.database import AsyncSessionLocal
from backend.db.models import Message
from backend.jobs import await_single
from backend.tasks import spawn
from backend.telegram.runtime import runtime
from backend.telegram.convert import media_type as _media_type

logger = logging.getLogger(__name__)

MEDIA_DIR = DATA_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)
PROFILE_PHOTO_DIR = DATA_DIR / "profile_photos"
PROFILE_PHOTO_DIR.mkdir(exist_ok=True)

_EXTENSIONS = {
    "photo": ".jpg",
    "video": ".mp4",
    "video_note": ".mp4",
    "voice": ".ogg",
    "audio": ".mp3",
}

DOWNLOADABLE_TYPES = frozenset(set(_EXTENSIONS) | {"document", "gif"})


class MediaUnavailable(Exception):
    """Telegram is not connected."""


class MediaNotFound(Exception):
    """The message or its media no longer exists on Telegram."""


class MediaIncomplete(Exception):
    """A downloaded file is shorter than Telegram says it is."""


def _document_size(tg_msg) -> Optional[int]:
    doc = getattr(tg_msg, "document", None)
    return getattr(doc, "size", None) if doc is not None else None


def file_is_complete(path: Path, media_type: Optional[str], expected_size: Optional[int]) -> bool:
    """Detect files truncated by an interrupted download.

    Exact size check when Telegram's size is known (documents, videos, voice,
    and the chosen photo thumbnail); otherwise JPEGs must end with the EOI
    marker. Stored photo ``media_size`` is the *largest* size, not the
    downloaded thumbnail, so it is never used for photos.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False
    if expected_size:
        return size == expected_size
    if media_type == "photo" or path.suffix.lower() in (".jpg", ".jpeg"):
        try:
            with open(path, "rb") as f:
                f.seek(-2, os.SEEK_END)
                return f.read(2) == b"\xff\xd9"
        except OSError:
            return False
    return True


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


def _final_path(chat_id: int, tg_msg_id: int, ext: str) -> Path:
    return MEDIA_DIR / str(chat_id) / f"{tg_msg_id}{ext}"


async def _persist_media_path(chat_id: int, tg_msg_id: int, rel: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Message)
            .where(Message.chat_id == chat_id, Message.tg_message_id == tg_msg_id)
            .values(media_path=rel)
        )
        await db.commit()


async def _download(chat_id: int, tg_msg_id: int, media_type: Optional[str], tg_msg, media_filename: Optional[str]) -> str:
    client = await runtime.client(require=False)
    if client is None:
        raise MediaUnavailable("Telegram client not connected")
    if tg_msg is None:
        tg_msg = await client.get_messages(chat_id, ids=tg_msg_id)
    if not tg_msg or not getattr(tg_msg, "media", None):
        raise MediaNotFound("Media not found on Telegram")
    media_type = media_type or _media_type(tg_msg)
    ext = _extension(media_type or "", tg_msg, media_filename)
    final = _final_path(chat_id, tg_msg_id, ext)
    rel = str(final.relative_to(DATA_DIR))
    thumb = pick_thumb(tg_msg) if media_type == "photo" else None
    expected = getattr(thumb, "size", None) if thumb is not None else _document_size(tg_msg)
    if final.exists() and not file_is_complete(final, media_type, expected):
        # Truncated by the old non-atomic download code: fetch it again.
        logger.info("Re-downloading truncated media %s", rel)
        final.unlink()
    if not final.exists():
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f".{tg_msg_id}.{uuid.uuid4().hex[:8]}.part{ext}")
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
    await _persist_media_path(chat_id, tg_msg_id, rel)
    return rel


async def download_message_media(
    chat_id: int,
    tg_msg_id: int,
    *,
    media_type: Optional[str] = None,
    tg_msg=None,
    media_filename: Optional[str] = None,
) -> str:
    """Download a message's media (photos as a ~600px thumbnail) and store
    ``media_path``. Concurrent callers for the same message share one
    download. Returns the path relative to DATA_DIR."""
    return await await_single(
        f"media:{chat_id}:{tg_msg_id}",
        lambda: _download(chat_id, tg_msg_id, media_type, tg_msg, media_filename),
    )


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
        chat_id, tg_msg_id, tg_msg = await queue.get()
        try:
            rel = await download_message_media(chat_id, tg_msg_id, media_type="photo", tg_msg=tg_msg)
            await ws_manager.broadcast({
                "type": "media_ready",
                "chat_id": chat_id,
                "tg_message_id": tg_msg_id,
                "media_type": "photo",
                "media_url": f"/api/media/{rel}",
            })
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Background photo download failed for %s/%s: %s", chat_id, tg_msg_id, e)
        finally:
            queue.task_done()


async def audit_media_files() -> int:
    """Startup pass: find stored media files that are truncated (left by the
    old non-atomic downloader), delete them and clear ``media_path`` so the
    UI re-downloads through the atomic path. Returns the number cleared."""
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Message.chat_id, Message.tg_message_id, Message.media_path, Message.media_type, Message.media_size)
            .where(Message.media_path.isnot(None))
        )).all()

    def _scan() -> list:
        bad = []
        for chat_id, tg_id, rel, mtype, msize in rows:
            path = DATA_DIR / rel
            expected = msize if mtype != "photo" else None
            if not file_is_complete(path, mtype, expected):
                bad.append((chat_id, tg_id, path))
        return bad

    bad = await asyncio.to_thread(_scan)
    if not bad:
        return 0
    async with AsyncSessionLocal() as db:
        for chat_id, tg_id, path in bad:
            try:
                path.unlink()
            except OSError:
                pass
            await db.execute(
                update(Message)
                .where(Message.chat_id == chat_id, Message.tg_message_id == tg_id)
                .values(media_path=None)
            )
        await db.commit()
    logger.info("Cleared %d truncated/missing media files", len(bad))
    return len(bad)


def enqueue_photo(chat_id: int, tg_msg) -> bool:
    """Queue a photo thumbnail download. Returns False when the queue is full
    (the on-demand /api/media-download endpoint covers whatever is dropped)."""
    if _media_type(tg_msg) != "photo":
        return False
    try:
        _ensure_queue().put_nowait((chat_id, tg_msg.id, tg_msg))
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


async def download_profile_photo(client, entity, dialog_id: int) -> None:
    """Download a chat/user profile photo once, atomically."""
    final = PROFILE_PHOTO_DIR / f"{dialog_id}.jpg"
    if final.exists():
        return
    tmp = final.with_name(f".{dialog_id}.{uuid.uuid4().hex[:8]}.part.jpg")
    try:
        out = await client.download_profile_photo(entity, file=str(tmp), download_big=False)
        if out:
            os.replace(out, final)
    except Exception:
        logger.debug("Profile photo download failed for %s", dialog_id, exc_info=True)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
