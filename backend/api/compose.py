import os
import tempfile
import time
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.jobs import await_single
from backend.telegram.runtime import runtime
from backend.telegram.convert import GENERAL_TOPIC_ID, message_to_row

router = APIRouter()

# Completed idempotent sends: client_nonce → (expires_at, response)
_NONCE_TTL = 600
_completed: dict[str, tuple[float, dict]] = {}


def _cached(nonce: Optional[str]) -> Optional[dict]:
    now = time.monotonic()
    for k in [k for k, (exp, _) in _completed.items() if exp < now]:
        _completed.pop(k, None)
    hit = _completed.get(nonce) if nonce else None
    return hit[1] if hit else None


async def _idempotent(nonce: Optional[str], factory) -> dict:
    """Run ``factory`` once per client nonce: a retry while the first attempt is
    in flight joins it, a retry after success gets the same response."""
    if not nonce:
        return await factory()
    hit = _cached(nonce)
    if hit is not None:
        return hit
    result = await await_single(f"compose:{nonce}", factory)
    _completed[nonce] = (time.monotonic() + _NONCE_TTL, result)
    return result


async def _require_client():
    client = await runtime.client()
    if not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authenticated")
    return client


def _reply_target(reply_to_id: Optional[int], topic_id: Optional[int]) -> Optional[int]:
    """Messages in a forum topic are sent as replies to the topic's root
    message; General (topic 1) takes no reply header."""
    if reply_to_id:
        return reply_to_id
    if topic_id and topic_id != GENERAL_TOPIC_ID:
        return topic_id
    return None


async def _persist_sent(chat_id: int, msgs: list, reply_to_id: Optional[int], topic_id: Optional[int]) -> list[dict]:
    from backend.api.chats import _serialize_msg

    async with AsyncSessionLocal() as db:
        chat = await crud.get_chat_by_telegram_id(db, chat_id)
        forum = bool(chat and chat.is_forum)
        rows = []
        for i, msg in enumerate(msgs):
            row = message_to_row(msg, chat_id, forum=forum)
            if topic_id is not None:
                row["topic_id"] = topic_id
            if i == 0 and reply_to_id:
                row["reply_to_tg_message_id"] = reply_to_id
            rows.append(row)
        out = []
        for row in rows:
            saved = await crud.save_message(db, **row)
            out.append(_serialize_msg((saved, None, None, None)))
    return out


class SendRequest(BaseModel):
    chat_id: int
    text: str
    reply_to_id: Optional[int] = None
    topic_id: Optional[int] = None
    client_nonce: Optional[str] = None


