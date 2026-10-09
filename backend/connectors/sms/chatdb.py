"""Read-only access to macOS Messages' database (``~/Library/Messages/chat.db``).

The file is protected by macOS privacy controls: only an app with Full Disk
Access can open it. ``connect`` tells a missing database apart from a denied
one so sign-in can explain what to do.

The database is opened with ``mode=ro`` (never written, never copied) while
Messages keeps writing to it in WAL mode; a busy / locked database is retried
with backoff. Columns that older macOS versions lack (``attributedBody``,
``thread_originator_guid``, …) are read as NULL.

Everything here is synchronous; the connector runs it in a worker thread.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, TypeVar
from urllib.parse import quote

from backend.connectors.sms.typedstream import decode_attributed_body

T = TypeVar("T")

APPLE_EPOCH = datetime(2001, 1, 1)
# Seconds since 2001 stay below this until the year ~5100; nanosecond
# timestamps (macOS High Sierra and later) are far above it.
_NANOSECONDS_FROM = 10 ** 11
BUSY_TIMEOUT = 5.0  # sqlite's own wait for a lock, per statement
BUSY_RETRIES = 4
_IN_CHUNK = 500
_DENIED = ("unable to open database", "authorization denied", "not authorized", "operation not permitted")


def default_db_path() -> Path:
    return Path.home() / "Library" / "Messages" / "chat.db"


def default_attachments_root() -> Path:
    return Path.home() / "Library" / "Messages" / "Attachments"


def apple_time(value: Any) -> Optional[datetime]:
    """Naive UTC datetime of a Messages timestamp: seconds since 2001-01-01 on
    old macOS, nanoseconds since then on current versions. 0 / NULL → None."""
    if not value:
        return None
    v = int(value)
    if abs(v) >= _NANOSECONDS_FROM:
        return APPLE_EPOCH + timedelta(microseconds=v // 1000)
    return APPLE_EPOCH + timedelta(seconds=v)


def to_apple_ns(dt: datetime) -> int:
    delta = dt - APPLE_EPOCH
    return (delta.days * 86400 + delta.seconds) * 10 ** 9 + delta.microseconds * 1000


class ChatDbUnavailable(Exception):
    """The database can't be read. ``reason``: missing | denied | invalid | busy | error."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class RawChat:
    rowid: int
    guid: str
    chat_identifier: Optional[str]
    display_name: Optional[str]
    style: Optional[int]  # 43 = group, 45 = one-to-one
    service: Optional[str]  # iMessage | SMS | RCS
    members: list[str] = field(default_factory=list)  # handle ids (phone / email), the user excluded


@dataclass
class RawAttachment:
    rowid: int
    filename: Optional[str]  # "~/Library/Messages/Attachments/…", NULL until downloaded
    mime_type: Optional[str]
    transfer_name: Optional[str]
    total_bytes: Optional[int]


@dataclass
class RawMessage:
    rowid: int
    guid: str
    chat_id: Optional[int]
    text: Optional[str]  # ``text``, else decoded from ``attributedBody``
    handle: Optional[str]  # sender (or, for the user's own messages in a 1:1 chat, the recipient)
    date: Optional[datetime]
    is_from_me: bool
    service: Optional[str]
    associated_type: int = 0  # 2000–2006 tapback, 3000–3006 tapback removed
    associated_guid: Optional[str] = None
    associated_emoji: Optional[str] = None
    thread_originator_guid: Optional[str] = None
    item_type: int = 0  # non-zero: a group event (1 member, 2 rename, 3 left / photo)
    group_action_type: int = 0
    group_title: Optional[str] = None
    other_handle: Optional[str] = None  # the member an item_type 1 event added / removed
    is_edited: bool = False


@dataclass
class Batch:
    messages: list[RawMessage]
    chats: dict[int, RawChat]
    attachments: dict[int, list[RawAttachment]]  # by message rowid

    @property
    def last_rowid(self) -> int:
        return self.messages[-1].rowid if self.messages else 0


def _is_busy(e: sqlite3.OperationalError) -> bool:
    text = str(e).lower()
    return "locked" in text or "busy" in text


def _retry(fn: Callable[[], T]) -> T:
    for attempt in range(BUSY_RETRIES + 1):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if not _is_busy(e):
                raise
            if attempt == BUSY_RETRIES:
                raise ChatDbUnavailable("busy", "Messages' database stayed locked; will try again.") from e
            time.sleep(0.2 * 2 ** attempt)
    raise AssertionError("unreachable")


def _denied(path: Path) -> ChatDbUnavailable:
    return ChatDbUnavailable(
        "denied", f"Mingly isn't allowed to read {path} (Full Disk Access is required).",
    )


