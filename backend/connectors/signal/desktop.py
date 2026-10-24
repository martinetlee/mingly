"""Read Signal Desktop's local history on this Mac (read-only), for a
one-time import into Mingly (``desktop_import``).

Signal Desktop keeps everything in ``~/Library/Application Support/Signal``:

- ``sql/db.sqlite`` (+ ``-wal``): a SQLCipher 4 database keyed with a raw
  32-byte key (``PRAGMA key = "x'<hex>'"``). The hex key is in
  ``config.json``: ``encryptedKey`` (hex of Electron safeStorage's ``v10`` ‖
  AES-128-CBC blob, whose key is PBKDF2-HMAC-SHA1(<Keychain "Signal Safe
  Storage" password>, "saltysalt", 1003, 16), IV 16 spaces) or, on old
  installs, plaintext ``key``.
- ``attachments.noindex/<xx>/<hash>``: attachment files; ``version: 2`` ones
  are IV ‖ AES-256-CBC(plaintext + zero padding) ‖ HMAC-SHA256 under the
  attachment's ``localKey`` (aes key ‖ mac key), older ones plain.

The database is decrypted page by page (SQLCipher 4 defaults: 4096-byte
pages ending in IV(16) ‖ HMAC-SHA512(64); page 1 starts with the 16-byte KDF
salt; HMAC key = PBKDF2-HMAC-SHA512(key, salt ⊕ 0x3a, 2, 32) over
ciphertext ‖ IV ‖ page number LE32) into a plain SQLite file, with the WAL's
committed frames replayed, so no SQLCipher library is needed. Formats per
Signal-Desktop v8.32, Chromium OSCrypt (mac) and SQLCipher 4 sources.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import shutil
import sqlite3
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DEFAULT_ROOT = Path.home() / "Library" / "Application Support" / "Signal"
# Electron names the item "<app> Safe Storage"; its account has been "<app>"
# or "<app> Key" across versions, so it's looked up by service alone.
KEYCHAIN_SERVICE = "Signal Safe Storage"

PAGE_SIZE = 4096
_IV = 16
_HMAC = 64
_RESERVE = _IV + _HMAC
_SALT = 16
_SQLITE_MAGIC = b"SQLite format 3\x00"
_WAL_MAGICS = (0x377F0682, 0x377F0683)


class DesktopImportError(Exception):
    """Something the user can act on (shown as the import's error)."""


@dataclass
class DesktopInstall:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "sql" / "db.sqlite"

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def attachments(self) -> Path:
        return self.root / "attachments.noindex"

    def attachments_size(self) -> int:
        """Bytes in attachments.noindex (what an import copies at most)."""
        total = 0
        if self.attachments.is_dir():
            for p in self.attachments.rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
        return total


def detect(root: Optional[Path] = None) -> Optional[DesktopInstall]:
    """Signal Desktop's data on this Mac, or None (stat only, reads nothing)."""
    install = DesktopInstall(root or DEFAULT_ROOT)
    return install if install.db.is_file() and install.config.is_file() else None


# ── The database key ─────────────────────────────────────────────────────────

def _keychain_password() -> bytes:
    """The safeStorage password. macOS asks the user to allow access."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DesktopImportError(f"Couldn't ask the Keychain for Signal's key: {e}")
    if out.returncode != 0:
        err = (out.stderr or b"").decode(errors="replace").strip()
        if out.returncode == 44 or "could not be found" in err:
            raise DesktopImportError("Signal Desktop's key isn't in the Keychain (open Signal Desktop once, then retry)")
        raise DesktopImportError("Access to Signal's key was denied in the Keychain prompt")
    return out.stdout.strip()


def decrypt_safe_storage(blob: bytes, password: bytes) -> str:
    """Electron safeStorage (Chromium OSCrypt mac, ``v10``) → the plaintext."""
    if not blob.startswith(b"v10"):
        raise DesktopImportError("Signal Desktop's key is in an unknown format")
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    padded = dec.update(blob[3:]) + dec.finalize()
    try:
        unpadder = sym_padding.PKCS7(128).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode()
    except (ValueError, UnicodeDecodeError):
        raise DesktopImportError("Couldn't decrypt Signal Desktop's key (the Keychain password doesn't match)")


def database_key(install: DesktopInstall, keychain=None) -> bytes:
    """The 32-byte SQLCipher key (``keychain``: the safeStorage password
    source, the macOS Keychain by default)."""
    try:
        config = json.loads(install.config.read_text())
    except (OSError, ValueError) as e:
        raise DesktopImportError(f"Couldn't read Signal Desktop's settings: {e}")
    hex_key = config.get("key")
    if not hex_key and config.get("encryptedKey"):
        password = keychain() if keychain is not None else _keychain_password()
        hex_key = decrypt_safe_storage(bytes.fromhex(config["encryptedKey"]), password)
    if not isinstance(hex_key, str) or len(hex_key) != 64:
        raise DesktopImportError("Signal Desktop's database key wasn't found")
    try:
        return bytes.fromhex(hex_key)
    except ValueError:
        raise DesktopImportError("Signal Desktop's database key is malformed")


# ── SQLCipher → plain SQLite ─────────────────────────────────────────────────

def _hmac_key(key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3A for b in salt), 2, 32)


def decrypt_page(raw: bytes, pgno: int, key: bytes, hmac_key: bytes) -> bytes:
    """One SQLCipher page → the plain page (reserved bytes zeroed)."""
    offset = _SALT if pgno == 1 else 0
    end = PAGE_SIZE - _RESERVE
    ct, iv, mac = raw[offset:end], raw[end:end + _IV], raw[end + _IV:PAGE_SIZE]
    want = hmac.new(hmac_key, ct + iv + struct.pack("<I", pgno), hashlib.sha512).digest()
    if not hmac.compare_digest(want, mac):
        raise DesktopImportError(
            "Signal Desktop's database didn't decrypt (wrong key, or an unsupported database format)"
            if pgno == 1 else f"Signal Desktop's database is damaged (page {pgno})"
        )
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = dec.update(ct) + dec.finalize()
    return (_SQLITE_MAGIC if pgno == 1 else b"") + plain + b"\x00" * _RESERVE


def _wal_checksum(data: bytes, s0: int, s1: int, big_endian: bool) -> tuple[int, int]:
    fmt = (">" if big_endian else "<") + f"{len(data) // 4}I"
    words = struct.unpack(fmt, data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def committed_wal_pages(wal: bytes) -> tuple[dict[int, bytes], Optional[int]]:
    """The newest committed copy of each page in a WAL (still encrypted), and
    the database size in pages after the last commit (None: no commit)."""
    if len(wal) < 32:
        return {}, None
    magic, _version, page_size, _seq, salt1, salt2, c1, c2 = struct.unpack(">8I", wal[:32])
    if magic not in _WAL_MAGICS or page_size != PAGE_SIZE:
        return {}, None
    big = magic == 0x377F0683
    s0, s1 = _wal_checksum(wal[:24], 0, 0, big)
    if (s0, s1) != (c1, c2):
        return {}, None
    pending: dict[int, bytes] = {}
    committed: dict[int, bytes] = {}
    db_pages = None
    pos = 32
    while pos + 24 + page_size <= len(wal):
        pgno, size_after, f_salt1, f_salt2, f_c1, f_c2 = struct.unpack(">6I", wal[pos:pos + 24])
        data = wal[pos + 24:pos + 24 + page_size]
        if (f_salt1, f_salt2) != (salt1, salt2):
            break
        s0, s1 = _wal_checksum(wal[pos:pos + 8], s0, s1, big)
        s0, s1 = _wal_checksum(data, s0, s1, big)
        if (s0, s1) != (f_c1, f_c2):
            break
        pending[pgno] = data
        if size_after:
            committed.update(pending)
            pending = {}
            db_pages = size_after
        pos += 24 + page_size
    return committed, db_pages


def decrypt_database(src: Path, wal: Optional[Path], key: bytes, dest: Path) -> None:
    """Write the plain SQLite database for ``src`` (+ its WAL) to ``dest``."""
    data = src.read_bytes()
    if len(data) < PAGE_SIZE or len(data) % PAGE_SIZE:
        raise DesktopImportError("Signal Desktop's database has an unexpected size")
    if data[:16] == _SQLITE_MAGIC:
        raise DesktopImportError("Signal Desktop's database isn't encrypted as expected")
    hkey = _hmac_key(key, data[:_SALT])
    frames, db_pages = committed_wal_pages(wal.read_bytes()) if wal is not None and wal.is_file() else ({}, None)
    n_pages = db_pages or len(data) // PAGE_SIZE
    with open(dest, "wb") as out:
        for pgno in range(1, n_pages + 1):
            raw = frames.get(pgno)
            if raw is None:
                start = (pgno - 1) * PAGE_SIZE
                raw = data[start:start + PAGE_SIZE]
            if len(raw) < PAGE_SIZE:
                raise DesktopImportError(f"Signal Desktop's database is truncated (page {pgno})")
            page = decrypt_page(raw, pgno, key, hkey)
            if pgno == 1:
                # Signal Desktop runs in WAL mode (header bytes 18-19 = 2). Its WAL is merged into
                # this copy, which has none: mark it a rollback-journal database, or SQLite won't
                # open it read-only without -wal / -shm files.
                page = page[:18] + b"\x01\x01" + page[20:]
            out.write(page)


def snapshot(install: DesktopInstall, key: bytes, workdir: Path) -> Path:
    """Copy the live database files (Signal Desktop may be running) and
    decrypt them into ``workdir/plain.sqlite``."""
    shutil.copyfile(install.db, workdir / "db.sqlite")
    wal = install.db.with_name("db.sqlite-wal")
    if wal.is_file():
        shutil.copyfile(wal, workdir / "db.sqlite-wal")
    plain = workdir / "plain.sqlite"
    decrypt_database(workdir / "db.sqlite", workdir / "db.sqlite-wal", key, plain)
    return plain


# ── Attachments ──────────────────────────────────────────────────────────────

def decrypt_attachment_file(path: Path, local_key: Optional[str], size: Optional[int], version: Optional[int]) -> bytes:
    """An attachment's plaintext (v2: verified and decrypted; older: as is)."""
    raw = path.read_bytes()
    if version != 2 or not local_key:
        return raw
    keys = base64.b64decode(local_key)
    if len(keys) != 64 or len(raw) < _IV + 16 + 32:
        raise DesktopImportError("attachment key or file is malformed")
    aes_key, mac_key = keys[:32], keys[32:]
    body, mac = raw[:-32], raw[-32:]
    if not hmac.compare_digest(hmac.new(mac_key, body, hashlib.sha256).digest(), mac):
        raise DesktopImportError("attachment failed its integrity check")
    dec = Cipher(algorithms.AES(aes_key), modes.CBC(body[:_IV])).decryptor()
    padded = dec.update(body[_IV:]) + dec.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return plain[:size] if size is not None else plain


# ── Reading the decrypted database ───────────────────────────────────────────

@dataclass
class DesktopAttachment:
    path: Optional[str]  # relative to attachments.noindex; None = never downloaded
    local_key: Optional[str]
    version: Optional[int]
    size: Optional[int]
    content_type: Optional[str]
    file_name: Optional[str]
    kind: str = "attachment"  # attachment | sticker | long-message


@dataclass
class DesktopContact:
    conversation_id: str  # Desktop's own id
    aci: Optional[str]
    e164: Optional[str]
    name: Optional[str]


@dataclass
class DesktopConversation:
    id: str  # Desktop's own id
    kind: str  # direct | group
    key: str  # Mingly's external id rule: peer ACI or number, or the base64 group id
    title: Optional[str]
    peer: Optional[DesktopContact] = None
    is_self: bool = False


@dataclass
class DesktopMessage:
    sent_at: int
    outgoing: bool
    author: Optional[DesktopContact]  # None for outgoing
    text: Optional[str]
    quote: Optional[tuple[str, int]] = None  # (author key, sent_at)
    reactions: dict[str, str] = field(default_factory=dict)  # reactor key ('self' for ours) -> emoji
    is_edited: bool = False
    expire_seconds: Optional[int] = None
    sticker: Optional[dict] = None
    view_once: bool = False
    attachments: list[DesktopAttachment] = field(default_factory=list)


_MESSAGE_TYPES = ("incoming", "outgoing")
_REQUIRED = {
    "conversations": {"id", "json", "type"},
    "messages": {"id", "json", "conversationId", "sent_at", "type"},
    "items": {"id", "json"},
}


def _contact_name(c: dict) -> Optional[str]:
    full = " ".join(p for p in (c.get("profileName"), c.get("profileFamilyName")) if p).strip()
    nick = " ".join(p for p in (c.get("nicknameGivenName"), c.get("nicknameFamilyName")) if p).strip()
    return c.get("name") or nick or c.get("profileFullName") or full or c.get("username") or c.get("e164") or None


class DesktopReader:
    """The decrypted database, mapped to Mingly's Signal keys."""

    SELF = "self"  # reactor key of the user's own reactions

    def __init__(self, plain_db: Path, attachments_root: Path):
        # Opened off the event loop, then read from it (one reader, sequential use)
        self.db = sqlite3.connect(f"file:{plain_db}?mode=ro", uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.attachments_root = attachments_root
        self.columns = {
            t: {r[1] for r in self.db.execute(f"PRAGMA table_info({t})")}
            for (t,) in self.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.schema_version = self.db.execute("PRAGMA user_version").fetchone()[0]
        missing = [f"{t}.{c}" for t, cols in _REQUIRED.items() for c in cols - self.columns.get(t, set())]
        if missing:
            raise DesktopImportError(
                f"This Signal Desktop version isn't supported yet (schema {self.schema_version}; missing {', '.join(missing)})"
            )
        self._load_conversations()

    def close(self) -> None:
        self.db.close()

    def _item(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT json FROM items WHERE id = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["json"]).get("value")
        except (ValueError, AttributeError):
            return None
        return value if isinstance(value, str) else None

    def self_aci(self) -> Optional[str]:
        raw = self._item("uuid_id")
        return raw.split(".", 1)[0] if raw else None

    def self_number(self) -> Optional[str]:
        raw = self._item("number_id")
        return raw.split(".", 1)[0] if raw else None

    def _load_conversations(self) -> None:
        cols = self.columns["conversations"]
        extra = [c for c in ("serviceId", "e164", "groupId") if c in cols]
        rows = self.db.execute(f"SELECT id, json, type{''.join(', ' + c for c in extra)} FROM conversations").fetchall()
        self._convs: dict[str, dict] = {}
        self.contacts: dict[str, DesktopContact] = {}
        self.by_aci: dict[str, DesktopContact] = {}
        for r in rows:
            try:
                data = json.loads(r["json"]) if r["json"] else {}
            except ValueError:
                data = {}
            for c in extra:
                if r[c] is not None:
                    data.setdefault(c, r[c])
            data["type"] = r["type"] or data.get("type")
            self._convs[r["id"]] = data
            if data["type"] == "private":
                contact = DesktopContact(r["id"], data.get("serviceId") or None, data.get("e164") or None, _contact_name(data))
                self.contacts[r["id"]] = contact
                if contact.aci:
                    self.by_aci[contact.aci] = contact

    def contact_key(self, contact: DesktopContact) -> Optional[str]:
        return contact.aci or contact.e164

    def conversations(self) -> list[DesktopConversation]:
        me = self.self_aci()
        out = []
        for cid, data in self._convs.items():
            if data["type"] == "group":
                gid = data.get("groupId")
                if gid and data.get("groupVersion", 2) == 2:
                    out.append(DesktopConversation(cid, "group", gid, data.get("name") or None))
            elif data["type"] == "private":
                contact = self.contacts[cid]
                key = self.contact_key(contact)
                if key:
                    out.append(DesktopConversation(cid, "direct", key, contact.name, peer=contact,
                                                   is_self=bool(me) and contact.aci == me))
        return out

    def message_count(self, conversation_id: Optional[str] = None) -> int:
        q = "SELECT COUNT(*) FROM messages WHERE type IN ('incoming', 'outgoing')"
        args: tuple = ()
        if conversation_id is not None:
            q += " AND conversationId = ?"
            args = (conversation_id,)
        return self.db.execute(q, args).fetchone()[0]

    def _attachments(self, message_id: str, data: dict) -> list[DesktopAttachment]:
        out: list[DesktopAttachment] = []
        if "message_attachments" in self.columns:
            cols = self.columns["message_attachments"]
            where = "messageId = ?" + (" AND editHistoryIndex = -1" if "editHistoryIndex" in cols else "")
            order = "orderInMessage" if "orderInMessage" in cols else "rowid"
            for r in self.db.execute(f"SELECT * FROM message_attachments WHERE {where} ORDER BY {order}", (message_id,)):
                kind = r["attachmentType"] if "attachmentType" in cols else "attachment"
                if kind not in ("attachment", "sticker", "long-message"):
                    continue  # quote thumbnails, link previews, contact avatars
                out.append(DesktopAttachment(
                    path=r["path"], local_key=r["localKey"] if "localKey" in cols else None,
                    version=r["version"] if "version" in cols else None, size=r["size"],
                    content_type=r["contentType"], file_name=r["fileName"], kind=kind,
                ))
        if not out:
            for a in data.get("attachments") or []:
                if isinstance(a, dict):
                    out.append(DesktopAttachment(a.get("path"), a.get("localKey"), a.get("version"), a.get("size"),
                                                 a.get("contentType"), a.get("fileName")))
            body = data.get("bodyAttachment")
            if isinstance(body, dict):
                out.append(DesktopAttachment(body.get("path"), body.get("localKey"), body.get("version"),
                                             body.get("size"), body.get("contentType"), None, kind="long-message"))
        return out

    def read_attachment(self, att: DesktopAttachment) -> bytes:
        if not att.path:
            raise DesktopImportError("attachment was never downloaded in Signal Desktop")
        path = (self.attachments_root / att.path).resolve()
        if self.attachments_root.resolve() not in path.parents:
            raise DesktopImportError("attachment path is outside Signal Desktop's folder")
        if not path.is_file():
            raise DesktopImportError("attachment file is missing")
        return decrypt_attachment_file(path, att.local_key, att.size, att.version)

    def _text(self, data: dict, attachments: list[DesktopAttachment]) -> Optional[str]:
        text = data.get("body")
        long = next((a for a in attachments if a.kind == "long-message"), None)
        if long is not None:
            try:
                text = self.read_attachment(long).decode("utf-8", errors="replace")
            except (DesktopImportError, OSError):
                pass  # the stored preview body is still there
        ranges = [r for r in data.get("bodyRanges") or [] if isinstance(r, dict) and r.get("mentionAci")]
        if text and ranges:
            for r in sorted(ranges, key=lambda r: r.get("start", 0), reverse=True):
                start, length = r.get("start"), r.get("length", 1)
                if not isinstance(start, int) or not 0 <= start < len(text):
                    continue
                who = self.by_aci.get(r["mentionAci"])
                text = text[:start] + f"@{(who.name if who else None) or 'someone'}" + text[start + length:]
        return text or None

    def messages(self, conv: DesktopConversation) -> Iterator[DesktopMessage]:
        cols = self.columns["messages"]
        wanted = [c for c in ("body", "sourceServiceId", "source", "storyId", "isErased", "isViewOnce", "expireTimer")
                  if c in cols]
        rows = self.db.execute(
            f"SELECT id, json, sent_at, type{''.join(', ' + c for c in wanted)} FROM messages "
            "WHERE conversationId = ? AND type IN ('incoming', 'outgoing') ORDER BY sent_at, rowid",
            (conv.id,),
        )
        for r in rows:
            try:
                data = json.loads(r["json"]) if r["json"] else {}
            except ValueError:
                continue
            for c in wanted:
                if r[c] is not None:
                    data[c] = r[c]
            if data.get("storyId") or data.get("deletedForEveryone") or data.get("isErased") or r["sent_at"] is None:
                continue
            outgoing = r["type"] == "outgoing"
            author = None
            if not outgoing:
                aci, e164 = data.get("sourceServiceId"), data.get("source")
                author = self.by_aci.get(aci) if aci else None
                if author is None:
                    if not (aci or e164):
                        continue
                    author = DesktopContact("", aci, e164, None)
            attachments = self._attachments(r["id"], data)
            view_once = bool(data.get("isViewOnce"))
            yield DesktopMessage(
                sent_at=int(r["sent_at"]),
                outgoing=outgoing,
                author=author,
                text=self._text(data, attachments),
                quote=self._quote(data.get("quote")),
                reactions=self._reactions(data.get("reactions")),
                is_edited=bool(data.get("editHistory") or data.get("editMessageTimestamp")),
                expire_seconds=data.get("expireTimer") or None,
                sticker=({"packId": data["sticker"].get("packId"), "stickerId": data["sticker"].get("stickerId")}
                         if isinstance(data.get("sticker"), dict) else None),
                view_once=view_once,
                attachments=[] if view_once else [a for a in attachments if a.kind != "long-message"],
            )

    def _quote(self, quote: Any) -> Optional[tuple[str, int]]:
        if not isinstance(quote, dict) or quote.get("id") is None:
            return None
        author = quote.get("authorAci") or quote.get("author")
        return (author, int(quote["id"])) if author else None

    def _reactions(self, reactions: Any) -> dict[str, str]:
        me = self.self_aci()
        out: dict[str, str] = {}
        for r in reactions or []:
            if not isinstance(r, dict) or not r.get("emoji") or not r.get("fromId"):
                continue
            contact = self.contacts.get(r["fromId"])
            if contact is None:
                continue
            key = self.SELF if me and contact.aci == me else self.contact_key(contact)
            if key:
                out[key] = r["emoji"]
        return out
