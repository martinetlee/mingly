"""Signal Desktop history import (backend/connectors/signal/desktop*.py).

Fixtures are written in Signal Desktop's own formats by the encryptors below
(SQLCipher 4 pages, the WAL, Electron safeStorage, v2 attachments), so the
reader is checked against the formats, not against itself; the importer
then lands the history in Mingly with the live connector's keys.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import struct
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import select

from backend import config
from backend.connectors.signal import desktop, desktop_import
from backend.connectors.signal.connector import NO_HISTORY_NOTE, SignalConnector
from backend.connectors.signal.desktop import DesktopImportError, DesktopReader
from backend.connectors.sync_state import load_sync_state
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, Identity, Message, MessageAttachment
from backend.jobs import get_running
from tests import factories

PAGE = 4096
KEY = bytes(range(32))
ME, ALICE, BOB = "aci-me-0000", "aci-alice-1111", "aci-bob-2222"
ME_NUMBER, BOB_NUMBER = "+15550001111", "+15550002222"
GROUP_ID = base64.b64encode(b"g" * 32).decode()


# ── Writers in Signal Desktop's formats ──────────────────────────────────────

def _empty_db(path: Path) -> None:
    """An empty SQLite database whose pages reserve 80 bytes, like the
    plaintext of a SQLCipher 4 database (SQLite keeps an existing header's
    reserve)."""
    h = bytearray(100)
    h[0:16] = b"SQLite format 3\x00"
    struct.pack_into(">H", h, 16, PAGE)
    h[18], h[19], h[20], h[21], h[22], h[23] = 1, 1, 80, 64, 32, 32
    struct.pack_into(">I", h, 24, 1)
    struct.pack_into(">I", h, 28, 1)
    struct.pack_into(">I", h, 44, 4)
    struct.pack_into(">I", h, 56, 1)
    struct.pack_into(">I", h, 92, 1)
    struct.pack_into(">I", h, 96, 3037000)
    page = bytearray(PAGE)
    page[:100] = h
    page[100] = 0x0D  # empty table leaf (sqlite_schema)
    struct.pack_into(">H", page, 105, PAGE - 80)
    path.write_bytes(bytes(page))


def _wal_mode(plain: bytes) -> bytes:
    """Mark a database WAL-mode in its header, as Signal Desktop's is."""
    return plain[:18] + b"\x02\x02" + plain[20:]


