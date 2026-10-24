"""Channel-neutral attachment storage: where media files live, the errors a
connector's ``fetch_attachment`` raises, and the integrity checks used for
every channel's downloads.

Files live under ``DATA_DIR/media/<conversation_id>/``; avatars under
``media/avatars/``. ``MessageAttachment.local_path`` stores the path relative
to DATA_DIR.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from sqlalchemy import select, update

from backend.config import DATA_DIR
from backend.db.database import AsyncSessionLocal
from backend.db.models import MessageAttachment
from backend.media_paths import MEDIA_DIR

logger = logging.getLogger(__name__)

MEDIA_DIR.mkdir(exist_ok=True)

# Attachment kinds the UI may ask to download on demand.
DOWNLOADABLE_TYPES = frozenset({"photo", "video", "video_note", "voice", "audio", "document", "gif"})


class MediaUnavailable(Exception):
    """The attachment's channel is not connected."""


class MediaNotFound(Exception):
    """The message or its media no longer exists on the channel."""


class MediaIncomplete(Exception):
    """A downloaded file is shorter than the channel says it is."""


def file_is_complete(path: Path, media_type: Optional[str], expected_size: Optional[int]) -> bool:
    """Detect files truncated by an interrupted download.

    Exact size check when the channel's size is known (documents, videos,
    voice, and the chosen photo thumbnail); otherwise JPEGs must end with the
    EOI marker. Stored photo ``media_size`` is the *largest* size, not the
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


async def audit_media_files() -> int:
    """Startup pass: find stored attachment files that are truncated or
    missing, delete them and clear ``local_path`` so the UI re-downloads
    through the atomic path. Returns the number cleared."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(MessageAttachment.id, MessageAttachment.local_path, MessageAttachment.kind, MessageAttachment.size)
            .where(MessageAttachment.local_path.isnot(None))
        )).all()

    def _scan() -> list:
        bad = []
        for att_id, rel, kind, size in rows:
            path = DATA_DIR / rel
            expected = size if kind != "photo" else None
            if not file_is_complete(path, kind, expected):
                bad.append((att_id, path))
        return bad

    bad = await asyncio.to_thread(_scan)
    if not bad:
        return 0
    async with AsyncSessionLocal() as db:
        for att_id, path in bad:
            try:
                path.unlink()
            except OSError:
                pass
            await db.execute(update(MessageAttachment).where(MessageAttachment.id == att_id).values(local_path=None))
        await db.commit()
    logger.info("Cleared %d truncated/missing media files", len(bad))
    return len(bad)
