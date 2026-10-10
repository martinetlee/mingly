"""What the user sends or changes on Telegram: messages, albums, edits,
deletes, forwards, pins, reactions and read receipts.

Called by ``TelegramConnector``; arguments are stored rows, and sent messages
are persisted like received ones (``rows_for`` + ``crud.save_message``).
"""

import os
from typing import Optional

from backend.connectors.base import OutgoingMessage, SendFailed, SentFiles
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, Message
from backend.errors import NotAuthenticated
from backend.telegram import client as tg_client
from backend.telegram import ids
from backend.telegram.convert import GENERAL_TOPIC_ID, rows_for

ALBUM_MAX = 10  # Telegram's album size limit

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv"}


async def _authorized_client():
    client = await tg_client.connected_client()
    if not await client.is_user_authorized():
        raise NotAuthenticated()
    return client


def _reply_target(reply_to_seq: Optional[int], topic_id: Optional[int]) -> Optional[int]:
    """Telegram reply header: the replied-to message, else (in a forum topic)
    the topic's root message; General (topic 1) takes no reply header."""
    if reply_to_seq:
        return reply_to_seq
    if topic_id and topic_id != GENERAL_TOPIC_ID:
        return topic_id
    return None


async def _reply_seq(reply_to_message_id: Optional[int]) -> Optional[int]:
    if reply_to_message_id is None:
        return None
    async with AsyncSessionLocal() as db:
        msg = await crud.get_message(db, reply_to_message_id)
    return ids.tg_message_id_of(msg) if msg is not None else None


async def _persist_sent(conv: Conversation, msgs: list, reply_to_seq: Optional[int],
                        topic_id: Optional[int]) -> list[int]:
    """Store sent Telegram messages; returns their internal ids in order."""
    async with AsyncSessionLocal() as db:
        rows = await rows_for(db, conv.id, msgs, forum=conv.is_forum)
        saved_ids = []
        for i, row in enumerate(rows):
            if topic_id is not None:
                row["topic_id"] = topic_id
            if i == 0 and reply_to_seq:
                row["reply_to_external_id"] = ids.message_key(reply_to_seq)
            saved_ids.append((await crud.save_message(db, **row)).id)
        return saved_ids


async def send(conv: Conversation, draft: OutgoingMessage) -> list[int]:
    if draft.files:
        return (await send_files(conv, draft)).message_ids
    client = await _authorized_client()
    reply_seq = await _reply_seq(draft.reply_to_message_id)
    msg = await client.send_message(
        ids.peer_id_of(conv), draft.text, reply_to=_reply_target(reply_seq, draft.topic_id),
    )
    return await _persist_sent(conv, [msg], reply_seq, draft.topic_id)


def _is_document(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext not in _IMAGE_EXTS and ext not in _VIDEO_EXTS


async def send_files(conv: Conversation, draft: OutgoingMessage) -> SentFiles:
    """Albums: photos/videos grouped together, documents grouped together
    (Telegram can't mix them), at most ALBUM_MAX per album, one request per
    album so a failure maps to exactly those files. The caption goes on the
    first item that is sent. Raises SendFailed when nothing was sent."""
    client = await _authorized_client()
    peer = ids.peer_id_of(conv)
    reply_seq = await _reply_seq(draft.reply_to_message_id)
    reply_to = _reply_target(reply_seq, draft.topic_id)
    entries = [
        {"filename": f.filename, "path": f.path, "document": _is_document(f.filename), "status": "pending"}
        for f in draft.files
    ]
    sent_msgs = []
    caption_pending = bool(draft.text)
    for as_doc in (False, True):
        group = [e for e in entries if e["document"] == as_doc]
        for i in range(0, len(group), ALBUM_MAX):
            chunk = group[i:i + ALBUM_MAX]
            captions = [draft.text if caption_pending and j == 0 else "" for j in range(len(chunk))]
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
        raise SendFailed(f"No files were sent: {entries[0].get('error')}", _report())
    saved_ids = await _persist_sent(conv, sent_msgs, reply_seq, draft.topic_id)
    async with AsyncSessionLocal() as db:
        stored = await crud.get_messages_by_ids(db, saved_ids)
    by_tg = {ids.tg_message_id_of(stored[i]): i for i in saved_ids if i in stored}
    for entry in entries:
        if "tg_id" in entry:
            entry["message_id"] = by_tg.get(entry.pop("tg_id"))
    return SentFiles(message_ids=saved_ids, files=_report())


async def edit_message(conv: Conversation, message: Message, text: str) -> int:
    client = await _authorized_client()
    msg = await client.edit_message(ids.peer_id_of(conv), ids.tg_message_id_of(message), text)
    return (await _persist_sent(conv, [msg], None, None))[0]


async def delete_messages(conv: Conversation, messages: list, *, revoke: bool) -> None:
    client = await _authorized_client()
    await client.delete_messages(ids.peer_id_of(conv), [ids.tg_message_id_of(m) for m in messages], revoke=revoke)


async def forward_messages(source: Conversation, messages: list, target: Conversation) -> None:
    client = await _authorized_client()
    await client.forward_messages(
        ids.peer_id_of(target), [ids.tg_message_id_of(m) for m in messages], ids.peer_id_of(source),
    )


async def pin_message(conv: Conversation, message: Message, *, pinned: bool, notify: bool) -> None:
    client = await tg_client.connected_client()
    peer, msg_id = ids.peer_id_of(conv), ids.tg_message_id_of(message)
    if pinned:
        await client.pin_message(peer, msg_id, notify=notify)
    else:
        await client.unpin_message(peer, msg_id)


async def react(conv: Conversation, message: Message, emoji: Optional[str]) -> None:
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji

    client = await tg_client.connected_client()
    reaction = [ReactionEmoji(emoticon=emoji)] if emoji else []
    await client(SendReactionRequest(peer=ids.peer_id_of(conv), msg_id=ids.tg_message_id_of(message), reaction=reaction))


async def acknowledge_read(conv: Conversation) -> None:
    """Best-effort: without it the periodic dialog sync restores Telegram's
    unread count and the badge resurrects within ~2 min."""
    client = await tg_client.connected_client(require=False)
    if client is None:
        return
    await client.send_read_acknowledge(await client.get_input_entity(ids.peer_id_of(conv)))