def _hmac_key(salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", KEY, bytes(b ^ 0x3A for b in salt), 2, 32)


def _encrypt_page(plain: bytes, pgno: int, salt: bytes, key: bytes = KEY) -> bytes:
    offset = 16 if pgno == 1 else 0
    iv = os.urandom(16)
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(plain[offset:PAGE - 80]) + enc.finalize()
    mac = hmac.new(_hmac_key(salt), ct + iv + struct.pack("<I", pgno), hashlib.sha512).digest()
    return (salt if pgno == 1 else b"") + ct + iv + mac


def _encrypt_db(plain: bytes, salt: bytes = b"S" * 16) -> bytes:
    pages = [plain[i:i + PAGE] for i in range(0, len(plain), PAGE)]
    return b"".join(_encrypt_page(p, n, salt) for n, p in enumerate(pages, start=1))


def _wal_checksum(data: bytes, s0: int, s1: int) -> tuple:
    words = struct.unpack(f"<{len(data) // 4}I", data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def _wal(frames: list, salt: bytes = b"S" * 16) -> bytes:
    """frames: [(pgno, plain page, db size after commit or 0)], encrypted."""
    s1_, s2_ = 7, 9
    head = struct.pack(">6I", 0x377F0682, 3007000, PAGE, 0, s1_, s2_)
    c = _wal_checksum(head, 0, 0)
    out = bytearray(head + struct.pack(">2I", *c))
    for pgno, plain, size_after in frames:
        data = _encrypt_page(plain, pgno, salt)
        fh = struct.pack(">4I", pgno, size_after, s1_, s2_)
        c = _wal_checksum(fh[:8], *c)
        c = _wal_checksum(data, *c)
        out += fh + struct.pack(">2I", *c) + data
    return bytes(out)


def _safe_storage(plaintext: str, password: bytes) -> bytes:
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    padder = sym_padding.PKCS7(128).padder()
    data = padder.update(plaintext.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return b"v10" + enc.update(data) + enc.finalize()


def _encrypt_attachment(plain: bytes, keys: bytes) -> bytes:
    """Signal Desktop's v2 local format: IV ‖ AES-CBC(plain + zero padding) ‖ HMAC-SHA256."""
    iv = os.urandom(16)
    padded_plain = plain + b"\x00" * 541  # bucket padding (zeros), trimmed by size on read
    padder = sym_padding.PKCS7(128).padder()
    data = padder.update(padded_plain) + padder.finalize()
    enc = Cipher(algorithms.AES(keys[:32]), modes.CBC(iv)).encryptor()
    body = iv + enc.update(data) + enc.finalize()
    return body + hmac.new(keys[32:], body, hashlib.sha256).digest()


# ── A Signal Desktop data folder ─────────────────────────────────────────────

def _desktop_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE conversations (id TEXT PRIMARY KEY, json TEXT, type TEXT, serviceId TEXT, e164 TEXT, groupId TEXT);
        CREATE TABLE messages (id TEXT PRIMARY KEY, json TEXT, conversationId TEXT, sent_at INTEGER, type TEXT,
            body TEXT, sourceServiceId TEXT, source TEXT, storyId TEXT, isErased INTEGER, isViewOnce INTEGER,
            expireTimer INTEGER);
        CREATE TABLE message_attachments (messageId TEXT, editHistoryIndex INTEGER, attachmentType TEXT,
            orderInMessage INTEGER, path TEXT, localKey TEXT, version INTEGER, size INTEGER, contentType TEXT,
            fileName TEXT);
        CREATE TABLE items (id TEXT PRIMARY KEY, json TEXT);
        PRAGMA user_version = 1830;
    """)


def _conv(db, cid, ctype, **fields):
    db.execute("INSERT INTO conversations (id, json, type, serviceId, e164, groupId) VALUES (?,?,?,?,?,?)",
               (cid, json.dumps({"id": cid, "type": ctype, **fields}), ctype, fields.get("serviceId"),
                fields.get("e164"), None))


def _message(db, mid, conv, sent_at, mtype, body=None, aci=None, **json_fields):
    db.execute(
        "INSERT INTO messages (id, json, conversationId, sent_at, type, body, sourceServiceId) VALUES (?,?,?,?,?,?,?)",
        (mid, json.dumps({"id": mid, **json_fields}), conv, sent_at, mtype, body, aci),
    )


def _attachment(db, mid, order, path, *, kind="attachment", local_key=None, version=None, size=None,
                content_type="image/jpeg", file_name=None):
    db.execute("INSERT INTO message_attachments VALUES (?,?,?,?,?,?,?,?,?,?)",
               (mid, -1, kind, order, path, local_key, version, size, content_type, file_name))


def build_desktop(root: Path, *, password: bytes = b"keychain-pw", legacy_key: bool = False) -> dict:
    """A Signal Desktop folder for the user ME with Alice (DM), Bob (DM by
    number only, no ACI known), a group, Note to Self, plus things the import
    skips. Returns facts the tests check."""
    (root / "sql").mkdir(parents=True)
    att_root = root / "attachments.noindex"
    plain_path = root / "plain.sqlite"
    _empty_db(plain_path)
    db = sqlite3.connect(plain_path)
    _desktop_schema(db)
    db.execute("INSERT INTO items VALUES ('uuid_id', ?)", (json.dumps({"id": "uuid_id", "value": f"{ME}.1"}),))
    db.execute("INSERT INTO items VALUES ('number_id', ?)", (json.dumps({"id": "number_id", "value": f"{ME_NUMBER}.1"}),))
    _conv(db, "c-me", "private", serviceId=ME, e164=ME_NUMBER, profileName="Me")
    _conv(db, "c-alice", "private", serviceId=ALICE, name="Alice Contact", profileName="Ali")
    _conv(db, "c-bob", "private", e164=BOB_NUMBER, profileName="Bob")
    _conv(db, "c-group", "group", groupId=GROUP_ID, groupVersion=2, name="Climbing crew")
    _conv(db, "c-v1", "group", groupId="legacy-v1", groupVersion=1, name="Old v1 group")
    _conv(db, "c-empty", "private", serviceId="aci-nobody", profileName="Nobody")

    keys = os.urandom(64)
    photo = b"\xff\xd8 a photo " * 50
    (att_root / "ab").mkdir(parents=True)
    (att_root / "ab" / "photo1").write_bytes(_encrypt_attachment(photo, keys))
    (att_root / "cd").mkdir(parents=True)
    (att_root / "cd" / "legacy").write_bytes(b"plain legacy file")
    long_text = "long " * 600
    long_keys = os.urandom(64)
    (att_root / "ef").mkdir(parents=True)
    (att_root / "ef" / "long").write_bytes(_encrypt_attachment(long_text.encode(), long_keys))

    # Alice: incoming with a reaction from me and Alice, my reply quoting it, a photo, a long message
    _message(db, "m1", "c-alice", 1_000, "incoming", "hi from desktop", ALICE,
             reactions=[{"emoji": "❤️", "fromId": "c-me", "targetTimestamp": 1_000},
                        {"emoji": "👍", "fromId": "c-alice", "targetTimestamp": 1_000}])
    _message(db, "m2", "c-alice", 2_000, "outgoing", "replying", None,
             quote={"id": 1_000, "authorAci": ALICE, "text": "hi from desktop"})
    _message(db, "m3", "c-alice", 3_000, "incoming", "look", ALICE)
    _attachment(db, "m3", 0, "ab/photo1", local_key=base64.b64encode(keys).decode(), version=2, size=len(photo),
                file_name="beach.jpg")
    _attachment(db, "m3", 1, "zz/missing", local_key=base64.b64encode(keys).decode(), version=2, size=10,
                content_type="application/pdf", file_name="gone.pdf")
    _message(db, "m4", "c-alice", 4_000, "incoming", "long long", ALICE)
    _attachment(db, "m4", 0, "ef/long", kind="long-message", local_key=base64.b64encode(long_keys).decode(),
                version=2, size=len(long_text.encode()), content_type="text/x-signal-plain")
    _message(db, "m5", "c-alice", 5_000, "incoming", "edited text", ALICE, editHistory=[{"body": "edited text"}])
    # Skipped: a story reply, a message deleted for everyone, a notification
    _message(db, "m6", "c-alice", 6_000, "incoming", "story reply", ALICE, storyId="s1")
    _message(db, "m7", "c-alice", 7_000, "incoming", "deleted", ALICE, deletedForEveryone=True)
    _message(db, "m8", "c-alice", 8_000, "keychange", None, None)
    # Bob (number only) with a legacy json attachment and a mention of Alice
    _message(db, "m9", "c-bob", 9_000, "incoming", "￼ see this", None,
             bodyRanges=[{"start": 0, "length": 1, "mentionAci": ALICE}],
             attachments=[{"path": "cd/legacy", "contentType": "text/plain", "fileName": "notes.txt", "size": 17}])
    db.execute("UPDATE messages SET source = ? WHERE id = 'm9'", (BOB_NUMBER,))
    # Group: Bob (no ACI) can't be keyed by ACI, Alice posts; a v1 group's message is skipped
    _message(db, "m10", "c-group", 10_000, "incoming", "gm crew", ALICE, expireTimer=3600)
    _message(db, "m11", "c-v1", 11_000, "incoming", "ancient", ALICE)
    # Note to Self
    _message(db, "m12", "c-me", 12_000, "outgoing", "remember milk", None)
    db.commit()
    db.close()

    plain = _wal_mode(plain_path.read_bytes())
    plain_path.unlink()
    (root / "sql" / "db.sqlite").write_bytes(_encrypt_db(plain))
    if legacy_key:
        cfg = {"key": KEY.hex()}
    else:
        cfg = {"encryptedKey": _safe_storage(KEY.hex(), password).hex()}
    (root / "config.json").write_text(json.dumps(cfg))
    return {"photo": photo, "long_text": long_text, "password": password}


# ── Crypto / reader ──────────────────────────────────────────────────────────

def test_key_from_safe_storage_and_legacy(tmp_path):
    facts = build_desktop(tmp_path / "Signal")
    install = desktop.detect(tmp_path / "Signal")
    assert desktop.database_key(install, keychain=lambda: facts["password"]) == KEY
    with pytest.raises(DesktopImportError, match="doesn't match"):
        desktop.database_key(install, keychain=lambda: b"wrong")
    legacy = tmp_path / "Legacy"
    build_desktop(legacy, legacy_key=True)
    assert desktop.database_key(desktop.detect(legacy), keychain=lambda: pytest.fail("no Keychain for a plain key")) == KEY
    assert desktop.detect(tmp_path / "nowhere") is None


def test_keychain_denied_is_a_clear_error():
    class Done:
        returncode, stdout, stderr = 51, b"", b"User canceled the operation."

    with patch.object(desktop.subprocess, "run", return_value=Done()):
        with pytest.raises(DesktopImportError, match="denied"):
            desktop._keychain_password()


def test_decrypts_database_and_replays_only_committed_wal_frames(tmp_path):
    v1 = tmp_path / "v1.sqlite"
    _empty_db(v1)
    with sqlite3.connect(v1) as db:
        db.execute("CREATE TABLE t (x TEXT)")
        db.executemany("INSERT INTO t VALUES (?)", [(f"row {i}",) for i in range(3)])
    v2 = tmp_path / "v2.sqlite"
    v2.write_bytes(v1.read_bytes())
    with sqlite3.connect(v2) as db:
        db.executemany("INSERT INTO t VALUES (?)", [("x" * 500,) for _ in range(30)])  # grows the file
    old, new = v1.read_bytes(), v2.read_bytes()
    assert old[20] == 80 and new[20] == 80  # SQLite kept the reserve
    changed = [n for n in range(1, len(new) // PAGE + 1) if new[(n - 1) * PAGE:n * PAGE] != old[(n - 1) * PAGE:n * PAGE]]
    frames = [(n, new[(n - 1) * PAGE:n * PAGE], 0) for n in changed]
    frames[-1] = (frames[-1][0], frames[-1][1], len(new) // PAGE)  # the commit frame
    frames.append((1, b"\x01" * PAGE, 0))  # written, never committed: must be ignored

    enc = tmp_path / "enc.sqlite"
    enc.write_bytes(_encrypt_db(_wal_mode(old)))
    wal = tmp_path / "enc.sqlite-wal"
    wal.write_bytes(_wal(frames))
    out = tmp_path / "plain.sqlite"
    desktop.decrypt_database(enc, wal, KEY, out)
    # Opens read-only with no -wal / -shm beside it (the WAL is merged in)
    with sqlite3.connect(f"file:{out}?mode=ro", uri=True) as db:
        assert db.execute("SELECT COUNT(*) FROM t").fetchone() == (33,)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    # Without the WAL: the checkpointed state
    desktop.decrypt_database(enc, None, KEY, out)
    with sqlite3.connect(out) as db:
        assert db.execute("SELECT COUNT(*) FROM t").fetchone() == (3,)
    with pytest.raises(DesktopImportError, match="wrong key"):
        desktop.decrypt_database(enc, None, b"\x00" * 32, out)


def test_attachments_decrypt_and_legacy_files_pass_through(tmp_path):
    keys = os.urandom(64)
    f = tmp_path / "a"
    f.write_bytes(_encrypt_attachment(b"hello world", keys))
    assert desktop.decrypt_attachment_file(f, base64.b64encode(keys).decode(), 11, 2) == b"hello world"
    tampered = bytearray(f.read_bytes())
    tampered[20] ^= 1
    f.write_bytes(bytes(tampered))
    with pytest.raises(DesktopImportError, match="integrity"):
        desktop.decrypt_attachment_file(f, base64.b64encode(keys).decode(), 11, 2)
    g = tmp_path / "b"
    g.write_bytes(b"plain")
    assert desktop.decrypt_attachment_file(g, None, None, None) == b"plain"


def test_reader_maps_conversations_and_messages(tmp_path):
    facts = build_desktop(tmp_path / "Signal")
    install = desktop.detect(tmp_path / "Signal")
    work = tmp_path / "work"
    work.mkdir()
    plain = desktop.snapshot(install, KEY, work)
    reader = DesktopReader(plain, install.attachments)
    assert reader.self_aci() == ME and reader.self_number() == ME_NUMBER
    convs = {c.id: c for c in reader.conversations()}
    assert (convs["c-alice"].kind, convs["c-alice"].key, convs["c-alice"].title) == ("direct", ALICE, "Alice Contact")
    assert convs["c-bob"].key == BOB_NUMBER and convs["c-me"].is_self
    assert (convs["c-group"].kind, convs["c-group"].key) == ("group", GROUP_ID)
    assert "c-v1" not in convs  # GV1 groups can't be matched to signal-cli's

    alice = list(reader.messages(convs["c-alice"]))
    assert [m.sent_at for m in alice] == [1_000, 2_000, 3_000, 4_000, 5_000]  # story / deleted / keychange skipped
    assert alice[0].reactions == {DesktopReader.SELF: "❤️", ALICE: "👍"}
    assert alice[1].outgoing and alice[1].quote == (ALICE, 1_000)
    assert [a.file_name for a in alice[2].attachments] == ["beach.jpg", "gone.pdf"]
    assert reader.read_attachment(alice[2].attachments[0]) == facts["photo"]
    assert alice[3].text == facts["long_text"] and alice[3].attachments == []
    assert alice[4].is_edited
    [bob] = reader.messages(convs["c-bob"])
    assert bob.text == "@Alice Contact see this" and bob.author.e164 == BOB_NUMBER and bob.author.aci is None
    assert reader.read_attachment(bob.attachments[0]) == b"plain legacy file"
    [gm] = reader.messages(convs["c-group"])
    assert gm.expire_seconds == 3600
    reader.close()


def test_unsupported_schema_is_refused(tmp_path):
    path = tmp_path / "x.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE conversations (id TEXT)")
    with pytest.raises(DesktopImportError, match="isn't supported yet"):
        DesktopReader(path, tmp_path)


# ── Import into Mingly ───────────────────────────────────────────────────────

class _Broadcasts(list):
    async def __call__(self, data):
        self.append(data)


async def _connector(db) -> SignalConnector:
    acc = await factories.account(db, "signal")
    acc.external_id = ME_NUMBER
    await db.commit()
    conn = SignalConnector(acc.id)
    conn._number, conn._aci = ME_NUMBER, ME
    return conn


async def _run_import(conn, root) -> desktop_import.ImportState:
    st = desktop_import.start(conn, root=root)
    task = get_running(desktop_import.JOB_KEY)
    if task is not None:
        await task
    return st


async def _messages(conv_id):
    async with AsyncSessionLocal() as s:
        return {m.external_id: m for m in (await s.execute(
            select(Message).where(Message.conversation_id == conv_id))).scalars().all()}


@pytest.mark.asyncio
async def test_import_lands_history_with_live_keys(db, tmp_path):
    facts = build_desktop(tmp_path / "Signal")
    conn = await _connector(db)
    # A message already received live (after linking), with a live reaction
    live_conv = await factories.conversation(db, ALICE, title="Alice", channel="signal",
                                             peer=await factories.identity(db, ALICE, channel="signal"))
    await crud.save_messages(db, [{
        "conversation_id": live_conv.id, "external_id": f"{ALICE}:1000", "seq": 1000, "text": "hi (live copy)",
        "date": desktop_import.utc(1000), "is_outgoing": False,
        "meta_json": json.dumps({"signal_reactions": {ALICE: "😂"}}),
    }])
    sent = _Broadcasts()
    with patch("backend.ws.manager.ws_manager.broadcast", sent), \
            patch.object(desktop, "_keychain_password", return_value=facts["password"]):
        st = await _run_import(conn, tmp_path / "Signal")
    assert st.status == "done", st.error
    assert st.files == 2 and st.files_skipped == 1  # the photo and the legacy file; the missing pdf is skipped

    async with AsyncSessionLocal() as s:
        convs = {c.external_id: c for c in (await s.execute(select(Conversation))).scalars().all()}
    assert set(convs) >= {ALICE, BOB_NUMBER, GROUP_ID, ME}
    assert convs[ALICE].id == live_conv.id  # the live conversation, not a new one
    assert convs[ME].title == "Note to Self"

    alice = await _messages(live_conv.id)
    assert set(alice) == {f"{ALICE}:{t}" for t in (1000, 3000, 4000, 5000)} | {f"{ME}:2000"}
    live = alice[f"{ALICE}:1000"]
    assert live.text == "hi (live copy)"  # live data kept
    assert json.loads(live.meta_json)["signal_reactions"] == {ALICE: "😂", ME: "❤️"}  # Desktop's added, live's kept
    assert live.my_reaction == "❤️"
    assert alice[f"{ME}:2000"].reply_to_external_id == f"{ALICE}:1000" and alice[f"{ME}:2000"].is_outgoing
    assert alice[f"{ALICE}:4000"].text == facts["long_text"] and alice[f"{ALICE}:5000"].is_edited
    async with AsyncSessionLocal() as s:
        atts = {a.filename: a for a in (await s.execute(select(MessageAttachment))).scalars().all()}
    assert (Path(config.DATA_DIR) / atts["beach.jpg"].local_path).read_bytes() == facts["photo"]
    assert atts["gone.pdf"].local_path is None
    group = await _messages(convs[GROUP_ID].id)
    assert json.loads(group[f"{ALICE}:10000"].meta_json) == {"expires_in_seconds": 3600}

    # History arrives quietly: no new_message events, notifications or unread
    assert not [e for e in sent if e.get("type") == "new_message"]
    assert all((c.unread_count or 0) == 0 for c in convs.values())
    # Identities follow live keys; Bob is by number with his phone
    async with AsyncSessionLocal() as s:
        bob = (await s.execute(select(Identity).where(Identity.external_id == BOB_NUMBER))).scalar_one()
    assert bob.phone == BOB_NUMBER and bob.channel == "signal"
    # The decrypted copy is gone; the note says history was imported
    assert list((Path(config.DATA_DIR) / "tmp").iterdir()) == []
    assert "imported from Signal Desktop" in conn.history_note() and conn.history_note() != NO_HISTORY_NOTE
    state = (await load_sync_state(conn.account_id))["desktop_import"]
    assert state["messages"] == st.messages_imported

    # Running it again changes nothing
    before = {cid: set(await _messages(cid)) for cid in (live_conv.id, convs[GROUP_ID].id)}
    with patch.object(desktop, "_keychain_password", return_value=facts["password"]):
        again = await _run_import(conn, tmp_path / "Signal")
    assert again.status == "done" and again.messages_imported == 0 and again.files == 0
    assert {cid: set(await _messages(cid)) for cid in before} == before


@pytest.mark.asyncio
async def test_import_refuses_another_account_and_cleans_up(db, tmp_path):
    facts = build_desktop(tmp_path / "Signal")
    conn = await _connector(db)
    conn._aci = "aci-someone-else"
    with patch.object(desktop, "_keychain_password", return_value=facts["password"]):
        st = await _run_import(conn, tmp_path / "Signal")
    assert st.status == "error" and "different Signal account" in st.error
    assert list((Path(config.DATA_DIR) / "tmp").iterdir()) == []


@pytest.mark.asyncio
async def test_cancel_stops_between_conversations(db, tmp_path):
    facts = build_desktop(tmp_path / "Signal")
    conn = await _connector(db)
    real = desktop_import._import_conversation

    async def cancel_after_first(*args):
        await real(*args)
        desktop_import.cancel()

    with patch.object(desktop, "_keychain_password", return_value=facts["password"]), \
            patch.object(desktop_import, "_import_conversation", cancel_after_first):
        st = await _run_import(conn, tmp_path / "Signal")
    assert st.status == "cancelled" and st.conversations_done == 1
    assert list((Path(config.DATA_DIR) / "tmp").iterdir()) == []


@pytest.mark.asyncio
async def test_flows_offer_import_after_linking_and_from_connections(db, tmp_path):
    facts = build_desktop(tmp_path / "Signal")
    conn = await _connector(db)
    with patch.object(desktop, "DEFAULT_ROOT", tmp_path / "Signal"):
        assert [a["id"] for a in conn.account_actions()] == ["desktop_import"]
        offer = await conn.begin_auth({"desktop_import": True})
        assert offer.kind == "fields" and offer.submit_label == "Import history" and "Signal Safe Storage" in offer.message
        with patch.object(desktop, "_keychain_password", return_value=facts["password"]):
            step = await conn.continue_auth(offer.flow_id, {})
            assert step.kind in ("progress", "done")
            task = get_running(desktop_import.JOB_KEY)
            if task is not None:
                await task
            done = await conn.continue_auth(offer.flow_id, {})
        assert done.kind == "done" and "Imported" in done.message
        assert conn.account_actions()[0]["label"] == "Import again from Signal Desktop"
        linked = await conn._desktop_offer_step("f2", linked=ME_NUMBER)
        assert linked.cancel_label == "Skip" and linked.title == "Signal linked"
    with patch.object(desktop, "DEFAULT_ROOT", tmp_path / "none"):
        assert conn.account_actions() == []


def test_progress_is_messages_done_over_total():
    st = desktop_import.ImportState(status="running", messages_total=10, messages_done=4)
    assert st.running and st.progress == 0.4


@pytest.mark.asyncio
async def test_import_survives_other_writers_while_files_decrypt(db, tmp_path):
    """Mingly keeps syncing during an import: a commit by another writer while
    attachments decrypt must not fail the import's own writes (a transaction
    that read before that commit can't write after it in WAL mode)."""
    facts = build_desktop(tmp_path / "Signal")
    conn = await _connector(db)
    other = await factories.conversation(db, "other", title="Other", channel="signal")
    real_read = DesktopReader.read_attachment
    writes = []

    def read_while_syncing(self, att):
        async def commit_elsewhere():
            async with AsyncSessionLocal() as s:
                await crud.save_messages(s, [{
                    "conversation_id": other.id, "external_id": f"live:{len(writes)}", "seq": len(writes),
                    "text": "arrived during the import", "date": desktop_import.utc(1), "is_outgoing": False,
                }])
        writes.append(asyncio.run_coroutine_threadsafe(commit_elsewhere(), loop).result(10))
        return real_read(self, att)

    loop = asyncio.get_running_loop()
    with patch.object(desktop, "_keychain_password", return_value=facts["password"]), \
            patch.object(DesktopReader, "read_attachment", read_while_syncing):
        st = await _run_import(conn, tmp_path / "Signal")
    assert st.status == "done", st.error
    assert writes and st.files == 2
