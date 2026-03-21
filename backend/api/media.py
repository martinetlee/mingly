"""On-demand media download endpoint.

Downloads media from Telegram when requested by the frontend (lazy
download). Photos are normally downloaded during sync but may be missing
for messages synced before that feature existed.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from backend.config import DATA_DIR
from backend.db.database import AsyncSessionLocal
from backend.db.models import Message
from backend.telegram.client import get_client, ensure_connected

logger = logging.getLogger(__name__)

router = APIRouter()

MEDIA_DIR = DATA_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)

# File extensions per media type
_EXTENSIONS = {
    "photo": ".jpg",
    "video": ".mp4",
    "video_note": ".mp4",
    "voice": ".ogg",
    "audio": ".mp3",
}

DOWNLOADABLE_TYPES = frozenset(_EXTENSIONS.keys())


class MediaDownloadResponse(BaseModel):
    media_url: str
    media_type: str


@router.post("/{chat_telegram_id}/{msg_telegram_id}")
async def download_media(chat_telegram_id: int, msg_telegram_id: int) -> MediaDownloadResponse:
    """Download media for a specific message on demand.

    Fetches the original Telegram message, downloads the media file to disk,
    updates the database with the media path, and returns the serving URL.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Message).where(
                Message.telegram_id == msg_telegram_id,
                Message.chat_id == chat_telegram_id,
            ).limit(1)
        )
        msg_row = result.scalars().first()
        if not msg_row:
            raise HTTPException(status_code=404, detail="Message not found")

        if msg_row.media_type not in DOWNLOADABLE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Media type '{msg_row.media_type}' is not downloadable",
            )

        # If already downloaded, return existing path
        if msg_row.media_path:
            return MediaDownloadResponse(
                media_url=f"/api/media/{msg_row.media_path}",
                media_type=msg_row.media_type,
            )

        # Download from Telegram
        client = get_client()
        if not client:
            raise HTTPException(status_code=503, detail="Telegram client not connected")

        await ensure_connected()

        try:
            # Fetch the original message from Telegram
            tg_msg = await client.get_messages(chat_telegram_id, ids=msg_telegram_id)
            if not tg_msg or not tg_msg.media:
                raise HTTPException(status_code=404, detail="Media not found on Telegram")

            ext = _EXTENSIONS.get(msg_row.media_type, "")
            chat_dir = MEDIA_DIR / str(chat_telegram_id)
            chat_dir.mkdir(exist_ok=True)
            file_path = chat_dir / f"{msg_telegram_id}{ext}"

            if not file_path.exists():
                await client.download_media(tg_msg, file=str(file_path))

            relative_path = str(file_path.relative_to(DATA_DIR))

            # Update the database with the downloaded path
            msg_row.media_path = relative_path
            await db.commit()

            return MediaDownloadResponse(
                media_url=f"/api/media/{relative_path}",
                media_type=msg_row.media_type,
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to download media for msg %d in chat %d: %s",
                         msg_telegram_id, chat_telegram_id, e)
            raise HTTPException(status_code=500, detail="Failed to download media")