@router.post("/send")
async def send_message(req: SendRequest):
    client = await _require_client()

    async def _send() -> dict:
        try:
            msg = await client.send_message(
                req.chat_id,
                req.text,
                reply_to=_reply_target(req.reply_to_id, req.topic_id),
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        saved = await _persist_sent(req.chat_id, [msg], req.reply_to_id, req.topic_id)
        return {"success": True, "message_id": msg.id, "message": saved[0]}

    return await _idempotent(req.client_nonce, _send)


class EditRequest(BaseModel):
    chat_id: int
    message_id: int
    text: str


@router.post("/edit")
async def edit_message(req: EditRequest):
    """Edit an outgoing message via Telegram and store the result."""
    client = await _require_client()
    try:
        msg = await client.edit_message(req.chat_id, req.message_id, req.text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    saved = await _persist_sent(req.chat_id, [msg], None, None)
    return {"success": True, "message": saved[0]}


class DeleteRequest(BaseModel):
    chat_id: int
    message_ids: List[int]
    revoke: bool = False


@router.post("/delete")
async def delete_messages(req: DeleteRequest):
    """Delete messages. If revoke=True, delete for everyone."""
    client = await _require_client()
    try:
        await client.delete_messages(req.chat_id, req.message_ids, revoke=req.revoke)
        async with AsyncSessionLocal() as db:
            await crud.delete_messages_by_telegram_ids(db, req.chat_id, req.message_ids)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class ForwardRequest(BaseModel):
    from_chat_id: int
    message_ids: List[int]
    to_chat_id: int


@router.post("/forward")
async def forward_messages(req: ForwardRequest):
    """Forward messages from one chat to another."""
    client = await _require_client()
    try:
        await client.forward_messages(req.to_chat_id, req.message_ids, req.from_chat_id)
        return {"success": True}
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
    topic_id: Optional[int] = Form(None),
    client_nonce: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
):
    """Send files as albums: photos/videos grouped together, documents grouped
    together (Telegram can't mix them), at most 10 per album. The caption goes
    on the first item that is sent.

    Response ``files`` reports each file as "sent" (with message_id) or
    "failed" (with error). Status 200 = all sent, 207 = partial, 400 = none
    sent. ``client_nonce``: a repeat with the same nonce returns the first
    response and never re-sends; to retry the failed files, send just those
    with a new nonce."""
    client = await _require_client()
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    hit = _cached(client_nonce)
    if hit is not None:
        return _files_response(hit)

    temp_paths: list[str] = []
    started = False
    try:
        entries = []
        for upload in files:
            name = upload.filename or "file"
            suffix = os.path.splitext(name)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(await upload.read())
                temp_paths.append(tmp.name)
            entries.append({
                "filename": name,
                "path": tmp.name,
                "document": _detect_media_type(name) == "document",
                "status": "pending",
            })

        async def _send() -> dict:
            nonlocal started
            started = True
            try:
                return await _send_entries(client, chat_id, text, reply_to_id, topic_id, entries)
            finally:
                _cleanup(temp_paths)

        return _files_response(await _idempotent(client_nonce, _send))
    finally:
        # Once the send started, it owns the temp files (it may outlive this
        # request if the client disconnects); otherwise clean up here.
        if not started:
            _cleanup(temp_paths)


ALBUM_MAX = 10


async def _send_entries(client, chat_id, text, reply_to_id, topic_id, entries) -> dict:
    sent_msgs = []
    reply_to = _reply_target(reply_to_id, topic_id)
    caption_pending = bool(text)
    for as_doc in (False, True):
        group = [e for e in entries if e["document"] == as_doc]
        # One album per request so a failure maps to exactly these files.
        for i in range(0, len(group), ALBUM_MAX):
            chunk = group[i:i + ALBUM_MAX]
            captions = [text if caption_pending and j == 0 else "" for j in range(len(chunk))]
            try:
                result = await client.send_file(
                    chat_id,
                    [e["path"] for e in chunk] if len(chunk) > 1 else chunk[0]["path"],
                    caption=captions if len(chunk) > 1 else captions[0],
                    reply_to=reply_to,
                    force_document=as_doc,
                )
            except Exception as e:
                for entry in chunk:
                    entry.update(status="failed", error=str(e))
                continue
            msgs = result if isinstance(result, list) else [result]
            for entry, msg in zip(chunk, msgs):
                entry.update(status="sent", message_id=msg.id)
            sent_msgs.extend(msgs)
            caption_pending = False

    report = [
        {k: e[k] for k in ("filename", "status", "message_id", "error") if k in e}
        for e in entries
    ]
    if not sent_msgs:
        raise HTTPException(status_code=400, detail={
            "message": f"No files were sent: {entries[0].get('error')}",
            "code": "send_failed",
            "files": report,
        })
    saved = await _persist_sent(chat_id, sent_msgs, reply_to_id, topic_id)
    all_sent = all(e["status"] == "sent" for e in entries)
    return {
        "success": all_sent,
        "partial": not all_sent,
        "message_ids": [m.id for m in sent_msgs],
        "messages": saved,
        "files": report,
    }


def _files_response(body: dict):
    from fastapi.responses import JSONResponse
    return JSONResponse(body, status_code=207 if body.get("partial") else 200)


def _cleanup(paths: list[str]) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass
