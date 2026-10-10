"""Sending, editing, deleting and forwarding messages (/api/compose).
Requests carry internal conversation / message ids; the conversation's
connector does the channel work (409 ``unsupported_channel`` when it can't)."""

import os
import tempfile
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.api.idempotency import cached_response, cleanup_temp_files, idempotent
from backend.api.lookups import channel_call, connector_with, conversation_or_404, message_or_404, unsupported_channel
from backend.connectors.base import Capability, Connector, OutgoingFile, OutgoingMessage, SendFailed
from backend.connectors.roles import MessageActions
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation
from backend.serializers import messages_out

router = APIRouter()


async def _same_conversation(conversation_id: int, message_id: int):
    async with AsyncSessionLocal() as db:
        msg = await message_or_404(db, message_id)
    if msg.conversation_id != conversation_id:
        raise HTTPException(status_code=400, detail="The replied-to message is in another conversation")
    return msg


async def _stored_messages(message_ids: list[int]) -> list[dict]:
    """Stored messages as every read returns them (reply preview, participants)."""
    async with AsyncSessionLocal() as db:
        rows = await crud.get_reply_previews(db, message_ids)
        by_id = {r[0].id: r for r in rows}
        return await messages_out(db, [by_id[i] for i in message_ids if i in by_id])


def _sender(conv: Conversation) -> Connector:
    return connector_with(conv, Connector, "Sending", Capability.SEND)


async def _send(conv: Conversation, draft: OutgoingMessage) -> dict:
    message_ids = await channel_call(conv, _sender(conv).send(conv, draft))
    messages = await _stored_messages(message_ids)
    return {"success": True, "message_id": messages[0]["id"] if messages else None, "message": messages[0] if messages else None,
            "message_ids": [m["id"] for m in messages], "messages": messages}


class SendRequest(BaseModel):
    conversation_id: int
    text: str
    reply_to_message_id: Optional[int] = None
    topic_id: Optional[int] = None
    client_nonce: Optional[str] = None
    subject: Optional[str] = None  # email
    to: List[str] = []  # email: complete To list (empty = reply to the sender)
    cc: List[str] = []


@router.post("/send")
async def send_message(req: SendRequest):
    async with AsyncSessionLocal() as db:
        conv = await conversation_or_404(db, req.conversation_id)
    _sender(conv)
    if req.reply_to_message_id is not None:
        await _same_conversation(conv.id, req.reply_to_message_id)
    draft = OutgoingMessage(
        text=req.text, reply_to_message_id=req.reply_to_message_id, topic_id=req.topic_id,
        subject=req.subject, to=req.to, cc=req.cc,
    )
    return await idempotent(req.client_nonce, lambda: _send(conv, draft))


class EditRequest(BaseModel):
    message_id: int
    text: str


@router.post("/edit")
async def edit_message(req: EditRequest):
    """Edit an outgoing message on its channel and store the result."""
    async with AsyncSessionLocal() as db:
        stored = await message_or_404(db, req.message_id)
        conv = await conversation_or_404(db, stored.conversation_id)
    connector = connector_with(conv, MessageActions, "Editing", Capability.EDIT)
    message_id = await channel_call(conv, connector.edit_message(conv, stored, req.text))
    return {"success": True, "message": (await _stored_messages([message_id]))[0]}


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
    async with AsyncSessionLocal() as db:
        conv = await conversation_or_404(db, req.conversation_id)
    connector = connector_with(conv, MessageActions, "Deleting messages", Capability.DELETE)
    msgs = await _messages_in(conv.id, req.message_ids)
    await channel_call(conv, connector.delete_messages(conv, msgs, revoke=req.revoke))
    async with AsyncSessionLocal() as db:
        await crud.delete_messages(db, [m.id for m in msgs])
    return {"success": True}


class ForwardRequest(BaseModel):
    from_conversation_id: int
    message_ids: List[int]
    to_conversation_id: int


@router.post("/forward")
async def forward_messages(req: ForwardRequest):
    """Forward messages from one conversation to another (same account)."""
    async with AsyncSessionLocal() as db:
        source = await conversation_or_404(db, req.from_conversation_id)
        target = await conversation_or_404(db, req.to_conversation_id)
    connector = connector_with(source, MessageActions, "Forwarding", Capability.FORWARD)
    if connector_with(target, MessageActions, "Forwarding", Capability.FORWARD) is not connector:
        raise unsupported_channel(target, "Forwarding")
    msgs = await _messages_in(source.id, req.message_ids)
    await channel_call(source, connector.forward_messages(source, msgs, target))
    return {"success": True}


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
    """Send files with an optional caption. How they're grouped is the
    channel's business (Telegram: albums of at most 10, media and documents
    apart; email: one message).

    Response ``files`` reports each file as "sent" (with message_id) or
    "failed" (with error). Status 200 = all sent, 207 = partial, 400 = none
    sent. ``client_nonce``: a repeat with the same nonce returns the first
    response and never re-sends; to retry the failed files, send just those
    with a new nonce."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    async with AsyncSessionLocal() as db:
        conv = await conversation_or_404(db, conversation_id)
    connector = _sender(conv)

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
            text=text, reply_to_message_id=reply_to_message_id, topic_id=topic_id, files=outgoing,
            subject=subject, to=list(to), cc=list(cc),
        )

        async def _send_all() -> dict:
            nonlocal started
            started = True
            try:
                sent = await channel_call(conv, connector.send_files(conv, draft), passthrough=(SendFailed,))
            except SendFailed as e:
                raise HTTPException(status_code=400, detail={
                    "message": str(e), "code": "send_failed", "files": e.files,
                })
            finally:
                cleanup_temp_files(temp_paths)
            messages = await _stored_messages(sent.message_ids)
            return {
                "success": sent.all_sent,
                "partial": not sent.all_sent,
                "message_ids": [m["id"] for m in messages],
                "messages": messages,
                "files": sent.files,
            }

        return _files_response(await idempotent(client_nonce, _send_all))
    finally:
        # Once the send started, it owns the temp files (it may outlive this
        # request if the client disconnects); otherwise clean up here.
        if not started:
            cleanup_temp_files(temp_paths)


def _files_response(body: dict):
    return JSONResponse(body, status_code=207 if body.get("partial") else 200)
