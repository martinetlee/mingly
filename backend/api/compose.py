import os
import tempfile
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.telegram.client import get_client
from backend.db.database import AsyncSessionLocal
from backend.db import crud

router = APIRouter()


class SendRequest(BaseModel):
    chat_id: int
    text: str
    reply_to_id: Optional[int] = None


@router.post("/send")
async def send_message(req: SendRequest):
    client = get_client()
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        msg = await client.send_message(
            req.chat_id,
            req.text,
            reply_to=req.reply_to_id,
        )
        async with AsyncSessionLocal() as db:
            await crud.save_message(
                db,
                telegram_id=msg.id,
                chat_id=req.chat_id,
                sender_id=msg.sender_id,
                sender_name=None,
                text=msg.text or req.text,
                date=msg.date.replace(tzinfo=None),
                is_outgoing=True,
                media_type=None,
            )
        return {"success": True, "message_id": msg.id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def _detect_media_type(filename: str) -> str:
    """Detect media type from filename extension."""
    ext = os.path.splitext(filename)[1].lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}
    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv"}
    if ext in image_exts:
        return "photo"
    if ext in video_exts:
        return "video"
    return "document"


@router.post("/send-files")
async def send_files(
    chat_id: int = Form(...),
    text: str = Form(""),
    reply_to_id: Optional[int] = Form(None),
    files: List[UploadFile] = File(...),
):
    """Send one or more files (photos, videos, documents) to a chat.

    Accepts multipart form data with file uploads. Text caption is optional.
    """
    client = get_client()
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    sent_message_ids: list[int] = []
    temp_paths: list[str] = []

    try:
        for i, upload in enumerate(files):
            # Write uploaded file to a temp location
            suffix = os.path.splitext(upload.filename or "file")[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                content = await upload.read()
                tmp.write(content)
                temp_paths.append(tmp.name)

            # Only attach caption to the first file
            caption = text if i == 0 and text else None

            msg = await client.send_file(
                chat_id,
                tmp.name,
                caption=caption,
                reply_to=reply_to_id if i == 0 else None,
                force_document=(
                    _detect_media_type(upload.filename or "file") == "document"
                ),
            )

            media_type = _detect_media_type(upload.filename or "file")

            async with AsyncSessionLocal() as db:
                await crud.save_message(
                    db,
                    telegram_id=msg.id,
                    chat_id=chat_id,
                    sender_id=msg.sender_id,
                    sender_name=None,
                    text=caption or "",
                    date=msg.date.replace(tzinfo=None),
                    is_outgoing=True,
                    media_type=media_type,
                )

            sent_message_ids.append(msg.id)

        return {"success": True, "message_ids": sent_message_ids}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        # Clean up temp files
        for path in temp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
