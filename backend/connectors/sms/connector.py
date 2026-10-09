"""SMS / iMessage as a read-only ``Connector`` over macOS Messages' database.

Messages on a Mac signed in to the user's Apple ID keeps every iMessage (and,
with Text Message Forwarding, every SMS) in ``~/Library/Messages/chat.db``.
Mingly reads it in place, read-only, which requires Full Disk Access.

Connecting checks the database can be read and creates the one ``accounts``
row (``channel="sms"``). At runtime the last ``sms_backfill_days`` (pref,
default 90) are imported once, then new rows (ROWID above the last one seen,
kept in ``accounts.sync_state_json``) are polled every ``poll_interval``
seconds and ingested as live messages. Tapbacks become reactions on their
target message, ``thread_originator_guid`` a reply, group events (rename,
member added / removed, left) service messages. Edits made after a message
was imported are not picked up (they change rows instead of adding them).

Sending is not supported (Messages has no API for it; AppleScript later).
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import phonenumbers
from sqlalchemy import select, update

from backend import config
from backend.connectors.base import AuthFlowError, AuthStep, Capability, Connector, ConnectorStatus
from backend.connectors.sms import chatdb
from backend.connectors.sms.chatdb import Batch, ChatDbUnavailable, RawAttachment, RawChat, RawMessage
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Message, MessageAttachment
from backend.ingest import fanout
from backend.ingest import service as ingest_service
from backend.ingest.normalized import (
    NormalizedAttachment, NormalizedConversation, NormalizedIdentity, NormalizedMessage,
)
from backend.tasks import spawn
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

CHANNEL = "sms"
ACCOUNT_EXTERNAL_ID = "local"
ACCOUNT_LABEL = "Messages on this Mac"
DEFAULT_BACKFILL_DAYS = 90
CHUNK = 1000  # messages read from chat.db per batch
# A message older than this when the poll first sees it (the app was closed)
# is stored quietly instead of notifying like a live one.
REALTIME_MAX_AGE = timedelta(hours=1)
GROUP_STYLE = 43
SELF_KEY = "me"  # reactor key of the user's own tapbacks
# iPhone photos are HEIC, which browsers can't show: converted to JPEG on fetch.
HEIC_MIMES = ("image/heic", "image/heif")
HEIC_EXTS = (".heic", ".heif")
SIPS_TIMEOUT = 60.0

TAPBACKS = {0: "❤️", 1: "👍", 2: "👎", 3: "😂", 4: "‼️", 5: "❓"}
_CUSTOM_EMOJI = 6  # iOS 17+: any emoji, in ``associated_message_emoji``
_ATTACHMENT_FILLER = "￼"  # where an attachment sits in the text
_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_LOCALE_REGION = re.compile(r"^[a-z]{2,3}[_-]([A-Z]{2})\b")

FDA_HELP = (
    "Mingly reads your Messages history from this Mac, which macOS only allows with Full Disk Access. "
    "Open System Settings → Privacy & Security → Full Disk Access, add Mingly and turn it on "
    "(when running Mingly from source, add the Terminal or editor app that starts it instead), "
    "then quit and reopen Mingly and connect again."
)
DONE_MESSAGE = (
    "Mingly now reads your iMessage and SMS conversations from Messages on this Mac (the last {days} days, "
    "then new messages as they arrive). This connection is read-only: replying from Mingly isn't supported "
    "yet, so answer in Messages."
)


def backfill_days() -> int:
    from backend.prefs import get_pref
    try:
        days = int(get_pref("sms_backfill_days", DEFAULT_BACKFILL_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_BACKFILL_DAYS
    return max(1, min(days, 3650))


# -- translation (pure) ---------------------------------------------------------------

def _default_region() -> Optional[str]:
    """Country for national-format numbers (``0912 345 678``) from the locale."""
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        m = _LOCALE_REGION.match(os.environ.get(var) or "")
        if m:
            return m.group(1)
    return None


def normalize_handle(raw: str) -> tuple[str, Optional[str]]:
    """``(external_id, e164 phone or None)`` of a Messages handle: E.164 for
    phone numbers, lowercased email, else the handle as is (short codes)."""
    handle = raw.strip()
    if "@" in handle:
        return handle.lower(), None
    phone = None
    if handle.startswith("+") or handle.startswith("00"):
        phone = crud.normalize_phone(handle)
    elif re.fullmatch(r"[\d\s().\-]{7,}", handle):
        region = _default_region()
        phone = crud.normalize_phone(handle, region) if region else None
    return (phone, phone) if phone else (handle.lower(), None)


def handle_identity(raw: str) -> NormalizedIdentity:
    external_id, phone = normalize_handle(raw)
    display = raw.strip()
    if phone:
        display = phonenumbers.format_number(phonenumbers.parse(phone), phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    return NormalizedIdentity(external_id=external_id, display_name=display, phone=phone)


def self_identity(own_handle: Optional[str] = None) -> NormalizedIdentity:
    phone = normalize_handle(own_handle)[1] if own_handle else None
    return NormalizedIdentity(external_id=SELF_KEY, display_name="Me", phone=phone, is_self=True)


def to_conversation(chat: RawChat) -> NormalizedConversation:
    members = [handle_identity(h) for h in chat.members]
    if chat.style == GROUP_STYLE or len(members) > 1:
        names = ", ".join(m.display_name or m.external_id for m in members[:3])
        if len(members) > 3:
            names += f" +{len(members) - 3}"
        return NormalizedConversation(
            chat.guid, "group", title=chat.display_name or names or "Group conversation",
            members_count=len(members) + 1,
        )
    peer = members[0] if members else handle_identity(chat.chat_identifier or chat.guid)
    return NormalizedConversation(chat.guid, "direct", title=chat.display_name or peer.display_name, peer=peer)


def _clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    text = text.replace(_ATTACHMENT_FILLER, "").strip()
    return text or None


def attachment_kind(mime: Optional[str], name: Optional[str]) -> str:
    mime = (mime or "").lower()
    if mime == "image/gif":
        return "gif"
    if mime.startswith("image/"):
        return "photo"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        # Audio messages recorded in Messages are .caf files.
        return "voice" if (name or "").lower().endswith(".caf") else "audio"
    return "document"


def extension_for(name: Optional[str], mime: Optional[str]) -> str:
    m = _EXT.search(name or "")
    if m:
        return m.group(0).lower()
    return (mimetypes.guess_extension(mime) if mime else None) or ""


def _attachments(raws: list[RawAttachment]) -> list[NormalizedAttachment]:
    out = []
    for a in raws:
        name = a.transfer_name or (os.path.basename(a.filename) if a.filename else None)
        if name and name.endswith(".pluginPayloadAttachment"):  # link-preview data, not a file
            continue
        mime = a.mime_type or (mimetypes.guess_type(name)[0] if name else None)
        out.append(NormalizedAttachment(
            attachment_kind(mime, name), filename=name, mime_type=mime, size=a.total_bytes,
            external_ref=a.filename or None,
        ))
    return out


def _actor(raw: RawMessage) -> str:
    if raw.is_from_me:
        return "You"
    return handle_identity(raw.handle).display_name if raw.handle else "Someone"


def service_text(raw: RawMessage) -> Optional[str]:
    """Text of a group event, None for events not worth showing."""
    actor = _actor(raw)
    other = handle_identity(raw.other_handle).display_name if raw.other_handle else "someone"
    if raw.item_type == 1:
        return f"{actor} {'removed' if raw.group_action_type == 1 else 'added'} {other}"
    if raw.item_type == 2:
        return f"{actor} named the conversation “{raw.group_title}”" if raw.group_title else \
            f"{actor} removed the conversation name"
    if raw.item_type == 3:
        if raw.group_action_type == 0:
            return f"{actor} left the conversation"
        return f"{actor} changed the group photo" if raw.group_action_type == 1 else f"{actor} removed the group photo"
    return None  # location sharing, kept audio, … — not conversation content


def strip_part(guid: str) -> str:
    """``p:0/<guid>`` / ``bp:<guid>`` (a part of a message) → ``<guid>``."""
    if guid.startswith("p:") and "/" in guid:
        return guid.split("/", 1)[1]
    if guid.startswith("bp:"):
        return guid[3:]
    return guid


@dataclass
class Reaction:
    target_guid: str
    reactor: str  # SELF_KEY or the reactor's identity external_id
    emoji: str
    remove: bool


def reaction_of(raw: RawMessage) -> Optional[Reaction]:
    t = raw.associated_type
    if 2000 <= t < 2100:
        kind, remove = t - 2000, False
    elif 3000 <= t < 3100:
        kind, remove = t - 3000, True
    else:
        return None
    emoji = raw.associated_emoji if kind == _CUSTOM_EMOJI else TAPBACKS.get(kind)
    if not emoji or not raw.associated_guid:
        return None
    reactor = SELF_KEY if raw.is_from_me else (normalize_handle(raw.handle)[0] if raw.handle else None)
    if reactor is None:
        return None
    return Reaction(strip_part(raw.associated_guid), reactor, emoji, remove)


def is_reaction_row(raw: RawMessage) -> bool:
    return 2000 <= raw.associated_type < 4000


def to_message(raw: RawMessage, attachments: list[RawAttachment], me: NormalizedIdentity) -> Optional[NormalizedMessage]:
    """A message row → NormalizedMessage; None for rows with nothing to show
    (tapbacks are ``reaction_of``'s)."""
    if is_reaction_row(raw):
        return None
    kind, files = "message", []
    if raw.item_type:
        kind, text = "service", service_text(raw)
        if text is None:
            return None
    else:
        text, files = _clean(raw.text), _attachments(attachments)
        if text is None and not files:
            return None
    sender = me if raw.is_from_me else (handle_identity(raw.handle) if raw.handle else None)
    return NormalizedMessage(
        external_id=raw.guid,
        date=raw.date or datetime.utcnow(),
        seq=raw.rowid,
        sender=sender,
        text=text,
        is_outgoing=raw.is_from_me,
        is_edited=raw.is_edited,
        kind=kind,
        reply_to_external_id=strip_part(raw.thread_originator_guid) if raw.thread_originator_guid else None,
        attachments=files,
        meta={"service": raw.service} if raw.service else {},
    )


def _reaction_summary(by_reactor: dict) -> Optional[str]:
    if not by_reactor:
        return None
    counts = Counter(by_reactor.values())
    return json.dumps([{"emoji": e, "count": n} for e, n in counts.most_common()], ensure_ascii=False)


# -- connector ------------------------------------------------------------------------

class SmsConnector(Connector):
    channel = CHANNEL
    display_name = "SMS / iMessage"
    capabilities = frozenset({Capability.HISTORY_BACKFILL})

    poll_interval = 10.0
    backoff_max = 300.0

    def __init__(self, db_path: Optional[Path] = None, attachments_root: Optional[Path] = None) -> None:
        super().__init__(account_id=None)
        self._db_path = Path(db_path) if db_path is not None else None
        self._attachments_root = Path(attachments_root) if attachments_root is not None else None
        self._state = "stopped"
        self._error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._me = self_identity()

    @property
    def account_key(self) -> str:
        # One Messages database per Mac.
        return "sms:default"

    @property
    def db_path(self) -> Path:
        return self._db_path or chatdb.default_db_path()

    @property
    def attachments_root(self) -> Path:
        return self._attachments_root or chatdb.default_attachments_root()

    # -- state -----------------------------------------------------------------------

    def is_configured(self) -> bool:
        # The manager's load_accounts sets account_id from the signed-in row.
        return self.account_id is not None

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(self._state, self._error)

    def conversation_capabilities(self, conversation: Any) -> frozenset[Capability]:
        """The account imports its history from chat.db; a conversation has
        no older history to page in (HISTORY_BACKFILL is the account's)."""
        return self.capabilities - {Capability.HISTORY_BACKFILL}

    async def _set_state(self, state: str, error: Optional[str] = None) -> None:
        if (state, error) == (self._state, self._error):
            return
        self._state, self._error = state, error
        await ws_manager.broadcast({
            "type": "connector_status", "channel": self.channel, "account_key": self.account_key,
            "state": state, "error": error,
        })

    async def _load_state(self) -> dict:
        async with AsyncSessionLocal() as db:
            account = await crud.get_account(db, self.account_id)
        return json.loads(account.sync_state_json) if account and account.sync_state_json else {}

    async def _save_state(self, state: dict) -> None:
        async with AsyncSessionLocal() as db:
            account = await crud.get_account(db, self.account_id)
            if account is None:
                return
            account.sync_state_json = json.dumps(state)
            await db.commit()

    # -- lifecycle -------------------------------------------------------------------

    async def start(self) -> bool:
        generation = self._stop_generation
        async with self._lifecycle():
            return await self._start_locked(generation)

    async def _start_locked(self, generation: int) -> bool:
        if self._stop_generation != generation:  # a stop came after this start was asked for
            return False
        if self._task is not None and not self._task.done():
            return True
        if self.account_id is None:
            return False
        self._wake = asyncio.Event()
        await self._set_state("syncing")
        self._task = spawn(self._run(), name="sms_sync")
        return True

    async def stop(self) -> None:
        self._stop_generation += 1
        async with self._lifecycle():
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._set_state("stopped")

    async def sync_now(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _run(self) -> None:
        delay = self.poll_interval
        while True:
            try:
                await self.sync_once()
            except ChatDbUnavailable as e:
                if e.reason == "denied":
                    await self._set_state("unauthorized", FDA_HELP)
                else:
                    await self._set_state("error", e.message)
                delay = min(max(delay, self.poll_interval) * 2, self.backoff_max)
            except Exception as e:
                logger.exception("Messages sync failed")
                await self._set_state("error", f"Reading Messages failed: {e}")
                delay = min(max(delay, self.poll_interval) * 2, self.backoff_max)
            else:
                await self._set_state("ready")
                delay = self.poll_interval
            assert self._wake is not None
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def sync_once(self) -> None:
        """Finish the initial import if needed, then ingest every new row."""
        state = await self._load_state()
        if not state.get("backfilled"):
            await self._backfill(state)
        last = int(state.get("last_rowid") or 0)
        while True:
            batch = await asyncio.to_thread(chatdb.read_after, self.db_path, last, CHUNK)
            if not batch.messages:
                return
            await self._ingest_batch(batch, realtime=True)
            last = batch.last_rowid
            state["last_rowid"] = last
            await self._save_state(state)

    async def _backfill(self, state: dict) -> None:
        since = datetime.utcnow() - timedelta(days=backfill_days())
        snapshot = await asyncio.to_thread(chatdb.max_rowid, self.db_path)
        last = int(state.get("backfill_rowid") or 0)
        while True:
            batch = await asyncio.to_thread(chatdb.read_after, self.db_path, last, CHUNK, since)
            if not batch.messages:
                break
            await self._ingest_batch(batch, realtime=False)
            last = batch.last_rowid
            state["backfill_rowid"] = last  # resume point if interrupted
            await self._save_state(state)
        # Rows past the snapshot that the date filter skipped are old history
        # synced in late; the poll must not import them as new.
        state.pop("backfill_rowid", None)
        state.update(backfilled=True, last_rowid=max(last, snapshot))
        await self._save_state(state)

    async def _ingest_batch(self, batch: Batch, *, realtime: bool) -> None:
        by_chat: OrderedDict[int, list[RawMessage]] = OrderedDict()
        for raw in batch.messages:
            if raw.chat_id is not None and raw.chat_id in batch.chats:
                by_chat.setdefault(raw.chat_id, []).append(raw)
        live_after = datetime.utcnow() - REALTIME_MAX_AGE
        for chat_id, raws in by_chat.items():
            conv = to_conversation(batch.chats[chat_id])
            reactions = [r for r in (reaction_of(raw) for raw in raws) if r is not None]
            msgs = [m for m in (to_message(raw, batch.attachments.get(raw.rowid, []), self._me) for raw in raws) if m]
            if msgs:
                await self._carry_reactions(conv.external_id, msgs)
                live = [m for m in msgs if realtime and m.date >= live_after]
                quiet = [m for m in msgs if not (realtime and m.date >= live_after)]
                if quiet:
                    await ingest_service.ingest(self.account_id, self.channel, conv, quiet, source="backfill")
                if live:
                    await ingest_service.ingest(self.account_id, self.channel, conv, live, source="realtime")
            if reactions:
                await self._apply_reactions(conv.external_id, reactions, live=realtime)

    async def _carry_reactions(self, conv_external_id: str, msgs: list[NormalizedMessage]) -> None:
        """A message imported again keeps its per-reactor map (its meta is
        replaced; ingest itself keeps the stored reaction summary)."""
        async with AsyncSessionLocal() as db:
            conv = await crud.get_conversation_by_external(db, self.account_id, conv_external_id)
            if conv is None:
                return
            stored = {
                m.external_id: m for m in (await db.execute(
                    select(Message).where(Message.conversation_id == conv.id,
                                          Message.external_id.in_([m.external_id for m in msgs]))
                )).scalars().all()
            }
        for m in msgs:
            old = stored.get(m.external_id)
            if old is None:
                continue
            old_meta = json.loads(old.meta_json) if old.meta_json else {}
            if old_meta.get("sms_reactions"):
                m.meta = {**(m.meta or {}), "sms_reactions": old_meta["sms_reactions"]}

    async def _apply_reactions(self, conv_external_id: str, reactions: list[Reaction], *, live: bool) -> None:
        changed: dict[int, Message] = {}
        async with AsyncSessionLocal() as db:
            conv = await crud.get_conversation_by_external(db, self.account_id, conv_external_id)
            if conv is None:
                return
            for r in reactions:
                msg = await crud.get_message_by_external(db, conv.id, r.target_guid)
                if msg is None:  # the target is older than what was imported
                    continue
                meta = json.loads(msg.meta_json) if msg.meta_json else {}
                by_reactor = dict(meta.get("sms_reactions") or {})
                if r.remove:
                    if by_reactor.get(r.reactor) != r.emoji:
                        continue
                    by_reactor.pop(r.reactor)
                else:
                    by_reactor[r.reactor] = r.emoji
                if by_reactor:
                    meta["sms_reactions"] = by_reactor
                else:
                    meta.pop("sms_reactions", None)
                msg.meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
                msg.reactions_json = _reaction_summary(by_reactor)
                if r.reactor == SELF_KEY:
                    msg.my_reaction = None if r.remove else r.emoji
                changed[msg.id] = msg
            await db.commit()
            for msg in changed.values():
                await db.refresh(msg)
            conversation_id = conv.id
        if not changed:
            return
        if live:
            for msg in changed.values():
                await fanout.message_edited(conversation_id, msg, text_changed=False)
        else:
            await ws_manager.broadcast({"type": "conversations_synced", "conversation_ids": [conversation_id]})

    # -- attachments -------------------------------------------------------------------

    def _attachment_source(self, ref: Optional[str]) -> Path:
        from backend.telegram.media import MediaNotFound

        if not ref:
            raise MediaNotFound("This attachment was never downloaded to this Mac")
        root = Path(os.path.normpath(self.attachments_root))
        path = Path(os.path.normpath(os.path.expanduser(ref)))
        if not path.is_absolute() or root not in path.parents:
            raise MediaNotFound("Not a Messages attachment")
        try:
            resolved = path.resolve(strict=True)
            real_root = root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise MediaNotFound("The attachment is no longer on this Mac")
        if real_root not in resolved.parents or not resolved.is_file():
            raise MediaNotFound("Not a Messages attachment")
        return resolved

    async def fetch_attachment(self, attachment: Any, message: Any, conversation: Any) -> str:
        src = self._attachment_source(attachment.external_ref)
        base = f"media/{conversation.id}/{attachment.id}"
        if is_heic(attachment.mime_type, attachment.filename or src.name):
            rel = f"{base}.jpg"
            if await _convert_to_jpeg(src, Path(config.DATA_DIR) / rel):
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        update(MessageAttachment).where(MessageAttachment.id == attachment.id)
                        .values(mime_type="image/jpeg")
                    )
                    await crud.set_attachment_local_path(db, attachment.id, rel)  # commits both
                return rel
        rel = base + extension_for(attachment.filename or src.name, attachment.mime_type)
        await asyncio.to_thread(_copy_atomic, src, Path(config.DATA_DIR) / rel)
        async with AsyncSessionLocal() as db:
            await crud.set_attachment_local_path(db, attachment.id, rel)
        return rel

    # -- sign-in ---------------------------------------------------------------------------

    async def begin_auth(self, params: dict[str, Any]) -> AuthStep:
        flow_id = secrets.token_urlsafe(12)
        try:
            own_handle = await asyncio.to_thread(chatdb.probe, self.db_path)
        except ChatDbUnavailable as e:
            if e.reason == "denied":
                return AuthStep(flow_id, "error", title="Full Disk Access needed", message=FDA_HELP)
            return AuthStep(flow_id, "error", title="Messages", message=e.message)
        self._me = self_identity(own_handle)
        async with AsyncSessionLocal() as db:
            account = await crud.get_or_create_channel_account(db, CHANNEL)
            self_id = await ingest_service.upsert_identity(db, CHANNEL, self._me)
            await db.commit()
            await crud.set_account_owner(
                db, account.id, external_id=ACCOUNT_EXTERNAL_ID, label=ACCOUNT_LABEL, self_identity_id=self_id,
            )
        self.account_id = account.id
        spawn(self._restart(), name="sms_restart")
        return AuthStep(
            flow_id, "done", title="Messages connected", message=DONE_MESSAGE.format(days=backfill_days()),
        )

    async def continue_auth(self, flow_id: str, data: dict[str, Any]) -> AuthStep:
        raise AuthFlowError("Unknown or expired sign-in flow")  # connecting is a single step

    async def _restart(self) -> None:
        generation = self._stop_generation
        async with self._lifecycle():
            await self._stop_locked()
            await self._start_locked(generation)

    async def logout(self) -> None:
        """Stop reading Messages. What was imported stays; connecting again
        continues from where the import stopped."""
        await self.stop()
        if self.account_id is not None:
            async with AsyncSessionLocal() as db:
                account = await crud.get_account(db, self.account_id)
                if account is not None:
                    account.external_id = None
                    await db.commit()
        self.account_id = None


def is_heic(mime: Optional[str], name: Optional[str]) -> bool:
    return (mime or "").lower() in HEIC_MIMES or (name or "").lower().endswith(HEIC_EXTS)


async def _convert_to_jpeg(src: Path, dest: Path) -> bool:
    """HEIC → JPEG with macOS's ``sips``, written atomically. False (and
    nothing written) when sips is missing or fails; the caller then keeps
    the original."""
    sips = shutil.which("sips")
    if sips is None:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.stem}.part.jpg")
    try:
        proc = await asyncio.create_subprocess_exec(
            sips, "-s", "format", "jpeg", str(src), "--out", str(tmp),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        logger.warning("Could not run sips to convert %s", src.name, exc_info=True)
        return False
    try:
        _, err = await asyncio.wait_for(proc.communicate(), SIPS_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        err, ok = b"timed out", False
    else:
        ok = proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0
    if not ok:
        logger.warning("sips could not convert %s: %s", src.name, (err or b"").decode(errors="replace").strip()[-300:])
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, dest)
    return True


def _copy_atomic(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)
