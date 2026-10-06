"""On-demand media download endpoint.

Downloads a message's media from Telegram when the frontend asks for it
(photos not yet fetched by sync, videos, voice/audio, documents, GIFs).
Downloads are atomic and single-flight per message (backend.telegram.media).
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from backend.config import DATA_DIR
from backend.db.database import AsyncSessionLocal
from backend.db.models import Message
from backend.telegram.media import (
    DOWNLOADABLE_TYPES, MediaNotFound, MediaUnavailable, download_message_media, file_is_complete,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class MediaDownloadResponse(BaseModel):
    media_url: str
    media_type: str


@router.post("/{chat_telegram_id}/{msg_telegram_id}")
async def download_media(chat_telegram_id: int, msg_telegram_id: int) -> MediaDownloadResponse:
    """Download media for a specific message and return its serving URL."""
    async with AsyncSessionLocal() as db:
        msg_row = (await db.execute(
            select(Message).where(
                Message.tg_message_id == msg_telegram_id,
                Message.chat_id == chat_telegram_id,
            ).limit(1)
        )).scalars().first()
    if not msg_row:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg_row.media_type not in DOWNLOADABLE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Media type '{msg_row.media_type}' is not downloadable",
        )

    expected = msg_row.media_size if msg_row.media_type != "photo" else None
    if msg_row.media_path and file_is_complete(DATA_DIR / msg_row.media_path, msg_row.media_type, expected):
        return MediaDownloadResponse(media_url=f"/api/media/{msg_row.media_path}", media_type=msg_row.media_type)

    try:
        rel = await download_message_media(
            chat_telegram_id,
            msg_telegram_id,
            media_type=msg_row.media_type,
            media_filename=msg_row.media_filename,
        )
    except MediaUnavailable:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    except MediaNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        logger.exception("Failed to download media for msg %d in chat %d", msg_telegram_id, chat_telegram_id)
        raise HTTPException(status_code=500, detail="Failed to download media")
    return MediaDownloadResponse(media_url=f"/api/media/{rel}", media_type=msg_row.media_type)
