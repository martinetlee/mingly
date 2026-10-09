"""Signal as a ``Connector``, through a supervised ``signal-cli`` child.

Sign-in links Mingly as a secondary device of the user's phone
(``signal-cli link`` → a ``sgnl://linkdevice`` QR code to scan in Signal →
Settings → Linked devices). A linked device only receives messages sent
after linking; there is no history backfill.

At runtime one ``signal-cli -a <number> jsonRpc`` process per account
streams envelopes, which ``envelopes.parse_envelope`` turns into events and
this class persists through ``backend.ingest.service``. The process is
restarted with exponential backoff when it dies; state changes are broadcast
as ``connector_status`` events.
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
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select, update

from backend import config
from backend.connectors.base import (
    AuthFlowError, AuthStep, Capability, Connector, ConnectorStatus, OutgoingMessage,
)
from backend.connectors.signal import cli
from backend.connectors.signal.envelopes import (
    Address, ConvRef, DeleteEvent, MessageEvent, ReactionEvent, ReceiptEvent, attachment_kind, extension_for,
    message_key, parse_envelope, split_message_key, utc,
)
from backend.connectors.signal.rpc import JsonRpcProcess, ProcessGone, RpcError
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Account, Conversation, Message, MessageAttachment
from backend.ingest import fanout
from backend.ingest import service as ingest_service
from backend.ingest.normalized import (
    NormalizedAttachment, NormalizedConversation, NormalizedIdentity, NormalizedMessage,
)
from backend.tasks import spawn
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

DEVICE_NAME = "Mingly"
MAX_OPEN_FLOWS = 4
LINK_URI_TIMEOUT = 90  # JVM start + provisioning request
LINK_TIMEOUT = 600  # the phone has this long to scan the code
POLL_WAIT = 1.0  # continue_auth waits this long for the link to finish before answering "still waiting"
READ_RECEIPT_BATCH = 100  # mark_read acknowledges at most this many incoming messages
NO_HISTORY_NOTE = (
    "Signal only shares messages with a linked device from the moment it is linked, "
    "so earlier conversations stay on your phone."
)
_UNAUTHORIZED = re.compile(r"not registered|does not exist|authorization failed|deviceremoved|unregistered", re.I)
_ASSOCIATED = re.compile(r"Associated with:?\s*(\+\d{6,15})")
_DELIVERY_RANK = {"sending": 0, "sent": 1, "delivered": 2, "read": 3}


@dataclass
class _LinkFlow:
    proc: asyncio.subprocess.Process
    started: float
    uri: Optional[str] = None
    stdout: list = field(default_factory=list)
    stderr: deque = field(default_factory=lambda: deque(maxlen=20))
    readers: list = field(default_factory=list)


@dataclass
class _Contact:
    aci: Optional[str]
    number: Optional[str]
    name: Optional[str]
    is_contact: bool


def _contact_from(raw: dict) -> Optional[_Contact]:
    aci, number = raw.get("uuid") or None, raw.get("number") or None
    if not aci and not number:
        return None
    given = " ".join(p for p in (raw.get("givenName"), raw.get("familyName")) if p).strip()
    contact_name = raw.get("name") or given or raw.get("nickName") or None
    profile = raw.get("profile") or {}
    profile_name = " ".join(p for p in (profile.get("givenName"), profile.get("familyName")) if p).strip() or None
    return _Contact(aci, number, contact_name or profile_name or raw.get("username"), bool(contact_name))


def _reaction_summary(by_reactor: dict) -> Optional[str]:
    if not by_reactor:
        return None
    counts = Counter(by_reactor.values())
    return json.dumps([{"emoji": e, "count": n} for e, n in counts.most_common()], ensure_ascii=False)


class SignalConnector(Connector):
    channel = "signal"
    display_name = "Signal"
    multi_account = True
    capabilities = frozenset({
        Capability.SEND, Capability.REPLY, Capability.REACT, Capability.MEDIA_SEND, Capability.READ_RECEIPTS,
    })

    # Supervisor restart delays (seconds); a process that stayed up this long resets the backoff.
    backoff_initial = 2.0
    backoff_max = 300.0
    healthy_after = 60.0

    def __init__(self, account_id: Optional[int] = None) -> None:
        super().__init__(account_id)
        self._flows: OrderedDict[str, _LinkFlow] = OrderedDict()
        self._state = "stopped"
        self._error: Optional[str] = None
        self._number: Optional[str] = None
        self._aci: Optional[str] = None
        self._rpc: Optional[JsonRpcProcess] = None
        self._queue: Optional[asyncio.Queue] = None
        self._supervisor: Optional[asyncio.Task] = None
        self._consumer: Optional[asyncio.Task] = None
        self._stopping = False
        self._contacts: dict[str, _Contact] = {}  # by ACI and by number
        self._groups: dict[str, dict] = {}  # group id -> {"name", "members_count"}
        self._groups_looked_up: set[str] = set()  # unknown group ids already re-listed for

    # -- state -----------------------------------------------------------------

    def is_configured(self) -> bool:
        return self.account_id is not None and (Path(config.DATA_DIR) / "signal" / "data").is_dir()

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(self._state, self._error)

    async def _set_state(self, state: str, error: Optional[str] = None) -> None:
        if (state, error) == (self._state, self._error):
            return
        self._state, self._error = state, error
        await ws_manager.broadcast({
            "type": "connector_status", "channel": self.channel, "account_key": self.account_key,
            "state": state, "error": error,
        })

    @property
    def _self_key(self) -> Optional[str]:
        return self._aci or self._number

    def _is_self(self, addr: Address) -> bool:
        return bool((addr.aci and addr.aci == self._aci) or (addr.number and addr.number == self._number))

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> bool:
        if self._supervisor is not None and not self._supervisor.done():
            return True
        if self.account_id is None:
            return False
        async with AsyncSessionLocal() as db:
            account = await crud.get_account(db, self.account_id)
            self_ident = (
                await crud.get_identity(db, account.self_identity_id)
                if account is not None and account.self_identity_id else None
            )
        if account is None or not account.external_id:
            await self._set_state("unauthorized")
            return False
        self._number = account.external_id
        if self_ident is not None and not self_ident.external_id.startswith("+"):
            self._aci = self_ident.external_id
        exe = cli.find_signal_cli()
        if exe is None:
            await self._set_state("error", f"signal-cli isn't installed. {cli.INSTALL_HINT}")
            return False
        linked = await cli.list_accounts(exe)
        if linked is not None and self._number not in linked:
            await self._set_state("unauthorized", "This device is no longer linked. Link it again from Connections.")
            return False
        aci = cli.account_aci(self._number)
        if aci and aci != self._aci:
            await self._learn_aci(aci)

        self._stopping = False
        self._queue = asyncio.Queue()
        await self._set_state("starting")
        self._consumer = spawn(self._consume(self._queue), name=f"signal_consume:{self.account_key}")
        self._supervisor = spawn(self._supervise(exe), name=f"signal_supervise:{self.account_key}")
        return True

    async def stop(self) -> None:
        self._stopping = True
        rpc = self._rpc
        if rpc is not None:
            await rpc.stop()
        tasks = [t for t in (self._supervisor, self._consumer) if t is not None and not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._supervisor = self._consumer = None
        self._rpc = None
        await self._set_state("stopped")

    async def _supervise(self, exe: str) -> None:
        loop = asyncio.get_running_loop()
        delay = self.backoff_initial
        while not self._stopping:
            assert self._number is not None
            rpc = JsonRpcProcess([exe, *cli.config_args(), "-a", self._number, "jsonRpc"], self._on_notification)
            started = loop.time()
            try:
                await rpc.start()
            except OSError as e:
                error = f"Could not start signal-cli: {e}"
            else:
                self._rpc = rpc
                await self._set_state("syncing")
                up = spawn(self._on_process_up(rpc), name=f"signal_directory:{self.account_key}")
                code = await rpc.wait()
                up.cancel()
                self._rpc = None
                error = rpc.last_error() or f"signal-cli exited with code {code}"
            if self._stopping:
                return
            if _UNAUTHORIZED.search(error):
                await self._set_state("unauthorized", error)
                return
            if loop.time() - started >= self.healthy_after:
                delay = self.backoff_initial
            logger.warning("signal-cli for %s exited (%s); restarting in %.0fs", self.account_key, error, delay)
            await self._set_state("reconnecting", error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.backoff_max)

    async def _on_process_up(self, rpc: JsonRpcProcess) -> None:
        try:
            await self._refresh_directory(rpc)
        except ProcessGone:
            return
        except Exception:
            logger.warning("Signal contact/group refresh failed for %s", self.account_key, exc_info=True)
        if self._rpc is rpc:
            await self._set_state("ready")

    async def sync_now(self) -> None:
        if self._rpc is not None:
            await self._refresh_directory(self._rpc)

    async def _on_notification(self, method: str, params: dict) -> None:
        if method == "receive" and self._queue is not None:
            self._queue.put_nowait(params)

    async def _consume(self, queue: asyncio.Queue) -> None:
        while True:
            params = await queue.get()
            try:
                await self.handle_receive(params)
            except Exception:
                logger.exception("Signal envelope handling failed for %s", self.account_key)

    # -- address book --------------------------------------------------------------

    async def _refresh_directory(self, rpc: JsonRpcProcess) -> None:
        """Contacts and groups → identities (phone contact points link people
        across channels) and the titles of stored conversations."""
        contacts = [c for c in (_contact_from(r) for r in (await rpc.call("listContacts") or [])
                                if isinstance(r, dict)) if c]
        groups = await rpc.call("listGroups") or []
        self._contacts = {}
        for c in contacts:
            for k in (c.aci, c.number):
                if k:
                    self._contacts[k] = c
        self._set_groups(groups)
        async with AsyncSessionLocal() as db:
            for c in contacts:
                if self._is_self(Address(c.aci, c.number)):
                    continue
                await ingest_service.upsert_identity(db, self.channel, self._identity(Address(c.aci, c.number)))
                if c.name:
                    await db.execute(
                        update(Conversation)
                        .where(Conversation.account_id == self.account_id, Conversation.kind == "direct",
                               Conversation.external_id.in_([k for k in (c.aci, c.number) if k]))
                        .values(title=c.name)
                    )
            for gid, g in self._groups.items():
                values = {k: v for k, v in (("title", g["name"]), ("members_count", g["members_count"])) if v}
                if values:
                    await db.execute(
                        update(Conversation)
                        .where(Conversation.account_id == self.account_id, Conversation.external_id == gid)
                        .values(**values)
                    )
            await db.commit()

    def _set_groups(self, groups: list) -> None:
        self._groups = {
            str(g["id"]): {"name": g.get("name") or None, "members_count": len(g.get("members") or []) or None}
            for g in groups if isinstance(g, dict) and g.get("id")
        }

    def _contact(self, addr: Address) -> Optional[_Contact]:
        return self._contacts.get(addr.aci or "") or self._contacts.get(addr.number or "")

    def _identity(self, addr: Address) -> NormalizedIdentity:
        if self._is_self(addr):
            return self._self_identity()
        c = self._contact(addr)
        number = addr.number or (c.number if c else None)
        return NormalizedIdentity(
            external_id=addr.aci or (c.aci if c else None) or number or "",
            display_name=(c.name if c else None) or addr.name or number,
            phone=number,
            is_contact=c.is_contact if c else None,
        )

    def _self_identity(self) -> NormalizedIdentity:
        return NormalizedIdentity(external_id=self._self_key or "", phone=self._number, is_self=True)

    async def _learn_aci(self, aci: str) -> None:
        """The account's ACI became known: key the self identity by it."""
        self._aci = aci
        await self._record_self()

    async def _record_self(self) -> None:
        """Store the self identity (by ACI, else number) as the account owner."""
        if self.account_id is None or self._number is None:
            return
        async with AsyncSessionLocal() as db:
            ident_id = await ingest_service.upsert_identity(db, self.channel, self._self_identity())
            await db.commit()
            await crud.set_account_owner(
                db, self.account_id, external_id=self._number, label=self._number, self_identity_id=ident_id,
            )

    # -- envelopes -----------------------------------------------------------------

    async def handle_receive(self, params: dict) -> None:
        if params.get("exception"):
            logger.info("signal-cli could not decrypt a message: %s", params["exception"])
        envelope = params.get("envelope")
        if not isinstance(envelope, dict):
            return
        if (envelope.get("syncMessage") and envelope.get("sourceUuid")
                and envelope.get("sourceNumber") == self._number and envelope["sourceUuid"] != self._aci):
            await self._learn_aci(envelope["sourceUuid"])
        for event in parse_envelope(envelope, Address(self._aci, self._number)):
            if isinstance(event, MessageEvent):
                await self._on_message(event)
            elif isinstance(event, DeleteEvent):
                await ingest_service.delete_messages(self.account_id, event.conv.external_id, [event.target_external_id])
            elif isinstance(event, ReactionEvent):
                await self._apply_reaction(event.conv.external_id, event.target_external_id,
                                           event.reactor, event.emoji, event.remove)
            elif isinstance(event, ReceiptEvent):
                await self._apply_receipt(event)

    async def _conversation(self, ref: ConvRef) -> NormalizedConversation:
        async with AsyncSessionLocal() as db:
            stored = await crud.get_conversation_by_external(db, self.account_id, ref.external_id)
        stored_title = stored.title if stored is not None else None
        if ref.kind == "group":
            rpc = self._rpc
            if ref.external_id not in self._groups and ref.external_id not in self._groups_looked_up and rpc:
                self._groups_looked_up.add(ref.external_id)  # joined since start: list groups again, once
                try:
                    self._set_groups(await rpc.call("listGroups") or [])
                except RpcError:
                    logger.debug("listGroups failed", exc_info=True)
            g = self._groups.get(ref.external_id, {})
            return NormalizedConversation(
                external_id=ref.external_id, kind="group",
                title=g.get("name") or ref.group_name or stored_title or "Signal group",
                members_count=g.get("members_count"),
            )
        peer_addr = ref.peer or Address()
        if self._is_self(peer_addr) or ref.external_id == self._self_key:
            return NormalizedConversation(ref.external_id, "direct", title="Note to Self", peer=self._self_identity())
        peer = self._identity(peer_addr)
        c = self._contact(peer_addr)
        title = (c.name if c else None) or peer_addr.name or stored_title or peer.display_name or "Signal user"
        return NormalizedConversation(ref.external_id, "direct", title=title, peer=peer)

    async def _on_message(self, ev: MessageEvent) -> None:
        conv = await self._conversation(ev.conv)
        meta = dict(ev.meta)
        reactions_json = None
        attachments = list(ev.attachments)
        # Re-delivered or edited: keep what the event doesn't carry
        # (reactions, an edit's unchanged attachments).
        async with AsyncSessionLocal() as db:
            stored_conv = await crud.get_conversation_by_external(db, self.account_id, ev.conv.external_id)
            stored = (await crud.get_message_by_external(db, stored_conv.id, ev.external_id)
                      if stored_conv is not None else None)
            if stored is not None:
                old_meta = json.loads(stored.meta_json) if stored.meta_json else {}
                if old_meta.get("signal_reactions"):
                    meta["signal_reactions"] = old_meta["signal_reactions"]
                reactions_json = stored.reactions_json
                if not attachments:
                    attachments = [
                        NormalizedAttachment(a.kind, a.filename, a.mime_type, a.size, a.external_ref)
                        for a in (await db.execute(
                            select(MessageAttachment).where(MessageAttachment.message_id == stored.id)
                            .order_by(MessageAttachment.position)
                        )).scalars().all()
                    ]
        msg = NormalizedMessage(
            external_id=ev.external_id,
            date=ev.date,
            seq=ev.seq,
            sender=self._self_identity() if ev.is_outgoing else self._identity(ev.sender),
            text=ev.text,
            is_outgoing=ev.is_outgoing,
            is_edited=ev.is_edit or bool(stored is not None and stored.is_edited),
            reply_to_external_id=ev.reply_to_external_id,
            attachments=attachments,
            meta=meta,
            reactions_json=reactions_json,
        )
        res = await ingest_service.ingest(self.account_id, self.channel, conv, [msg], source="realtime")
        if res.updated:
            async with AsyncSessionLocal() as db:
                updated = await crud.get_messages_by_ids(db, res.updated)
            for mid in res.updated:
                await fanout.message_edited(res.conversation_id, updated[mid], text_changed=ev.is_edit)

    async def _apply_reaction(
        self, conv_external_id: str, target_external_id: str, reactor: Address, emoji: Optional[str], remove: bool,
    ) -> None:
        mine = self._is_self(reactor)
        reactor_key = self._self_key if mine else reactor.key
        if not reactor_key:
            return
        async with AsyncSessionLocal() as db:
            conv = await crud.get_conversation_by_external(db, self.account_id, conv_external_id)
            msg = await crud.get_message_by_external(db, conv.id, target_external_id) if conv is not None else None
            if msg is None:
                return
            meta = json.loads(msg.meta_json) if msg.meta_json else {}
            by_reactor = dict(meta.get("signal_reactions") or {})
            if remove or not emoji:
                by_reactor.pop(reactor_key, None)
            else:
                by_reactor[reactor_key] = emoji
            if by_reactor:
                meta["signal_reactions"] = by_reactor
            else:
                meta.pop("signal_reactions", None)
            msg.meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
            msg.reactions_json = _reaction_summary(by_reactor)
            if mine:
                msg.my_reaction = None if remove else emoji
            await db.commit()
            await db.refresh(msg)
            conversation_id = conv.id
        await fanout.message_edited(conversation_id, msg, text_changed=False)

    async def _apply_receipt(self, ev: ReceiptEvent) -> None:
        keys = [k for k in (self._aci, self._number) if k]
        ext_ids = [message_key(k, ts) for k in keys for ts in ev.timestamps]
        if not ext_ids:
            return
        rank = _DELIVERY_RANK[ev.state]
        async with AsyncSessionLocal() as db:
            msgs = list((await db.execute(
                select(Message).join(Conversation, Conversation.id == Message.conversation_id)
                .where(Conversation.account_id == self.account_id, Message.is_outgoing == True,  # noqa: E712
                       Message.external_id.in_(ext_ids))
            )).scalars().all())
            changed = [m for m in msgs if _DELIVERY_RANK.get(m.delivery_state or "", -1) < rank]
            for m in changed:
                m.delivery_state = ev.state
            await db.commit()
            for m in changed:
                await db.refresh(m)
        for m in changed:
            await fanout.message_edited(m.conversation_id, m, text_changed=False)

    # -- outgoing ------------------------------------------------------------------

    def _require_rpc(self) -> JsonRpcProcess:
        if self._rpc is None or self._self_key is None:
            raise RuntimeError("Signal is not connected")
        return self._rpc

    def _target(self, conversation: Any) -> dict:
        if conversation.kind == "group":
            return {"groupId": conversation.external_id}
        recipient = self._number if conversation.external_id == self._self_key else conversation.external_id
        return {"recipient": [recipient]}

    async def send(self, conversation: Any, draft: OutgoingMessage) -> list[int]:
        rpc = self._require_rpc()
        if not draft.text and not draft.files:
            raise ValueError("Nothing to send")
        params = self._target(conversation)
        if draft.text:
            params["message"] = draft.text
        if draft.files:
            params["attachments"] = [f.path for f in draft.files]
        reply_ext = None
        if draft.reply_to_message_id is not None:
            async with AsyncSessionLocal() as db:
                quoted = await crud.get_message(db, draft.reply_to_message_id)
            if quoted is not None and quoted.conversation_id == conversation.id:
                author, ts = split_message_key(quoted.external_id)
                params.update(quoteTimestamp=ts, quoteAuthor=author, quoteMessage=quoted.text or "")
                reply_ext = quoted.external_id
        result = await rpc.call("send", params) or {}
        results = result.get("results") or []
        if results and not any(r.get("type") == "SUCCESS" for r in results if isinstance(r, dict)):
            raise RuntimeError(f"Signal could not deliver the message ({results[0].get('type', 'failed')})")
        ts = int(result["timestamp"])

        attachments = []
        for f in draft.files:
            mime = f.mime_type or mimetypes.guess_type(f.filename)[0]
            size = os.path.getsize(f.path) if os.path.exists(f.path) else None
            attachments.append(NormalizedAttachment(attachment_kind(mime), f.filename, mime, size))
        msg = NormalizedMessage(
            external_id=message_key(self._self_key, ts), date=utc(ts), seq=ts, sender=self._self_identity(),
            text=draft.text or None, is_outgoing=True, reply_to_external_id=reply_ext, attachments=attachments,
        )
        conv = NormalizedConversation(conversation.external_id, conversation.kind, title=conversation.title)
        res = await ingest_service.ingest(self.account_id, self.channel, conv, [msg], source="realtime")
        ids = res.created + res.updated
        if draft.files and ids:
            await self._keep_sent_files(res.conversation_id, ids[0], draft)
        return ids

    async def _keep_sent_files(self, conversation_id: int, message_id: int, draft: OutgoingMessage) -> None:
        """signal-cli doesn't keep what it sent: copy the files into media/
        now, while the caller's temp files still exist."""
        from backend.serializers import media_url

        async with AsyncSessionLocal() as db:
            atts = list((await db.execute(
                select(MessageAttachment).where(MessageAttachment.message_id == message_id)
                .order_by(MessageAttachment.position)
            )).scalars().all())
        for att, f in zip(atts, draft.files):
            rel = self._media_rel(conversation_id, att.id, extension_for(f.filename, att.mime_type))
            try:
                await asyncio.to_thread(_copy_atomic, Path(f.path), Path(config.DATA_DIR) / rel)
            except OSError:
                logger.warning("Could not keep sent Signal file %s", f.filename, exc_info=True)
                continue
            async with AsyncSessionLocal() as db:
                await crud.set_attachment_local_path(db, att.id, rel)
            await ws_manager.broadcast({
                "type": "media_ready", "conversation_id": conversation_id, "message_id": message_id,
                "attachment_id": att.id, "kind": att.kind, "url": media_url(rel),
            })

    async def react(self, conversation: Any, message: Any, emoji: Optional[str]) -> None:
        """Set (or with ``emoji=None`` remove) the user's reaction."""
        rpc = self._require_rpc()
        previous = message.my_reaction
        if emoji is None and not previous:
            return
        author, ts = split_message_key(message.external_id)
        params = self._target(conversation)
        params.update(emoji=emoji or previous, targetAuthor=author, targetTimestamp=ts, remove=emoji is None)
        await rpc.call("sendReaction", params)
        await self._apply_reaction(conversation.external_id, message.external_id,
                                   Address(self._aci, self._number), emoji, emoji is None)

    async def mark_read(self, conversation: Any) -> None:
        """Send read receipts for incoming messages not yet acknowledged
        (the newest ``READ_RECEIPT_BATCH``), one ``sendReceipt`` per sender.
        The acknowledged position is kept per conversation in
        ``accounts.sync_state_json`` so a message is receipted once."""
        rpc = self._rpc
        if rpc is None:
            return
        async with AsyncSessionLocal() as db:
            account = await crud.get_account(db, self.account_id)
            state = json.loads(account.sync_state_json) if account and account.sync_state_json else {}
            read_through = (state.get("read_through") or {}).get(conversation.external_id, 0)
            msgs = list((await db.execute(
                select(Message).where(Message.conversation_id == conversation.id,
                                      Message.is_outgoing == False,  # noqa: E712
                                      Message.seq > read_through)
                .order_by(Message.seq.desc()).limit(READ_RECEIPT_BATCH)
            )).scalars().all())
        if not msgs:
            return
        by_sender: dict[str, list[int]] = {}
        for m in msgs:
            author, ts = split_message_key(m.external_id)
            by_sender.setdefault(author, []).append(ts)
        for author, stamps in by_sender.items():
            await rpc.call("sendReceipt", {"recipient": [author], "targetTimestamp": sorted(stamps), "type": "read"})
        async with AsyncSessionLocal() as db:
            account = await crud.get_account(db, self.account_id)
            if account is None:
                return
            state = json.loads(account.sync_state_json) if account.sync_state_json else {}
            marks = state.setdefault("read_through", {})
            marks[conversation.external_id] = max(marks.get(conversation.external_id, 0), msgs[0].seq)
            account.sync_state_json = json.dumps(state)
            await db.commit()

    # -- attachments ---------------------------------------------------------------

    @staticmethod
    def _media_rel(conversation_id: int, attachment_id: int, ext: str) -> str:
        return f"media/{conversation_id}/{attachment_id}{ext}"

    async def fetch_attachment(self, attachment: Any, message: Any, conversation: Any) -> str:
        from backend.telegram.media import MediaNotFound

        ref = attachment.external_ref
        if not ref or Path(ref).name != ref or ref in (".", ".."):
            raise MediaNotFound("This Signal attachment can't be downloaded")
        src = cli.attachments_dir() / ref
        if not src.is_file():
            raise MediaNotFound("The attachment is no longer in signal-cli's storage")
        rel = self._media_rel(conversation.id, attachment.id, extension_for(attachment.filename, attachment.mime_type))
        await asyncio.to_thread(_copy_atomic, src, Path(config.DATA_DIR) / rel)
        async with AsyncSessionLocal() as db:
            await crud.set_attachment_local_path(db, attachment.id, rel)
        return rel

    # -- sign-in (device linking) ------------------------------------------------------

    def _qr_step(self, flow_id: str, flow: _LinkFlow) -> AuthStep:
        return AuthStep(
            flow_id, "qr", title="Link Signal",
            message="On your phone open Signal → Settings → Linked devices → Link new device, and scan this code.",
            qr_payload=flow.uri, poll=True,
        )

    def _error_step(self, flow_id: str, message: str, title: str = "Signal") -> AuthStep:
        return AuthStep(flow_id, "error", title=title, message=message)

    def _drop_flow(self, flow_id: str) -> None:
        flow = self._flows.pop(flow_id, None)
        if flow is not None and flow.proc.returncode is None:
            try:
                flow.proc.kill()
            except ProcessLookupError:
                pass

    async def begin_auth(self, params: dict[str, Any]) -> AuthStep:
        flow_id = secrets.token_urlsafe(12)
        try:
            exe, _version = await cli.check_installed()
        except cli.SignalCliMissing as e:
            return self._error_step(flow_id, str(e), title="signal-cli is required")
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, *cli.config_args(), "link", "-n", DEVICE_NAME,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=cli.child_env(),
            )
        except OSError as e:
            return self._error_step(flow_id, f"Could not start signal-cli: {e}")
        flow = _LinkFlow(proc, time.monotonic())
        got_uri = asyncio.Event()

        async def read_stdout() -> None:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                flow.stdout.append(line)
                if flow.uri is None and (line.startswith("sgnl://") or line.startswith("tsdevice:")):
                    flow.uri = line
                    got_uri.set()

        async def read_stderr() -> None:
            assert proc.stderr is not None
            async for raw in proc.stderr:
                flow.stderr.append(raw.decode(errors="replace").rstrip())

        flow.readers = [spawn(read_stdout(), name="signal_link_stdout"), spawn(read_stderr(), name="signal_link_stderr")]
        uri_wait = asyncio.ensure_future(got_uri.wait())
        exited = asyncio.ensure_future(proc.wait())
        await asyncio.wait({uri_wait, exited}, timeout=LINK_URI_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        uri_wait.cancel()
        exited.cancel()
        if flow.uri is None:
            if proc.returncode is None:
                proc.kill()
            await asyncio.gather(*flow.readers, return_exceptions=True)
            detail = next((ln for ln in reversed(flow.stderr) if ln.strip()), None) or "no link code received"
            return self._error_step(flow_id, f"signal-cli could not start linking: {detail}")
        self._flows[flow_id] = flow
        while len(self._flows) > MAX_OPEN_FLOWS:
            self._drop_flow(next(iter(self._flows)))
        return self._qr_step(flow_id, flow)

    async def continue_auth(self, flow_id: str, data: dict[str, Any]) -> AuthStep:
        flow = self._flows.get(flow_id)
        if flow is None:
            raise AuthFlowError("Unknown or expired sign-in flow")
        if flow.proc.returncode is None:
            try:
                await asyncio.wait_for(asyncio.shield(flow.proc.wait()), POLL_WAIT)
            except asyncio.TimeoutError:
                if time.monotonic() - flow.started > LINK_TIMEOUT:
                    self._drop_flow(flow_id)
                    return self._error_step(flow_id, "The link code expired. Start again to get a new one.")
                return self._qr_step(flow_id, flow)
        self._flows.pop(flow_id, None)
        await asyncio.gather(*flow.readers, return_exceptions=True)
        if flow.proc.returncode != 0:
            detail = next((ln for ln in reversed(flow.stderr) if ln.strip()), None) or f"exit code {flow.proc.returncode}"
            return self._error_step(flow_id, f"Linking failed: {detail}")
        number = None
        for line in flow.stdout:
            m = _ASSOCIATED.search(line)
            if m:
                number = m.group(1)
        if number is None:
            number = await self._newly_linked_number()
        if number is None:
            return self._error_step(flow_id, "Linked, but signal-cli did not report the phone number.")
        return await self._finish_link(flow_id, number)

    async def _newly_linked_number(self) -> Optional[str]:
        exe = cli.find_signal_cli()
        numbers = await cli.list_accounts(exe) if exe else None
        if not numbers:
            return None
        async with AsyncSessionLocal() as db:
            known = {a.external_id for a in await crud.get_accounts(db, self.channel)}
        fresh = [n for n in numbers if n not in known]
        return fresh[0] if len(fresh) == 1 else (numbers[0] if len(numbers) == 1 else None)

    async def _finish_link(self, flow_id: str, number: str) -> AuthStep:
        was_registered = self.account_id is not None
        async with AsyncSessionLocal() as db:
            existing = (await db.execute(
                select(Account).where(Account.channel == self.channel, Account.external_id == number)
            )).scalar_one_or_none()
            if existing is not None and existing.id != self.account_id:
                return self._error_step(flow_id, f"{number} is already connected.")
            if self.account_id is None:
                account = Account(channel=self.channel, external_id=number, label=number)
                db.add(account)
                await db.commit()
                self.account_id = account.id
        self._number = number
        self._aci = cli.account_aci(number)
        await self._record_self()
        if was_registered:  # re-linked an account the manager already runs
            spawn(self._restart(), name=f"signal_restart:{self.account_key}")
        return AuthStep(flow_id, "done", title="Signal linked", message=f"Linked {number}. {NO_HISTORY_NOTE}")

    async def _restart(self) -> None:
        await self.stop()
        await self.start()

    async def logout(self) -> None:
        """Stop, unlink this device and delete its local Signal data."""
        await self.stop()
        exe = cli.find_signal_cli()
        if exe and self._number:
            base = [*cli.config_args(), "-a", self._number]
            for args in (["unregister"], ["deleteLocalAccountData", "--ignore-registered"]):
                try:
                    res = await cli.run(exe, *base, *args)
                    if res.returncode != 0:
                        logger.warning("signal-cli %s failed: %s", args[0], res.stderr.strip()[-300:])
                except (OSError, asyncio.TimeoutError):
                    logger.warning("signal-cli %s failed", args[0], exc_info=True)
        await self._set_state("unauthorized")


def _copy_atomic(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)
