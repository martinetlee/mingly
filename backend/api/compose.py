"""Sending, editing, deleting and forwarding messages (/api/compose).
Requests carry internal conversation / message ids; the channel's own ids
are looked up from the stored rows."""

import os
import tempfile
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.api.lookups import conversation_or_404, message_or_404, telegram_peer, unsupported_channel
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation
from backend.api.idempotency import cached_response, cleanup_temp_files, idempotent
from backend.serializers import messages_out
from backend.telegram import ids
from backend.telegram.runtime import runtime
from backend.telegram.convert import GENERAL_TOPIC_ID, rows_for

router = APIRouter()


async def _require_client():
    client = await runtime.client()
    if not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authenticated")
    return client


def _reply_target(reply_to_seq: Optional[int], topic_id: Optional[int]) -> Optional[int]:
    """Telegram reply header: the replied-to message, else (in a forum topic)
    the topic's root message; General (topic 1) takes no reply header."""
    if reply_to_seq:
        return reply_to_seq
    if topic_id and topic_id != GENERAL_TOPIC_ID:
        return topic_id
    return None


async def _target(conversation_id: int, what: str) -> tuple[Conversation, int]:
    """(conversation, Telegram peer) for a send-like operation."""
    async with AsyncSessionLocal() as db:
        conv = await conversation_or_404(db, conversation_id)
    return conv, telegram_peer(conv, what)


async def _same_conversation(conversation_id: int, message_id: int):
    async with AsyncSessionLocal() as db:
        msg = await message_or_404(db, message_id)
    if msg.conversation_id != conversation_id:
        raise HTTPException(status_code=400, detail="The replied-to message is in another conversation")
    return msg


async def _reply_seq(conversation_id: int, reply_to_message_id: Optional[int]) -> Optional[int]:
    if reply_to_message_id is None:
        return None
    return ids.tg_message_id_of(await _same_conversation(conversation_id, reply_to_message_id))


async def _persist_sent(conv: Conversation, msgs: list, reply_to_seq: Optional[int], topic_id: Optional[int]) -> list[dict]:
    """Store sent Telegram messages; returns their Message payloads in order
    (with reply_to_message_id / reply_to_preview, like every other read)."""
    async with AsyncSessionLocal() as db:
        rows = await rows_for(db, conv.id, msgs, forum=conv.is_forum)
        saved_ids = []
        for i, row in enumerate(rows):
            if topic_id is not None:
                row["topic_id"] = topic_id
            if i == 0 and reply_to_seq:
                row["reply_to_external_id"] = ids.message_key(reply_to_seq)
            saved_ids.append((await crud.save_message(db, **row)).id)
        by_id = {r[0].id: r for r in await crud.get_reply_previews(db, saved_ids)}
        return await messages_out(db, [by_id[i] for i in saved_ids])


class SendRequest(BaseModel):
    conversation_id: int
    text: str
    reply_to_message_id: Optional[int] = None
    topic_id: Optional[int] = None
    client_nonce: Optional[str] = None
    subject: Optional[str] = None  # email
    to: List[str] = []  # email: complete To list (empty = reply to the sender)
    cc: List[str] = []


def _connector_for(conv: Conversation):
    """The connector of a non-Telegram conversation (409 when none is
    connected or it can't send)."""
    from backend.connectors.base import Capability
    from backend.connectors.manager import manager
    from backend.serializers import channel_of

    connector = manager.for_account(channel_of(conv), conv.account_id)
    if connector is None or Capability.SEND not in connector.conversation_capabilities(conv):
        raise unsupported_channel(conv, "Sending")
    return connector


async def _send_via_connector(conv: Conversation, draft) -> dict:
    from backend.connectors.base import UnsupportedOperation

    connector = _connector_for(conv)
    try:
        ids_ = await connector.send(conv, draft)
    except UnsupportedOperation as e:
        raise unsupported_channel(conv, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    from backend.serializers import messages_out

    async with AsyncSessionLocal() as db:
        rows = await crud.get_reply_previews(db, ids_)
        messages = await messages_out(db, list(rows))
    return {"success": True, "message_id": messages[0]["id"] if messages else None, "message": messages[0] if messages else None,
            "message_ids": [m["id"] for m in messages], "messages": messages}


@router.post("/send")
async def send_message(req: SendRequest):
    async with AsyncSessionLocal() as db:
        conv = await conversation_or_404(db, req.conversation_id)
    if not ids.is_telegram(conv):
        from backend.connectors.base import OutgoingMessage
        if req.reply_to_message_id is not None:
            await _same_conversation(conv.id, req.reply_to_message_id)
        draft = OutgoingMessage(
            text=req.text, reply_to_message_id=req.reply_to_message_id, subject=req.subject, to=req.to, cc=req.cc,
        )
        return await idempotent(req.client_nonce, lambda: _send_via_connector(conv, draft))

    conv, peer = await _target(req.conversation_id, "Sending")
    reply_seq = await _reply_seq(conv.id, req.reply_to_message_id)
    client = await _require_client()

    async def _send() -> dict:
        try:
            msg = await client.send_message(peer, req.text, reply_to=_reply_target(reply_seq, req.topic_id))
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        saved = await _persist_sent(conv, [msg], reply_seq, req.topic_id)
        return {"success": True, "message_id": saved[0]["id"], "message": saved[0]}

    return await idempotent(req.client_nonce, _send)


class EditRequest(BaseModel):
    message_id: int
    text: str


@router.post("/edit")
async def edit_message(req: EditRequest):
    """Edit an outgoing message on its channel and store the result."""
    async with AsyncSessionLocal() as db:
        stored = await message_or_404(db, req.message_id)
    conv, peer = await _target(stored.conversation_id, "Editing")
    client = await _require_client()
    try:
        msg = await client.edit_message(peer, ids.tg_message_id_of(stored), req.text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    saved = await _persist_sent(conv, [msg], None, None)
    return {"success": True, "message": saved[0]}


class DeleteRequest(BaseModel):
    conversation_id: int
    message_ids: List[int]
    revoke: bool = False


async def _messages_in(conversation_id: int, message_ids: List[int]) -> list:
    async with AsyncSessionLocal() as db:
        found = await crud.get_messages_by_ids(db, message_ids)
    msgs = [found[mid] for mid in message_ids if mid in found]
    if len(msgs) != len(set(message_ids)) or any(m.conversation_id != conversation_id for m in msgs):
        raise HTTPException(status_code=404, detail="Message not found in this conversation")
    return msgs


@router.post("/delete")
async def delete_messages(req: DeleteRequest):
    """Delete messages. If revoke=True, delete for everyone."""
    conv, peer = await _target(req.conversation_id, "Deleting messages")
    msgs = await _messages_in(conv.id, req.message_ids)
    client = await _require_client()
    try:
        await client.delete_messages(peer, [ids.tg_message_id_of(m) for m in msgs], revoke=req.revoke)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    async with AsyncSessionLocal() as db:
        await crud.delete_messages(db, [m.id for m in msgs])
    return {"success": True}


class ForwardRequest(BaseModel):
    from_conversation_id: int
    message_ids: List[int]
    to_conversation_id: int


@router.post("/forward")
async def forward_messages(req: ForwardRequest):
    """Forward messages from one conversation to another (same channel)."""
    _, from_peer = await _target(req.from_conversation_id, "Forwarding")
    _, to_peer = await _target(req.to_conversation_id, "Forwarding")
    msgs = await _messages_in(req.from_conversation_id, req.message_ids)
    client = await _require_client()
    try:
        await client.forward_messages(to_peer, [ids.tg_message_id_of(m) for m in msgs], from_peer)
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
    conversation_id: int = Form(...),
    text: str = Form(""),
    reply_to_message_id: Optional[int] = Form(None),
    topic_id: Optional[int] = Form(None),
    client_nonce: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    subject: Optional[str] = Form(None),  # email
    to: List[str] = Form([]),
    cc: List[str] = Form([]),
):
    """Send files as albums: photos/videos grouped together, documents grouped
    together (Telegram can't mix them), at most 10 per album. The caption goes
    on the first item that is sent.

    Response ``files`` reports each file as "sent" (with message_id) or
    "failed" (with error). Status 200 = all sent, 207 = partial, 400 = none
    sent. ``client_nonce``: a repeat with the same nonce returns the first
    response and never re-sends; to retry the failed files, send just those
    with a new nonce."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    async with AsyncSessionLocal() as db:
        target = await conversation_or_404(db, conversation_id)
    if not ids.is_telegram(target):
        return await _send_files_via_connector(
            target, text, reply_to_message_id, client_nonce, files, subject=subject, to=to, cc=cc,
        )
    conv, peer = await _target(conversation_id, "Sending files")
    reply_seq = await _reply_seq(conv.id, reply_to_message_id)
    client = await _require_client()

    hit = cached_response(client_nonce)
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
                return await _send_entries(client, conv, peer, text, reply_seq, topic_id, entries)
            finally:
                cleanup_temp_files(temp_paths)

        return _files_response(await idempotent(client_nonce, _send))
    finally:
        # Once the send started, it owns the temp files (it may outlive this
        # request if the client disconnects); otherwise clean up here.
        if not started:
            cleanup_temp_files(temp_paths)


async def _send_files_via_connector(conv, text, reply_to_message_id, client_nonce, files, *, subject=None, to=(), cc=()):
    """Other channels send all files with one message (e.g. one email)."""
    from backend.connectors.base import OutgoingFile, OutgoingMessage

    hit = cached_response(client_nonce)
    if hit is not None:
        return _files_response(hit)
    if reply_to_message_id is not None:
        await _same_conversation(conv.id, reply_to_message_id)
    temp_paths: list[str] = []
    started = False
    try:
        outgoing = []
        for upload in files:
            name = upload.filename or "file"
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(name)[1]) as tmp:
                tmp.write(await upload.read())
                temp_paths.append(tmp.name)
            outgoing.append(OutgoingFile(path=tmp.name, filename=name, mime_type=upload.content_type))
        draft = OutgoingMessage(
            text=text, reply_to_message_id=reply_to_message_id, files=outgoing,
            subject=subject, to=list(to), cc=list(cc),
        )

        async def _send() -> dict:
            nonlocal started
            started = True
            try:
                body = await _send_via_connector(conv, draft)
            finally:
                cleanup_temp_files(temp_paths)
            message_id = body["message_id"]
            return {
                "success": True, "partial": False, "message_ids": body["message_ids"], "messages": body["messages"],
                "files": [{"filename": f.filename, "status": "sent", "message_id": message_id} for f in outgoing],
            }

        return _files_response(await idempotent(client_nonce, _send))
    finally:
        # Once the send started, it owns the temp files (it may outlive this
        # request if the client disconnects); otherwise clean up here.
        if not started:
            cleanup_temp_files(temp_paths)


ALBUM_MAX = 10


async def _send_entries(client, conv, peer, text, reply_seq, topic_id, entries) -> dict:
    sent_msgs = []
    reply_to = _reply_target(reply_seq, topic_id)
    caption_pending = bool(text)
    for as_doc in (False, True):
        group = [e for e in entries if e["document"] == as_doc]
        # One album per request so a failure maps to exactly these files.
        for i in range(0, len(group), ALBUM_MAX):
            chunk = group[i:i + ALBUM_MAX]
            captions = [text if caption_pending and j == 0 else "" for j in range(len(chunk))]
            try:
                result = await client.send_file(
                    peer,
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
                entry.update(status="sent", tg_id=msg.id)
            sent_msgs.extend(msgs)
            caption_pending = False

    def _report() -> list[dict]:
        return [{k: e[k] for k in ("filename", "status", "message_id", "error") if k in e} for e in entries]

    if not sent_msgs:
        raise HTTPException(status_code=400, detail={
            "message": f"No files were sent: {entries[0].get('error')}",
            "code": "send_failed",
            "files": _report(),
        })
    saved = await _persist_sent(conv, sent_msgs, reply_seq, topic_id)
    by_tg = {int(m["external_id"]): m["id"] for m in saved}
    for entry in entries:
        if "tg_id" in entry:
            entry["message_id"] = by_tg.get(entry.pop("tg_id"))
    all_sent = all(e["status"] == "sent" for e in entries)
    return {
        "success": all_sent,
        "partial": not all_sent,
        "message_ids": [m["id"] for m in saved],
        "messages": saved,
        "files": _report(),
    }


def _files_response(body: dict):
    from fastapi.responses import JSONResponse
    return JSONResponse(body, status_code=207 if body.get("partial") else 200)


