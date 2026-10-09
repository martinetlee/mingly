"""On-demand attachment download (/api/media-download).

Fetches an attachment from its channel when the frontend asks for it
(photos not yet fetched by sync, videos, voice/audio, documents, GIFs).
Downloads are atomic and single-flight per attachment.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from backend.config import DATA_DIR
from backend.db.database import AsyncSessionLocal
from backend.db.models import MessageAttachment
from backend.serializers import media_url
from backend.telegram.media import (
    DOWNLOADABLE_TYPES, MediaNotFound, MediaUnavailable, download_attachment, file_is_complete,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _download(att: MessageAttachment) -> str:
    """Telegram downloads through its runtime; other channels through their
    connector."""
    from backend.connectors.base import UnsupportedOperation
    from backend.connectors.manager import manager
    from backend.db import crud
    from backend.serializers import channel_of

    async with AsyncSessionLocal() as db:
        msg = await crud.get_message(db, att.message_id)
        conv = await crud.get_conversation(db, msg.conversation_id)
    channel = channel_of(conv)
    if channel == "telegram":
        return await download_attachment(att.id)
    connector = manager.for_account(channel, conv.account_id)
    if connector is None:
        raise MediaUnavailable(f"{channel} is not connected")
    try:
        return await connector.fetch_attachment(att, msg, conv)
    except UnsupportedOperation as e:
        raise MediaNotFound(str(e))


class MediaDownloadResponse(BaseModel):
    url: str
    kind: str


@router.post("/{attachment_id}")
async def download_media(attachment_id: int) -> MediaDownloadResponse:
    """Download an attachment and return its serving URL."""
    async with AsyncSessionLocal() as db:
        att = (await db.execute(
            select(MessageAttachment).where(MessageAttachment.id == attachment_id)
        )).scalar_one_or_none()
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if att.kind not in DOWNLOADABLE_TYPES:
        raise HTTPException(status_code=400, detail=f"Attachment kind '{att.kind}' is not downloadable")

    expected = att.size if att.kind != "photo" else None
    if att.local_path and file_is_complete(DATA_DIR / att.local_path, att.kind, expected):
        return MediaDownloadResponse(url=media_url(att.local_path), kind=att.kind)

    try:
        rel = await _download(att)
    except MediaUnavailable:
        raise HTTPException(status_code=503, detail="Channel not connected")
    except MediaNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        logger.exception("Failed to download attachment %d", attachment_id)
        raise HTTPException(status_code=500, detail="Failed to download media")
    return MediaDownloadResponse(url=media_url(rel), kind=att.kind)
