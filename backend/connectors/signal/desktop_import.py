"""One-time import of Signal Desktop's history into Mingly's Signal account.

Runs in the background (one at a time), reporting progress through
``state()`` like the signal-cli setup (``install.py``). Every row is written
the way live Signal messages are (``connector.py`` / ``envelopes.py`` keys:
DMs by peer ACI, groups by base64 group id, messages
``"<author ACI>:<sent_at>"``), so history and live messages merge without
duplicates, through ``ingest_service.ingest(source="backfill")``: no
notifications, no Shield scan, no unread changes.

A message Mingly already stores (received live) keeps its text and edits;
Desktop only adds the reactions it knows of and attachment files it lacks.
Attachments are decrypted into Mingly's media folder. The decrypted copy of
Desktop's database lives in a private temp folder only while the import runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from backend import config, media_paths
from backend.connectors.signal import desktop
from backend.connectors.signal.desktop import DesktopConversation, DesktopImportError, DesktopMessage, DesktopReader
from backend.connectors.signal.envelopes import attachment_kind, extension_for, message_key, utc
from backend.connectors.sync_state import merge_sync_state
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Message, MessageAttachment
from backend.ingest import service as ingest_service
from backend.ingest.normalized import (
    NormalizedAttachment,
    NormalizedConversation,
    NormalizedIdentity,
    NormalizedMessage,
    reaction_summary,
)
from backend.jobs import get_running, run_single
from backend.serialization import iso_utc

# The connector is passed in (backend.connectors.signal.connector imports this
# module, so it isn't imported back): it provides account_id, channel,
# _self_key, _aci, _number, _self_identity() and desktop_import.
SignalConnector = Any

logger = logging.getLogger(__name__)

BATCH = 400
JOB_KEY = "signal_desktop_import"


class _Cancelled(Exception):
    pass


@dataclass
class ImportState:
    status: str = "idle"  # idle | running | done | cancelled | error
    stage: str = ""
    conversations_total: int = 0
    conversations_done: int = 0
    messages_total: int = 0
    messages_done: int = 0
    messages_imported: int = 0
    files: int = 0
    files_skipped: int = 0
    error: Optional[str] = None
    cancel_requested: bool = False

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def progress(self) -> float:
        return self.messages_done / self.messages_total if self.messages_total else 0.0


_state = ImportState()


def state() -> ImportState:
    return _state


def cancel() -> ImportState:
    if _state.running:
        _state.cancel_requested = True
    return _state


def start(connector: SignalConnector, root: Optional[Path] = None) -> ImportState:
    """Start the import (or return the one running)."""
    global _state
    if _state.running and get_running(JOB_KEY) is not None:
        return _state
    _state = ImportState(status="running", stage="Unlocking Signal Desktop's data…")
    st = _state
    run_single(JOB_KEY, lambda: _run(connector, st, root))
    return st


async def _run(connector: SignalConnector, st: ImportState, root: Optional[Path]) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="signal-desktop-", dir=_tmp_root()))
    try:
        await _import(connector, st, root, workdir)
        st.status, st.stage = "done", "Done"
    except _Cancelled:
        st.status, st.stage = "cancelled", "Cancelled"
    except DesktopImportError as e:
        st.status, st.error = "error", str(e)
    except Exception as e:  # noqa: BLE001 - reported to the user, logged for us
        logger.exception("Signal Desktop import failed")
        st.status, st.error = "error", f"The import stopped: {e}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)  # the decrypted database never outlives the import


def _tmp_root() -> Path:
    path = Path(config.DATA_DIR) / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _check(st: ImportState) -> None:
    if st.cancel_requested:
        raise _Cancelled()


async def _import(connector: SignalConnector, st: ImportState, root: Optional[Path], workdir: Path) -> None:
    install = desktop.detect(root)
    if install is None:
        raise DesktopImportError("Signal Desktop isn't installed on this Mac (or has never been opened)")
    if connector.account_id is None or connector._self_key is None:
        raise DesktopImportError("Link Signal first")
    key = await asyncio.to_thread(desktop.database_key, install)
    _check(st)
    st.stage = "Reading Signal Desktop's messages…"
    plain = await asyncio.to_thread(desktop.snapshot, install, key, workdir)
    reader = await asyncio.to_thread(DesktopReader, plain, install.attachments)
    try:
        _same_account(connector, reader)
        counts = await asyncio.to_thread(
            lambda: [(c, reader.message_count(c.id)) for c in reader.conversations()]
        )
        convs = [c for c, n in counts if n]
        st.conversations_total = len(convs)
        st.messages_total = sum(n for _, n in counts)
        st.stage = "Importing conversations…"
        for conv in convs:
            _check(st)
            await _import_conversation(connector, reader, conv, st)
            st.conversations_done += 1
    finally:
        reader.close()
    record = {
        "at": iso_utc(datetime.utcnow()),
        "conversations": st.conversations_done,
        "messages": st.messages_imported,
        "files": st.files,
        "files_skipped": st.files_skipped,
        "desktop_schema": reader.schema_version,
    }
    await merge_sync_state(connector.account_id, desktop_import=record)
    connector.desktop_import = record


def _same_account(connector: SignalConnector, reader: DesktopReader) -> None:
    """Desktop must be signed in to the account linked to Mingly (by ACI,
    else by number)."""
    aci, number = reader.self_aci(), reader.self_number()
    if connector._aci and aci:
        same = connector._aci == aci
    else:
        same = not number or number == connector._number
    if not same:
        raise DesktopImportError(
            f"Signal Desktop is signed in to a different Signal account ({number or aci}) than the one linked "
            f"to Mingly ({connector._number})"
        )


def _identity(connector: SignalConnector, contact) -> NormalizedIdentity:
    if contact.aci and contact.aci == connector._aci:
        return connector._self_identity()
    return NormalizedIdentity(
        external_id=contact.aci or contact.e164 or "",
        display_name=contact.name or contact.e164,
        phone=contact.e164,
    )


def _conversation(connector: SignalConnector, conv: DesktopConversation) -> NormalizedConversation:
    if conv.kind == "group":
        return NormalizedConversation(conv.key, "group", title=conv.title or "Signal group")
    if conv.is_self:
        return NormalizedConversation(connector._self_key, "direct", title="Note to Self",
                                      peer=connector._self_identity())
    return NormalizedConversation(conv.key, "direct", title=conv.title or conv.peer.e164 or "Signal user",
                                  peer=_identity(connector, conv.peer))


def _external_id(connector: SignalConnector, m: DesktopMessage) -> Optional[str]:
    author = connector._self_key if m.outgoing else (m.author.aci or m.author.e164)
    return message_key(author, m.sent_at) if author else None


def _reactions(connector: SignalConnector, m: DesktopMessage) -> dict[str, str]:
    return {(connector._self_key if k == DesktopReader.SELF else k): v for k, v in m.reactions.items()}


def _meta(m: DesktopMessage) -> dict:
    meta: dict = {}
    if m.sticker:
        meta["sticker"] = m.sticker
    if m.expire_seconds:
        meta["expires_in_seconds"] = m.expire_seconds
    if m.view_once:
        meta["view_once"] = True
    return meta


def _attachment(a: desktop.DesktopAttachment) -> NormalizedAttachment:
    kind = "sticker" if a.kind == "sticker" else attachment_kind(a.content_type)
    return NormalizedAttachment(kind=kind, filename=a.file_name, mime_type=a.content_type, size=a.size)


async def _import_conversation(connector: SignalConnector, reader: DesktopReader, conv: DesktopConversation,
                               st: ImportState) -> None:
    nconv = _conversation(connector, conv)
    # Reading (and long-message decryption) stays off the event loop
    msgs = await asyncio.to_thread(lambda: list(reader.messages(conv)))
    async with AsyncSessionLocal() as db:
        stored_conv = await crud.get_conversation_by_external(db, connector.account_id, nconv.external_id)
        stored_ids = (await crud.message_ids_by_external(
            db, stored_conv.id, [e for e in (_external_id(connector, m) for m in msgs) if e]
        )) if stored_conv is not None else {}

    for i in range(0, max(len(msgs), 1), BATCH):
        _check(st)
        batch = msgs[i:i + BATCH]
        new, existing = [], []
        for m in batch:
            ext = _external_id(connector, m)
            if ext is None:
                continue
            (existing if ext in stored_ids else new).append((ext, m))
        if new:
            rows = []
            for ext, m in new:
                by_reactor = _reactions(connector, m)
                meta = _meta(m)
                if by_reactor:
                    meta["signal_reactions"] = by_reactor
                rows.append(NormalizedMessage(
                    external_id=ext,
                    date=utc(m.sent_at),
                    seq=m.sent_at,
                    sender=connector._self_identity() if m.outgoing else _identity(connector, m.author),
                    text=m.text,
                    is_outgoing=m.outgoing,
                    is_edited=m.is_edited,
                    reply_to_external_id=message_key(*m.quote) if m.quote else None,
                    attachments=[_attachment(a) for a in m.attachments] or None,
                    meta=meta or None,
                    reactions_json=reaction_summary(by_reactor),
                ))
            if rows:
                await ingest_service.ingest(connector.account_id, connector.channel, nconv, rows, source="backfill")
                st.messages_imported += len(rows)
        if existing:
            await _merge_into_stored(connector, nconv, existing)
        await _store_files(connector, reader, nconv, [(ext, m) for ext, m in new + existing if m.attachments], st)
        await _set_my_reactions(connector, nconv, [(ext, m) for ext, m in new if DesktopReader.SELF in m.reactions])
        st.messages_done += len(batch)


# Every write is its own short transaction that starts by writing: Mingly keeps
# syncing during an import, and in WAL mode a transaction that read before
# another writer committed can't write after it. Reads (and the slow file
# decryption) happen outside, so no transaction spans them.

async def _write(statements: list) -> None:
    """Run UPDATEs in one transaction, retrying while another writer holds
    the lock longer than the busy timeout (live sync during a long import)."""
    if not statements:
        return
    for attempt in range(6):
        try:
            async with AsyncSessionLocal() as db:
                for stmt in statements:
                    await db.execute(stmt)
                await db.commit()
            return
        except OperationalError as e:
            if "locked" not in str(e) or attempt == 5:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))


async def _stored(connector, nconv, exts: list) -> dict:
    """{external id: Message (with attachments)} of this conversation."""
    async with AsyncSessionLocal() as db:
        conv = await crud.get_conversation_by_external(db, connector.account_id, nconv.external_id)
        if conv is None:
            return {}
        ids = await crud.message_ids_by_external(db, conv.id, exts)
        by_id = await crud.get_messages_by_ids(db, list(ids.values()))
    return {ext: by_id[mid] for ext, mid in ids.items() if mid in by_id}


async def _merge_into_stored(connector, nconv, pairs) -> None:
    """Messages already stored (received live) keep everything they have;
    Desktop adds reactions from reactors they don't list. Each update applies
    only if the stored reactions are still what was read (a live reaction
    arriving meanwhile wins)."""
    stored = await _stored(connector, nconv, [ext for ext, _ in pairs])
    statements = []
    for ext, m in pairs:
        from_desktop = _reactions(connector, m)
        msg = stored.get(ext)
        if not from_desktop or msg is None:
            continue
        meta = json.loads(msg.meta_json) if msg.meta_json else {}
        by_reactor = {**from_desktop, **(meta.get("signal_reactions") or {})}
        if by_reactor == (meta.get("signal_reactions") or {}):
            continue
        meta["signal_reactions"] = by_reactor
        mine = by_reactor.get(connector._self_key)
        statements.append(
            update(Message)
            .where(Message.id == msg.id, Message.meta_json.is_(msg.meta_json) if msg.meta_json is None
                   else Message.meta_json == msg.meta_json)
            .values(meta_json=json.dumps(meta, ensure_ascii=False), reactions_json=reaction_summary(by_reactor),
                    my_reaction=mine if mine is not None else msg.my_reaction)
        )
    await _write(statements)


async def _set_my_reactions(connector, nconv, pairs) -> None:
    if not pairs:
        return
    stored = await _stored(connector, nconv, [ext for ext, _ in pairs])
    await _write([
        update(Message).where(Message.id == stored[ext].id).values(my_reaction=m.reactions[DesktopReader.SELF])
        for ext, m in pairs if ext in stored
    ])


async def _store_files(connector, reader: DesktopReader, nconv, pairs, st: ImportState) -> None:
    """Decrypt each attachment Mingly has no file for into its media folder."""
    if not pairs:
        return
    stored = await _stored(connector, nconv, [ext for ext, _ in pairs])
    statements = []
    for ext, m in pairs:
        msg = stored.get(ext)
        if msg is None:
            continue
        by_position = {a.position: a for a in msg.attachments}
        for position, att in enumerate(m.attachments):
            row = by_position.get(position)
            if row is None or row.local_path:
                continue
            dest = media_paths.attachment_file(msg.conversation_id, row.id, extension_for(att.file_name, att.content_type))
            try:
                data = await asyncio.to_thread(reader.read_attachment, att)
                await asyncio.to_thread(_write_atomic, dest, data)
            except (DesktopImportError, OSError, ValueError) as e:
                st.files_skipped += 1
                logger.debug("Signal Desktop attachment skipped: %s", e)
                continue
            statements.append(update(MessageAttachment).where(MessageAttachment.id == row.id)
                              .values(local_path=str(dest.relative_to(config.DATA_DIR))))
            st.files += 1
    await _write(statements)


def _write_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
