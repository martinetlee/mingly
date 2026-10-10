"""Gmail HTTP routes (/api/connectors/gmail): OAuth callback and the Mail space.

The router is mounted WITHOUT the session token because Google's OAuth
redirect arrives from the browser without it (``/oauth/callback``,
authenticated by the flow's state parameter). Every other route here is for
the Mail space and declares the session dependency itself.
"""

import html
import os
import tempfile
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from backend.errors import DomainError
from backend.api.dependencies import require_session_token
from backend.api.idempotency import cleanup_temp_files, idempotent
from backend.api.lookups import conversation_or_404
from backend.connectors.base import OutgoingFile, OutgoingMessage
from backend.connectors.gmail import oauth
from backend.connectors.manager import manager
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.serializers import channel_of, messages_out
from backend.api.views import conversation_json
from backend.ws.manager import ws_manager

router = APIRouter()
_protected = [Depends(require_session_token)]

FOLDERS = ("inbox", "sent", "archive", "all")

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Mingly</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:-apple-system,system-ui,sans-serif;display:flex;align-items:center;justify-content:center;
min-height:90vh;margin:0 16px;color:#222;background:#fafafa}}div{{max-width:420px;text-align:center}}
h1{{font-size:20px}}p{{color:#555;line-height:1.5}}</style></head>
<body><div><h1>{title}</h1><p>{message}</p></div></body></html>"""


def _page(title: str, message: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        _PAGE.format(title=html.escape(title), message=html.escape(message)),
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(state: Optional[str] = None, code: Optional[str] = None, error: Optional[str] = None):
    pending = await oauth.complete(state, code, error)
    if pending is None:
        return _page("Sign-in link expired", "Start connecting Gmail again from Mingly.", status=400)
    if pending.error:
        return _page("Gmail was not connected", pending.error, status=400)
    return _page("Gmail connected", "You can close this tab and return to Mingly.")


# -- Mail space (session-protected) ------------------------------------------------

def _connector(account_id: Optional[int]):

    connector = manager.for_account("gmail", account_id)
    if connector is None:
        raise HTTPException(status_code=409, detail="This Gmail account is not connected")
    return connector


async def _thread(conversation_id: int):
    """The email-thread conversation and the connector of its account."""

    async with AsyncSessionLocal() as db:
        conv = await conversation_or_404(db, conversation_id)
    if channel_of(conv) != "gmail":
        raise HTTPException(status_code=409, detail="Not a Gmail conversation")
    return conv, _connector(conv.account_id)


async def _thread_summaries(db, conversation_ids: list) -> dict:
    """``{conversation_id: {senders: [{name, person_id, is_me}], message_count, has_attachments}}``
    — senders in order of their first message, like a mail client's thread row."""

    out: dict = {cid: {"senders": [], "message_count": 0, "has_attachments": False} for cid in conversation_ids}
    if not conversation_ids:
        return out
    for cid, name, outgoing, person_id, email in await crud.message_senders(db, conversation_ids):
        entry = out[cid]
        entry["message_count"] += 1
        key = "me" if outgoing else (email or name)
        if all(s["key"] != key for s in entry["senders"]):
            entry["senders"].append({"key": key, "name": "me" if outgoing else (name or email), "person_id": person_id,
                                     "is_me": bool(outgoing)})
    for cid in await crud.conversations_with_attachments(db, conversation_ids):
        out[cid]["has_attachments"] = True
    for entry in out.values():
        for s in entry["senders"]:
            s.pop("key")
    return out


@router.get("/threads", dependencies=_protected)
async def list_threads(
    account_id: Optional[int] = None,
    folder: str = "inbox",
    label: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Email threads of one account (or every Gmail account) in a folder:
    ``inbox`` (not archived; Promotions & co. are archived), ``sent``,
    ``archive``, ``all`` — or a ``label``. Each is a Conversation plus a
    ``thread`` summary (senders, message count, attachments)."""

    if folder not in FOLDERS:
        raise HTTPException(status_code=400, detail=f"Unknown folder: {folder}")
    archived = {"inbox": False, "archive": True}.get(folder) if label is None else None
    async with AsyncSessionLocal() as db:
        convs = await crud.list_conversations(
            db, limit=max(1, min(limit, 200)), offset=max(0, offset), kind="email_thread", account_id=account_id,
            channel="gmail", label=label or ("Sent" if folder == "sent" else None), archived=archived,
        )
        summaries = await _thread_summaries(db, [c.id for c in convs])
    return [{**conversation_json(c), "thread": summaries[c.id]} for c in convs]


@router.get("/labels", dependencies=_protected)
async def list_labels(account_id: Optional[int] = None):
    """Labels in use on one account (or all Gmail accounts): ``[{label, count}]``."""

    async with AsyncSessionLocal() as db:
        rows = await crud.conversation_labels(db, account_id=account_id)
    return [{"label": label, "count": n} for label, n in rows]


class ArchiveRequest(BaseModel):
    archived: bool


class ReadRequest(BaseModel):
    read: bool


async def _modify(conversation_id: int, add: list, remove: list) -> dict:

    conv, connector = await _thread(conversation_id)
    try:
        await connector.modify_thread(conv, add=add, remove=remove)
    except (HTTPException, DomainError):
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gmail: {e}")
    async with AsyncSessionLocal() as db:
        fresh = await crud.get_conversation(db, conversation_id)
    await ws_manager.broadcast({"type": "conversations_synced", "conversation_ids": [conversation_id]})
    return conversation_json(fresh)


@router.post("/threads/{conversation_id}/archive", dependencies=_protected)
async def archive_thread(conversation_id: int, req: ArchiveRequest):
    """Archive (remove from Inbox) or move back to the Inbox, at Gmail."""
    return await _modify(conversation_id, [] if req.archived else ["INBOX"], ["INBOX"] if req.archived else [])


@router.post("/threads/{conversation_id}/read", dependencies=_protected)
async def mark_thread_read(conversation_id: int, req: ReadRequest):
    """Mark every message of the thread read, or the thread unread, at Gmail."""
    return await _modify(conversation_id, [] if req.read else ["UNREAD"], ["UNREAD"] if req.read else [])


@router.post("/compose", dependencies=_protected)
async def compose_new(
    account_id: int = Form(...),
    text: str = Form(""),
    subject: str = Form(""),
    to: List[str] = Form([]),
    cc: List[str] = Form([]),
    client_nonce: Optional[str] = Form(None),
    files: List[UploadFile] = File([]),
):
    """Start a new email thread from one account (multipart, optional files).
    Returns ``{conversation_id, message_id, message}``."""

    connector = _connector(account_id)
    temp_paths: list = []
    started = False
    try:
        outgoing = []
        for upload in files:
            name = upload.filename or "file"
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(name)[1]) as tmp:
                tmp.write(await upload.read())
                temp_paths.append(tmp.name)
            outgoing.append(OutgoingFile(path=tmp.name, filename=name, mime_type=upload.content_type))
        draft = OutgoingMessage(text=text, subject=subject, to=to, cc=cc, files=outgoing)

        async def _send() -> dict:
            nonlocal started
            started = True
            try:
                conv_id, ids = await connector.send_new(draft)
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))
            finally:
                cleanup_temp_files(temp_paths)
            async with AsyncSessionLocal() as db:
                rows = await crud.get_reply_previews(db, ids)
                messages = await messages_out(db, list(rows))
            return {"success": True, "conversation_id": conv_id,
                    "message_id": messages[0]["id"] if messages else None,
                    "message": messages[0] if messages else None}

        return await idempotent(client_nonce, _send)
    finally:
        # Once the send started, it owns the temp files (it may outlive this
        # request if the client disconnects); otherwise clean up here.
        if not started:
            cleanup_temp_files(temp_paths)