def _parent_denied(path: Path) -> bool:
    try:
        os.listdir(path.parent)
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def connect(path: Path) -> sqlite3.Connection:
    """Open ``path`` read-only and check it is a Messages database."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(16)
    except FileNotFoundError:
        if _parent_denied(path):
            raise _denied(path)
        raise ChatDbUnavailable(
            "missing", f"No Messages database at {path}. Sign in to Messages on this Mac first.",
        )
    except PermissionError:
        raise _denied(path)
    except OSError as e:
        raise ChatDbUnavailable("error", f"Could not open {path}: {e}")
    if not header.startswith(b"SQLite format 3"):
        raise ChatDbUnavailable("invalid", f"{path} is not a Messages database.")
    conn = sqlite3.connect(
        f"file:{quote(str(path))}?mode=ro", uri=True, timeout=BUSY_TIMEOUT, check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    try:
        _retry(lambda: conn.execute("SELECT ROWID FROM message LIMIT 1").fetchall())
    except sqlite3.DatabaseError as e:
        conn.close()
        raise _unavailable(path, e) from e
    except BaseException:
        conn.close()
        raise
    return conn


def _unavailable(path: Path, e: sqlite3.DatabaseError) -> ChatDbUnavailable:
    text = str(e).lower()
    if any(d in text for d in _DENIED):
        return _denied(path)
    if "no such table" in text or "not a database" in text:
        return ChatDbUnavailable("invalid", f"{path} is not a Messages database.")
    return ChatDbUnavailable("error", f"Could not read {path}: {e}")


def _chunks(items: list, size: int = _IN_CHUNK) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class ChatDb:
    """One read session: ``with ChatDb(path) as db: db.read_after(...)``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._columns: dict[str, set[str]] = {}

    def __enter__(self) -> "ChatDb":
        self._conn = connect(self.path)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        assert self._conn is not None, "use ChatDb as a context manager"
        conn, args = self._conn, tuple(params)
        try:
            return _retry(lambda: conn.execute(sql, args).fetchall())
        except sqlite3.DatabaseError as e:  # e.g. access revoked while running
            raise _unavailable(self.path, e) from e

    def _has(self, table: str, column: str) -> bool:
        if table not in self._columns:
            self._columns[table] = {r["name"] for r in self._query(f"PRAGMA table_info({table})")}
        return column in self._columns[table]

    def _col(self, table: str, alias: str, column: str) -> str:
        return f"{alias}.{column}" if self._has(table, column) else f"NULL AS {column}"

    # -- reads -------------------------------------------------------------------

    def max_rowid(self) -> int:
        return int(self._query("SELECT COALESCE(MAX(ROWID), 0) AS n FROM message")[0]["n"])

    def self_handle(self) -> Optional[str]:
        """The user's own phone / email as Messages addresses chats from it
        (the most used ``chat.last_addressed_handle``)."""
        if not self._has("chat", "last_addressed_handle"):
            return None
        rows = self._query(
            "SELECT last_addressed_handle AS h FROM chat WHERE last_addressed_handle IS NOT NULL "
            "AND last_addressed_handle != '' GROUP BY last_addressed_handle ORDER BY COUNT(*) DESC LIMIT 1"
        )
        return rows[0]["h"] if rows else None

    def read_after(self, rowid: int, limit: int, since: Optional[datetime] = None) -> Batch:
        """Up to ``limit`` messages with ROWID > ``rowid`` (and, with
        ``since``, dated at or after it), oldest first, with their chats and
        attachments."""
        cols = ", ".join(self._col("message", "m", c) for c in (
            "attributedBody", "cache_has_attachments", "associated_message_type", "associated_message_guid",
            "associated_message_emoji", "thread_originator_guid", "item_type", "group_action_type",
            "group_title", "other_handle", "service", "date_edited",
        ))
        sql = (
            f"SELECT m.ROWID AS rowid, m.guid, m.text, m.handle_id, m.date, m.is_from_me, {cols}, "
            "(SELECT MIN(cmj.chat_id) FROM chat_message_join cmj WHERE cmj.message_id = m.ROWID) AS chat_id "
            "FROM message m WHERE m.ROWID > ?"
        )
        params: list[Any] = [rowid]
        if since is not None:
            # Both timestamp units, so one bound fits old and new rows.
            sql += " AND ((m.date >= ? AND m.date >= ?) OR (m.date < ? AND m.date >= ?))"
            seconds = int((since - APPLE_EPOCH).total_seconds())
            params += [_NANOSECONDS_FROM, to_apple_ns(since), _NANOSECONDS_FROM, seconds]
        sql += " ORDER BY m.ROWID LIMIT ?"
        params.append(limit)
        rows = self._query(sql, params)

        handle_ids = {r["handle_id"] for r in rows if r["handle_id"]} | {r["other_handle"] for r in rows if r["other_handle"]}
        chat_ids = sorted({r["chat_id"] for r in rows if r["chat_id"] is not None})
        chats = self._chats(chat_ids)
        handles = self._handles(sorted(handle_ids))
        messages = [self._message(r, handles) for r in rows]
        with_files = [r["rowid"] for r in rows if r["cache_has_attachments"] or r["cache_has_attachments"] is None]
        return Batch(messages, chats, self._attachments(with_files))

    def _message(self, r: sqlite3.Row, handles: dict[int, str]) -> RawMessage:
        text = r["text"]
        if text is None and r["attributedBody"] is not None:
            text = decode_attributed_body(r["attributedBody"])
        return RawMessage(
            rowid=r["rowid"],
            guid=r["guid"],
            chat_id=r["chat_id"],
            text=text,
            handle=handles.get(r["handle_id"]) if r["handle_id"] else None,
            date=apple_time(r["date"]),
            is_from_me=bool(r["is_from_me"]),
            service=r["service"],
            associated_type=int(r["associated_message_type"] or 0),
            associated_guid=r["associated_message_guid"],
            associated_emoji=r["associated_message_emoji"],
            thread_originator_guid=r["thread_originator_guid"] or None,
            item_type=int(r["item_type"] or 0),
            group_action_type=int(r["group_action_type"] or 0),
            group_title=r["group_title"],
            other_handle=handles.get(r["other_handle"]) if r["other_handle"] else None,
            is_edited=bool(r["date_edited"]),
        )

    def _handles(self, ids: list[int]) -> dict[int, str]:
        out: dict[int, str] = {}
        for chunk in _chunks(ids):
            marks = ",".join("?" * len(chunk))
            for r in self._query(f"SELECT ROWID AS rowid, id FROM handle WHERE ROWID IN ({marks})", chunk):
                out[r["rowid"]] = r["id"]
        return out

    def _chats(self, ids: list[int]) -> dict[int, RawChat]:
        out: dict[int, RawChat] = {}
        cols = ", ".join(self._col("chat", "c", col) for col in ("display_name", "style", "service_name"))
        for chunk in _chunks(ids):
            marks = ",".join("?" * len(chunk))
            for r in self._query(
                f"SELECT c.ROWID AS rowid, c.guid, c.chat_identifier, {cols} FROM chat c WHERE c.ROWID IN ({marks})",
                chunk,
            ):
                out[r["rowid"]] = RawChat(
                    r["rowid"], r["guid"], r["chat_identifier"], r["display_name"] or None, r["style"],
                    r["service_name"],
                )
            for r in self._query(
                "SELECT chj.chat_id, h.id FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id "
                f"WHERE chj.chat_id IN ({marks}) ORDER BY chj.chat_id, h.ROWID",
                chunk,
            ):
                if r["id"] not in out[r["chat_id"]].members:
                    out[r["chat_id"]].members.append(r["id"])
        return out

    def _attachments(self, message_ids: list[int]) -> dict[int, list[RawAttachment]]:
        out: dict[int, list[RawAttachment]] = {}
        cols = ", ".join(self._col("attachment", "a", c) for c in ("mime_type", "transfer_name", "total_bytes"))
        for chunk in _chunks(message_ids):
            marks = ",".join("?" * len(chunk))
            for r in self._query(
                f"SELECT maj.message_id, a.ROWID AS rowid, a.filename, {cols} FROM message_attachment_join maj "
                f"JOIN attachment a ON a.ROWID = maj.attachment_id WHERE maj.message_id IN ({marks}) "
                "ORDER BY maj.message_id, a.ROWID",
                chunk,
            ):
                out.setdefault(r["message_id"], []).append(RawAttachment(
                    r["rowid"], r["filename"], r["mime_type"], r["transfer_name"], r["total_bytes"],
                ))
        return out


def probe(path: Path) -> Optional[str]:
    """Check ``path`` can be read (raises ChatDbUnavailable otherwise);
    returns the user's own handle when Messages reveals it."""
    with ChatDb(path) as db:
        return db.self_handle()


def read_after(path: Path, rowid: int, limit: int, since: Optional[datetime] = None) -> Batch:
    with ChatDb(path) as db:
        return db.read_after(rowid, limit, since)


def max_rowid(path: Path) -> int:
    with ChatDb(path) as db:
        return db.max_rowid()
