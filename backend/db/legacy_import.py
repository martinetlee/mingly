"""Import a generation-1 database (the Telegram-only schema, keyed by
Telegram ids) into a fresh generation-2 database.

``import_generation1`` runs at startup in place of discarding the old file:

1. The old DB is copied to ``backups/mingly-retired-gen1-<ts>.db``, the
   import's read-only source.
2. A complete generation-2 DB is built next to it (``mingly.db.importing``)
   and filled from the backup in one transaction.
3. Downloaded media and profile photos are hardlinked (copied where links
   aren't possible) into the generation-2 layout under ``media.importing/``.
4. The result is verified: per-table counts, no foreign-key violations.
5. Swap, files first and the DB last: the old ``media/`` and
   ``profile_photos/`` move to ``backups/retired-gen1-<ts>/``, the staged
   tree becomes ``media/``, the new DB replaces ``mingly.db``.

Any failure before the DB swap leaves ``mingly.db`` as it was (still
generation 1), so the next start retries. A journal file records the swap
in progress, so a retry after a crash mid-swap reads the old files from
where they were moved.

Generation-1 integer ids are kept for rows that had them (conversations
from ``chats``, messages, tags, organizations, workspaces, notes…); people
and identities are new. Rows written the way the generation-2 Telegram code
writes them (``backend/telegram/ids.py``, ``convert.py``) so sync updates
them in place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import shutil
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from sqlalchemy.ext.asyncio import create_async_engine

from backend import media_paths
from backend.db.crud.identities import normalize_phone
from backend.db.models import ATTACHMENT_KINDS

logger = logging.getLogger(__name__)

TELEGRAM = "telegram"
_KIND = {"private": "direct", "group": "group", "channel": "channel"}
_JOURNAL = "gen1-import.json"


class LegacyImportError(RuntimeError):
    """The generation-1 database could not be imported; it was left as is."""


@dataclass
class ImportReport:
    backup: Optional[Path] = None
    counts: dict[str, int] = field(default_factory=dict)
    skipped: Counter = field(default_factory=Counter)
    files_staged: int = 0
    files_missing: int = 0
    avatars_staged: int = 0

    def skip(self, what: str, detail: Any = None) -> None:
        self.skipped[what] += 1
        logger.debug("gen-1 import skipped %s: %r", what, detail)


# ── Entry point ──────────────────────────────────────────────────────────────

async def import_generation1(db_path: Path, *, create_schema: Callable, now: Optional[datetime] = None) -> ImportReport:
    """Replace the generation-1 DB at ``db_path`` with its generation-2
    import. ``create_schema(engine)`` builds a fresh generation-2 schema
    (``startup.create_fresh_schema``)."""
    now = now or datetime.utcnow()
    stamp = f"{now:%Y%m%d%H%M%S}"
    data_dir = db_path.parent
    backups = data_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    tmp_db = db_path.with_name(db_path.name + ".importing")
    staging = data_dir / "media.importing"
    journal = data_dir / _JOURNAL

    # Where the old files are: in place, or moved by an interrupted swap
    sources = _old_file_sources(data_dir, journal)
    _remove_db(tmp_db)
    if staging.exists():
        shutil.rmtree(staging)

    report = ImportReport()
    report.backup = backups / f"{db_path.stem}-retired-gen1-{stamp}.db"
    _sqlite_copy(db_path, report.backup)
    try:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_db}")
        try:
            await create_schema(engine)
        finally:
            await engine.dispose()
        await asyncio.to_thread(_import, report.backup, tmp_db, sources, staging, report, now)
    except BaseException:
        _remove_db(tmp_db)
        shutil.rmtree(staging, ignore_errors=True)
        raise

    _swap(db_path, tmp_db, staging, sources, backups / f"retired-gen1-{stamp}", journal)
    logger.warning(
        "Imported the generation-1 database (backup: %s): %s; %d media files, %d avatars%s",
        report.backup, ", ".join(f"{k} {v}" for k, v in report.counts.items()),
        report.files_staged, report.avatars_staged,
        f"; skipped {dict(report.skipped)}" if report.skipped else "",
    )
    return report


# ── Files ────────────────────────────────────────────────────────────────────

@dataclass
class _Sources:
    media: Optional[Path]           # the generation-1 media/ tree
    profile_photos: Optional[Path]
    in_place: bool                  # still under data_dir (not moved by a swap)


def _old_file_sources(data_dir: Path, journal: Path) -> _Sources:
    if journal.exists():
        retired = Path(json.loads(journal.read_text())["retired"])
        # The interrupted swap may already have put a staged tree at media/:
        # it is links into the retired files, safe to drop.
        if (retired / "media").exists() and (data_dir / "media").exists():
            shutil.rmtree(data_dir / "media")
        return _Sources(_dir_or_none(retired / "media"), _dir_or_none(retired / "profile_photos"), in_place=False)
    return _Sources(_dir_or_none(data_dir / "media"), _dir_or_none(data_dir / "profile_photos"), in_place=True)


def _dir_or_none(p: Path) -> Optional[Path]:
    return p if p.is_dir() else None


def _link(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def _swap(db_path: Path, tmp_db: Path, staging: Path, sources: _Sources, retired: Path, journal: Path) -> None:
    data_dir = db_path.parent
    if sources.in_place:
        retired.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({"retired": str(retired)}))
        for name in ("media", "profile_photos"):
            if (data_dir / name).exists():
                os.replace(data_dir / name, retired / name)
    elif (data_dir / "media").exists():
        shutil.rmtree(data_dir / "media")
    if staging.exists():
        os.replace(staging, data_dir / "media")
    else:
        (data_dir / "media").mkdir(exist_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    os.replace(tmp_db, db_path)
    journal.unlink(missing_ok=True)


def _sqlite_copy(src: Path, dest: Path) -> None:
    with closing(sqlite3.connect(str(src))) as source, closing(sqlite3.connect(str(dest))) as target:
        source.backup(target)


def _remove_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


# ── The import ───────────────────────────────────────────────────────────────

class _Old:
    """Read access to the generation-1 DB that tolerates tables and columns
    an older generation-1 build didn't have yet."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.tables = {r[0] for r in conn.execute("SELECT name FROM old.sqlite_master WHERE type = 'table'")}

    def rows(self, table: str, order: str = "") -> list[dict]:
        if table not in self.tables:
            return []
        cur = self.conn.execute(f'SELECT * FROM old."{table}" {order}')
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _insert(conn: sqlite3.Connection, table: str, rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0])
    sql = f'INSERT INTO main."{table}" ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})'
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def _full_name(first: Optional[str], last: Optional[str]) -> Optional[str]:
    name = " ".join(p for p in (first, last) if p).strip()
    return name or None


def _import(source: Path, tmp_db: Path, sources: _Sources, staging: Path, report: ImportReport, now: datetime) -> None:
    conn = sqlite3.connect(str(tmp_db), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")  # insert order isn't FK order; checked at the end
        conn.execute("ATTACH DATABASE ? AS old", (str(source),))
        conn.execute("BEGIN")
        try:
            old = _Old(conn)
            _Importer(conn, old, sources, staging, report, now).run()
            _verify(conn, old, report)
            violations = conn.execute("PRAGMA main.foreign_key_check").fetchall()
            if violations:
                raise LegacyImportError(f"Import would leave foreign-key violations: {violations[:10]}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("DETACH DATABASE old")
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        conn.close()


class _Importer:
    def __init__(self, conn: sqlite3.Connection, old: _Old, sources: _Sources, staging: Path,
                 report: ImportReport, now: datetime):
        self.conn, self.old, self.sources, self.staging, self.report, self.now = conn, old, sources, staging, report, now
        self.identity: dict[int, int] = {}   # Telegram user id → identity id
        self.person: dict[int, int] = {}     # Telegram user id → person id
        self.conv: dict[int, int] = {}       # chat (marked) id → conversation id
        self.conv_kind: dict[int, str] = {}  # conversation id → kind
        self.msg: dict[tuple[int, int], int] = {}  # (chat id, Telegram message id) → message id
        self.contact_tg: dict[int, int] = {}  # contacts.id → Telegram user id

    def count(self, table: str, n: int) -> None:
        self.report.counts[table] = self.report.counts.get(table, 0) + n

    def run(self) -> None:
        chats = self.old.rows("chats", "ORDER BY id")
        self_chat = next((c for c in chats if c.get("is_self") and c.get("type") == "private"), None)
        self.self_id = self_chat["telegram_id"] if self_chat else None
        self._people(chats)
        account_id = self._account(self_chat)
        self._conversations(chats, account_id)
        self._messages()
        self._pinned(chats)
        self._avatars()
        self._tags_and_workspaces()
        self._organizations_and_notes()
        self._history_and_reviews()
        self._briefings_and_catchup()
        self._ai_conversations()
        self._shield()
        self._admin_log()

    # People ------------------------------------------------------------------

    def _people(self, chats: list[dict]) -> None:
        contacts = {c["telegram_id"]: c for c in self.old.rows("contacts")}
        self.contact_tg = {c["id"]: c["telegram_id"] for c in contacts.values()}
        private = {c["telegram_id"]: c for c in chats if c.get("type") == "private"}
        # Each sender's latest name (later rows overwrite earlier ones)
        latest_name = dict(self.conn.execute(
            "SELECT sender_id, sender_name FROM old.messages WHERE sender_id > 0 AND sender_name IS NOT NULL ORDER BY id"
        ).fetchall()) if "messages" in self.old.tables else {}

        user_ids: set[int] = set(contacts) | set(private)
        user_ids |= {r[0] for r in self.conn.execute("SELECT DISTINCT sender_id FROM old.messages WHERE sender_id > 0")} \
            if "messages" in self.old.tables else set()
        for table, col in (("person_notes", "telegram_id"), ("contact_positions", "telegram_id"),
                           ("scam_flags", "telegram_id"), ("scam_reports", "reported_telegram_id"),
                           ("scam_feedback", "related_flag_telegram_id"), ("admin_action_log", "target_user_id")):
            user_ids |= {r[col] for r in self.old.rows(table) if isinstance(r.get(col), int) and r[col] > 0}

        people, identities, points = [], [], []
        for n, uid in enumerate(sorted(user_ids), start=1):
            c, chat = contacts.get(uid, {}), private.get(uid, {})
            first, last = c.get("first_name"), c.get("last_name")
            display = _full_name(first, last) or chat.get("name") or latest_name.get(uid) \
                or c.get("username") or chat.get("username")
            is_self = uid == self.self_id
            people.append({
                "id": n, "display_name": None,
                "twitter_handle": c.get("twitter_handle"), "linkedin_url": c.get("linkedin_url"),
                "notes": c.get("notes"), "is_important": bool(c.get("is_important")), "is_self": is_self,
                "relationship_status": c.get("relationship_status"),
                "relationship_status_manual": bool(c.get("relationship_status_manual")),
                "last_interaction_at": c.get("last_interaction_at"), "last_outgoing_at": c.get("last_outgoing_at"),
                "last_incoming_at": c.get("last_incoming_at"),
                "created_at": c.get("created_at") or chat.get("created_at") or self.now,
                "updated_at": self.now,
            })
            phone = c.get("phone")
            identities.append({
                "id": n, "person_id": n, "channel": TELEGRAM, "external_id": str(uid),
                "display_name": display, "first_name": first, "last_name": last,
                "username": c.get("username") or chat.get("username"), "phone": phone, "bio": None,
                "avatar_path": None, "is_contact": False, "is_bot": bool(chat.get("is_bot")),
                "is_deleted": bool(chat.get("is_deleted_account")), "is_self": is_self,
                "synced_at": c.get("synced_at"), "created_at": c.get("created_at") or self.now, "updated_at": self.now,
            })
            e164 = normalize_phone(phone)
            if e164:
                points.append({"identity_id": n, "kind": "phone", "value": e164})
            self.identity[uid] = self.person[uid] = n
        self.count("people", _insert(self.conn, "people", people))
        self.count("identities", _insert(self.conn, "identities", identities))
        _insert(self.conn, "contact_points", points)

    def _account(self, self_chat: Optional[dict]) -> int:
        label = None
        if self_chat:
            label = f"@{self_chat['username']}" if self_chat.get("username") else self_chat.get("name")
        cur = self.conn.execute(
            "INSERT INTO main.accounts (channel, external_id, label, self_identity_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (TELEGRAM, str(self.self_id) if self.self_id else None, label,
             self.identity.get(self.self_id) if self.self_id else None, self.now),
        )
        return int(cur.lastrowid)

    # Conversations and messages ---------------------------------------------

    def _conversations(self, chats: list[dict], account_id: int) -> None:
        rows = []
        for c in chats:
            kind = _KIND.get(c.get("type") or "", "group")
            tg = c["telegram_id"]
            rows.append({
                "id": c["id"], "account_id": account_id, "external_id": str(tg), "kind": kind,
                "title": c.get("name"), "username": c.get("username"), "avatar_path": None,
                "peer_identity_id": self.identity.get(tg) if kind == "direct" else None,
                **{k: bool(c.get(k)) for k in (
                    "is_monitored", "is_hidden", "is_briefing", "is_tracked", "is_forum", "is_pinned", "is_archived",
                    "is_deactivated", "is_bot", "is_deleted_account", "is_self", "is_muted", "is_marked_unread")},
                "slow_mode_seconds": c.get("slow_mode_seconds") or 0, "muted_until": c.get("muted_until"),
                "unread_count": c.get("unread_count") or 0, "members_count": c.get("members_count"),
                "pinned_message_id": None, "last_message_at": c.get("last_message_date"),
                "last_message_text": c.get("last_message_text"), "last_message_sender": c.get("last_message_sender"),
                "last_activity_at": c.get("last_activity_at"), "last_synced_at": c.get("last_synced_at"),
                "labels_json": None, "created_at": c.get("created_at") or self.now,
            })
            self.conv[tg] = c["id"]
            self.conv_kind[c["id"]] = kind
        self.count("conversations", _insert(self.conn, "conversations", rows))

    def _messages(self) -> None:
        if "messages" not in self.old.tables:
            return
        cur = self.conn.execute("SELECT * FROM old.messages ORDER BY id")
        cols = [d[0] for d in cur.description]
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            messages, attachments = [], []
            for raw in batch:
                m = dict(zip(cols, raw))
                conv_id = self.conv.get(m["chat_id"])
                if conv_id is None:
                    self.report.skip("message without chat", m["id"])
                    continue
                self.msg[(m["chat_id"], m["tg_message_id"])] = m["id"]
                messages.append(self._message_row(m, conv_id))
                if m.get("media_type"):
                    attachments.append(self._attachment_row(m, conv_id))
                elif m.get("media_path"):
                    # A file without a media type (an old link-preview image):
                    # generation 1 never showed it; it stays in the retired files.
                    self.report.skip("file without media type", m["media_path"])
            self.count("messages", _insert(self.conn, "messages", messages))
            self.count("message_attachments", _insert(self.conn, "message_attachments", attachments))

    def _message_row(self, m: dict, conv_id: int) -> dict:
        sender = m.get("sender_id")
        meta_json = m.get("meta_json")
        # Posted as a channel or chat: no identity, the peer kept for Shield
        # (as convert.message_to_row does; broadcast posts carry no from_id)
        if isinstance(sender, int) and sender < 0 and self.conv_kind[conv_id] != "channel":
            meta = json.loads(meta_json) if meta_json else {}
            meta.setdefault("sender_peer", sender)
            meta_json = json.dumps(meta, ensure_ascii=False)
        reply = m.get("reply_to_tg_message_id")
        if reply is None:
            reply = m.get("reply_to_telegram_id")
        return {
            "id": m["id"], "conversation_id": conv_id, "external_id": str(m["tg_message_id"]),
            "seq": m["tg_message_id"],
            "sender_identity_id": self.identity.get(sender) if isinstance(sender, int) and sender > 0 else None,
            "sender_name": m.get("sender_name"), "subject": None, "text": m.get("text"),
            "text_html": m.get("text_html"), "reactions_json": m.get("reactions_json"), "date": m["date"],
            "is_outgoing": bool(m.get("is_outgoing")), "importance_score": m.get("importance_score"),
            "importance_reason": m.get("importance_reason"), "user_flagged": bool(m.get("user_flagged")),
            "reply_to_external_id": str(reply) if reply is not None else None,
            "my_reaction": m.get("my_reaction"), "topic_id": m.get("topic_id"), "is_pinned": bool(m.get("is_pinned")),
            "grouped_id": m.get("grouped_id"), "delivery_state": m.get("delivery_state") or "sent",
            "is_edited": bool(m.get("is_edited")),
            "scam_score": m.get("scam_score"), "scam_reason": m.get("scam_reason"),
            "scam_llm_prompt": m.get("scam_llm_prompt"), "scam_llm_response": m.get("scam_llm_response"),
            "scam_category": m.get("scam_category"), "scam_trace_json": m.get("scam_trace_json"),
            "scam_synced_to_cloud": bool(m.get("scam_synced_to_cloud")),
            "scam_manual_verdict": m.get("scam_manual_verdict"), "scam_status": m.get("scam_status"),
            "scam_error": m.get("scam_error"), "scam_attempts": m.get("scam_attempts") or 0,
            "scam_next_retry_at": m.get("scam_next_retry_at"), "scam_updated_at": m.get("scam_updated_at"),
            "scam_synced_at": m.get("scam_synced_at"), "kind": m.get("kind") or "message", "meta_json": meta_json,
            "fwd_from_name": m.get("fwd_from_name"), "created_at": m.get("created_at") or self.now,
        }

    def _attachment_row(self, m: dict, conv_id: int) -> dict:
        kind = m["media_type"] if m["media_type"] in ATTACHMENT_KINDS else "document"
        filename = m.get("media_filename")
        local_path = None
        src = self._old_media_file(m.get("media_path"))
        if src is not None:
            # Attachment id = message id (at most one per Telegram message)
            dest = media_paths.attachment_file(conv_id, m["id"], src.suffix.lower(), media_dir=self.staging)
            _link(src, dest)
            local_path = str(Path("media") / dest.relative_to(self.staging))
            self.report.files_staged += 1
        elif m.get("media_path"):
            self.report.files_missing += 1
        mime = "image/jpeg" if kind == "photo" else mimetypes.guess_type(filename or m.get("media_path") or "")[0]
        return {
            "id": m["id"], "message_id": m["id"], "position": 0, "kind": kind, "filename": filename,
            "mime_type": mime, "size": m.get("media_size"), "local_path": local_path, "external_ref": None,
            "created_at": m.get("created_at") or self.now,
        }

    def _old_media_file(self, media_path: Optional[str]) -> Optional[Path]:
        """``media/<chat>/<msg><ext>`` (relative to the old DATA_DIR) → the file, if present."""
        if not media_path or self.sources.media is None:
            return None
        parts = Path(media_path).parts
        if not parts or parts[0] != "media" or ".." in parts:
            return None
        src = self.sources.media.joinpath(*parts[1:])
        return src if src.is_file() else None

    def _pinned(self, chats: list[dict]) -> None:
        updates = []
        for c in chats:
            pinned = c.get("pinned_message_id")
            if pinned is not None:
                mid = self.msg.get((c["telegram_id"], pinned))
                if mid is not None:
                    updates.append((mid, c["id"]))
        self.conn.executemany("UPDATE main.conversations SET pinned_message_id = ? WHERE id = ?", updates)

    def _avatars(self) -> None:
        root = self.sources.profile_photos
        if root is None:
            return
        for f in root.glob("*.jpg"):
            try:
                peer = int(f.stem)
            except ValueError:
                continue
            if peer > 0 and peer in self.identity:
                dest = media_paths.identity_avatar_file(self.identity[peer], media_dir=self.staging)
                table, key = "identities", self.identity[peer]
            elif peer in self.conv and self.conv_kind[self.conv[peer]] != "direct":
                dest = media_paths.conversation_avatar_file(self.conv[peer], media_dir=self.staging)
                table, key = "conversations", self.conv[peer]
            else:
                continue
            _link(f, dest)
            rel = str(Path("media") / dest.relative_to(self.staging))
            self.conn.execute(f"UPDATE main.{table} SET avatar_path = ? WHERE id = ?", (rel, key))
            self.report.avatars_staged += 1

    # Tags, workspaces ---------------------------------------------------------

    def _tags_and_workspaces(self) -> None:
        tags = self.old.rows("tags", "ORDER BY id")
        if tags:
            # The generation-1 tags are the full set (its defaults included,
            # minus any the user deleted), so they replace the seeded ones.
            self.conn.execute("DELETE FROM main.tags")
            self.count("tags", _insert(self.conn, "tags", [{
                "id": t["id"], "name": t["name"], "color": t.get("color") or "slate", "category": t.get("category"),
                "is_default": bool(t.get("is_default")), "created_at": t.get("created_at") or self.now,
            } for t in tags]))
        self.count("conversation_tags", _insert(self.conn, "conversation_tags", self._by_chat(
            "chat_tags", "chat_id", lambda r, cid: {"id": r["id"], "conversation_id": cid, "tag_id": r["tag_id"]})))
        self.count("workspaces", _insert(self.conn, "workspaces", [{
            "id": w["id"], "name": w["name"], "emoji": w.get("emoji"), "color": w.get("color") or "indigo",
            "match_mode": w.get("match_mode") or "any", "view_mode": w.get("view_mode") or "list",
            "position": w.get("position") or 0, "board_watermark": w.get("board_watermark"),
            "created_at": w.get("created_at") or self.now,
        } for w in self.old.rows("workspaces")]))
        self.count("workspace_tags", _insert(self.conn, "workspace_tags", [{
            "id": r["id"], "workspace_id": r["workspace_id"], "tag_id": r["tag_id"],
            "is_exclusion": bool(r.get("is_exclusion")),
        } for r in self.old.rows("workspace_tags")]))
        self.count("workspace_boards", _insert(self.conn, "workspace_boards", [{
            "id": r["id"], "workspace_id": r["workspace_id"], "name": r["name"], "scope_tag_id": r.get("scope_tag_id"),
            "position": r.get("position") or 0,
        } for r in self.old.rows("workspace_boards")]))
        self.count("workspace_columns", _insert(self.conn, "workspace_columns", [{
            "id": r["id"], "workspace_id": r["workspace_id"], "tag_id": r["tag_id"], "board_id": r.get("board_id"),
            "label": r.get("label"), "position": r.get("position") or 0, "color": r.get("color"),
        } for r in self.old.rows("workspace_columns")]))
        self.count("workspace_conversation_overrides", _insert(self.conn, "workspace_conversation_overrides",
            self._by_chat("workspace_chat_overrides", "chat_id", lambda r, cid: {
                "id": r["id"], "workspace_id": r["workspace_id"], "conversation_id": cid,
                "needs_triage_since": r.get("needs_triage_since"), "triaged_at": r.get("triaged_at"),
            })))

    def _by_chat(self, table: str, col: str, build: Callable[[dict, int], dict]) -> list[dict]:
        out = []
        for r in self.old.rows(table):
            cid = self.conv.get(r[col])
            if cid is None:
                self.report.skip(f"{table} without chat", r[col])
                continue
            out.append(build(r, cid))
        return out

    # Organizations, positions, notes -----------------------------------------

    def _organizations_and_notes(self) -> None:
        self.count("organizations", _insert(self.conn, "organizations", [{
            "id": o["id"], "name": o["name"], "context": o.get("context"), "created_at": o.get("created_at") or self.now,
        } for o in self.old.rows("organizations")]))
        positions = []
        for p in self.old.rows("contact_positions"):
            person = self.person.get(p["telegram_id"])
            if person is None:
                self.report.skip("position without person", p["telegram_id"])
                continue
            positions.append({
                "id": p["id"], "person_id": person, "organization_id": p.get("organization_id"),
                "organization": p["organization"], "title": p.get("title"),
                "is_active": bool(p["is_active"]) if p.get("is_active") is not None else True,
                "ended_at": p.get("ended_at"), "created_at": p.get("created_at") or self.now,
            })
        self.count("person_positions", _insert(self.conn, "person_positions", positions))
        notes = []
        for n in self.old.rows("person_notes"):
            person = self.person.get(n["telegram_id"])
            if person is None:
                self.report.skip("note without person", n["id"])
                continue
            notes.append({
                "id": n["id"], "person_id": person, "kind": n["kind"], "title": n["title"], "body": n.get("body") or "",
                "citations_json": self._refs(n.get("citations_json"), "tg_message_id", "note citation"),
                "author": n.get("author") or "user", "agent_label": n.get("agent_label"),
                "created_at": n.get("created_at") or self.now, "updated_at": n.get("updated_at") or self.now,
            })
        self.count("person_notes", _insert(self.conn, "person_notes", notes))

    def _refs(self, raw: Optional[str], msg_key: str, what: str) -> Optional[str]:
        """``[{chat_id, <msg_key>}]`` → ``[{conversation_id, message_id}]``."""
        if not raw:
            return raw
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            self.report.skip(f"{what} (unreadable)", raw)
            return None
        out = []
        for item in items if isinstance(items, list) else []:
            chat, tg_msg = (item.get("chat_id"), item.get(msg_key)) if isinstance(item, dict) else (None, None)
            mid = self.msg.get((chat, tg_msg))
            if mid is None:
                self.report.skip(what, item)
                continue
            out.append({"conversation_id": self.conv[chat], "message_id": mid})
        return json.dumps(out)

    # History, reviews -----------------------------------------------------------

    def _history_and_reviews(self) -> None:
        self.count("history_coverage", _insert(self.conn, "history_coverage", self._by_chat(
            "chat_history_coverage", "chat_id", lambda r, cid: {
                "conversation_id": cid, "history_complete": bool(r.get("history_complete")),
                "oldest_seq": r.get("oldest_tg_id"), "newest_contiguous_seq": r.get("newest_contiguous_tg_id"),
                "walk_cursor_seq": r.get("walk_cursor_tg_id"), "last_walk_at": r.get("last_walk_at"),
                "last_topup_at": r.get("last_topup_at"), "error": r.get("error"), "attempts": r.get("attempts") or 0,
            })))
        self.count("agent_reviews", _insert(self.conn, "agent_reviews", self._by_chat(
            "agent_reviews", "chat_id", lambda r, cid: {
                "conversation_id": cid, "reviewed_through_seq": r["reviewed_through_tg_id"],
                "reviewed_message_count": r["reviewed_message_count"], "reviewed_at": r["reviewed_at"],
                "agent_label": r.get("agent_label"),
            })))

    # Briefings, catch-up ----------------------------------------------------------

    def _briefings_and_catchup(self) -> None:
        self.count("briefing_runs", _insert(self.conn, "briefing_runs", [{
            "id": r["id"], "generated_at": r["generated_at"], "days": r.get("days") or 1,
            "status": r.get("status") or "done", "error": r.get("error"), "finished_at": r.get("finished_at"),
        } for r in self.old.rows("briefing_runs")]))
        entries = []
        for e in self.old.rows("briefing_entries"):
            chat = e.get("chat_id")
            person = None
            conv = self.conv.get(chat)
            # An important-contact entry is about a person (conversation NULL)
            if e["category"] == "important_contact":
                person, conv = self.person.get(chat), None
            entries.append({
                "id": e["id"], "run_id": e["run_id"], "conversation_id": conv, "person_id": person,
                "conversation_title": e["chat_name"], "category": e["category"], "summary": e["summary"],
                "sources_json": self._refs(e.get("sources_json"), "telegram_id", "briefing source"),
            })
        self.count("briefing_entries", _insert(self.conn, "briefing_entries", entries))
        # catchup_summaries is an LLM cache the scheduler regenerates (and its
        # generation-1 blobs carry no message ids): not imported.
        items = []
        for r in self.old.rows("catchup_items"):
            cid = self.conv.get(r["chat_telegram_id"])
            if cid is None:
                self.report.skip("catch-up item without chat", r["chat_telegram_id"])
                continue
            tg_user = self.contact_tg.get(r.get("contact_id")) if r.get("contact_id") is not None else None
            items.append({
                "id": r["id"], "conversation_id": cid, "person_id": self.person.get(tg_user) if tg_user else None,
                "status": r.get("status") or "pending", "note": r.get("note"), "snoozed_until": r.get("snoozed_until"),
                "handled_at": r.get("handled_at"), "created_at": r.get("created_at") or self.now,
                "updated_at": r.get("updated_at") or self.now,
            })
        self.count("catchup_items", _insert(self.conn, "catchup_items", items))

    def _ai_conversations(self) -> None:
        # Generation 1 named the AI assistant's chats "conversations"
        self.count("ai_conversations", _insert(self.conn, "ai_conversations", [{
            "id": r["id"], "title": r.get("title"), "created_at": r["created_at"], "updated_at": r["updated_at"],
        } for r in self.old.rows("conversations")]))
        self.count("ai_conversation_messages", _insert(self.conn, "ai_conversation_messages", [{
            "id": r["id"], "ai_conversation_id": r["conversation_id"], "role": r["role"], "content": r["content"],
            "created_at": r["created_at"],
        } for r in self.old.rows("conversation_messages")]))

    # Shield ------------------------------------------------------------------------

    def _msg_in(self, chat: Any, tg_msg: Any) -> Optional[int]:
        return self.msg.get((chat, tg_msg)) if chat is not None and tg_msg is not None else None

    def _shield(self) -> None:
        flags = []
        for r in self.old.rows("scam_flags"):
            identity = self.identity.get(r["telegram_id"])
            if identity is None:
                self.report.skip("scam flag without identity", r["telegram_id"])
                continue
            chat = r.get("flagged_message_chat_id")
            flags.append({
                "id": r["id"], "identity_id": identity, "source": r["source"], "confidence": r.get("confidence"),
                "reason": r.get("reason"), "category": r.get("category"), "request_id": r.get("request_id"),
                "flagged_message_text": r.get("flagged_message_text"),
                "flagged_conversation_id": self.conv.get(chat) if chat is not None else None,
                "flagged_message_id": self._msg_in(chat, r.get("flagged_message_id")),
                "flagged_at": r["flagged_at"], "synced_to_cloud": bool(r.get("synced_to_cloud")),
                "status": r.get("status") or "active", "dismissed_at": r.get("dismissed_at"),
                "updated_at": r.get("updated_at"),
            })
        self.count("scam_flags", _insert(self.conn, "scam_flags", flags))

        reports = []
        for r in self.old.rows("scam_reports"):
            chat = r["reported_in_chat_id"]
            reports.append({
                "id": r["id"], "report_id": r["report_id"],
                "reported_identity_id": self.identity.get(r["reported_telegram_id"]),
                "reported_in_conversation_id": self.conv.get(chat), "chat_type": r.get("chat_type"),
                "reporter_verdict": r["reporter_verdict"], "scam_category": r.get("scam_category"),
                "custom_category": r.get("custom_category"),
                "messages_json": self._report_messages(r["messages_json"]),
                "local_detection_json": self._report_detection(r.get("local_detection_json"), chat),
                "status": r.get("status") or "submitted", "created_at": r["created_at"], "updated_at": r["updated_at"],
                "synced_at": r.get("synced_at"),
            })
        self.count("scam_reports", _insert(self.conn, "scam_reports", reports))

        self.count("scam_feedback", _insert(self.conn, "scam_feedback", [{
            "id": r["id"], "feedback_type": r["feedback_type"], "message": r["message"],
            "related_conversation_id": self.conv.get(r.get("related_chat_id")),
            "related_message_id": self._msg_in(r.get("related_chat_id"), r.get("related_message_id")),
            "related_flag_identity_id": self.identity.get(r.get("related_flag_telegram_id")),
            "status": r.get("status") or "pending", "synced_at": r.get("synced_at"), "created_at": r["created_at"],
        } for r in self.old.rows("scam_feedback")]))

        self.count("scam_dismissals", _insert(self.conn, "scam_dismissals", [
            self._dismissal(r) for r in self.old.rows("scam_dismissals")]))

        analytics = []
        for r in self.old.rows("scam_analytics"):
            row = {k: v for k, v in r.items() if k != "scans_private"}
            row["scans_direct"] = r.get("scans_private") or 0
            analytics.append(row)
        self.count("scam_analytics", _insert(self.conn, "scam_analytics", analytics))
        # scam_check_requests (in-flight cloud requests) and sync_text_repair
        # (a re-fetch queue) are transient: not imported.

    def _report_messages(self, raw: str) -> str:
        try:
            entries = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict) and "telegram_id" in e:
                e["identity_id"] = self.identity.get(e.pop("telegram_id"))
        return json.dumps(entries, default=str)

    def _report_detection(self, raw: Optional[str], chat: int) -> Optional[str]:
        if not raw:
            return raw
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        for s in data.get("scores", []) if isinstance(data, dict) else []:
            if isinstance(s, dict) and "tg_message_id" in s:
                s["message_id"] = self._msg_in(chat, s.pop("tg_message_id"))
        return json.dumps(data, default=str)

    def _dismissal(self, r: dict) -> dict:
        """Rewrite the snapshot to the generation-2 keys (scam/verdict.py),
        keeping the Telegram ids cloud contract v1 needs under ``cloud_v1``."""
        try:
            data = json.loads(r["payload_json"])
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            if r["dismissal_type"] == "scammer" and "telegram_id" in data:
                tg = data.pop("telegram_id")
                data = {"identity_id": self.identity.get(tg), **data, "cloud_v1": {"telegram_id": tg}}
            elif r["dismissal_type"] == "scan" and "chat_id" in data:
                chat, tg_msg, sender = data.pop("chat_id"), data.pop("tg_message_id", None), data.pop("sender_id", None)
                data = {
                    "conversation_id": self.conv.get(chat), "message_id": self._msg_in(chat, tg_msg),
                    "identity_id": self.identity.get(sender), **data,
                    "cloud_v1": {"chat_id": chat, "tg_message_id": tg_msg, "sender_id": sender},
                }
        return {
            "id": r["id"], "dismissal_type": r["dismissal_type"],
            "payload_json": json.dumps(data, default=str) if data is not None else r["payload_json"],
            "synced_at": r.get("synced_at"), "cloud_status": "synced" if r.get("synced_at") else "pending",
            "cloud_skipped_reason": None, "created_at": r["created_at"],
        }

    def _admin_log(self) -> None:
        self.count("admin_action_log", _insert(self.conn, "admin_action_log", self._by_chat(
            "admin_action_log", "chat_telegram_id", lambda r, cid: {
                "id": r["id"], "conversation_id": cid, "action": r["action"],
                "target_identity_id": self.identity.get(r.get("target_user_id")),
                "details": r.get("details"), "created_at": r["created_at"],
            })))


# Generation-2 table → its generation-1 source and the skip reasons that
# account for rows not carried over.
_VERIFIED = (
    ("conversations", "chats", ()),
    ("messages", "messages", ("message without chat",)),
    ("person_notes", "person_notes", ("note without person",)),
    ("person_positions", "contact_positions", ("position without person",)),
    ("organizations", "organizations", ()),
    ("tags", "tags", ()),
    ("conversation_tags", "chat_tags", ("chat_tags without chat",)),
    ("workspaces", "workspaces", ()),
    ("workspace_boards", "workspace_boards", ()),
    ("workspace_columns", "workspace_columns", ()),
    ("workspace_conversation_overrides", "workspace_chat_overrides", ("workspace_chat_overrides without chat",)),
    ("history_coverage", "chat_history_coverage", ("chat_history_coverage without chat",)),
    ("agent_reviews", "agent_reviews", ("agent_reviews without chat",)),
    ("ai_conversation_messages", "conversation_messages", ()),
)


def _verify(conn: sqlite3.Connection, old: _Old, report: ImportReport) -> None:
    """Every generation-1 row is in the new DB unless the report says why not."""
    problems = []
    for new, src, reasons in _VERIFIED:
        if src not in old.tables:
            continue
        have = conn.execute(f'SELECT COUNT(*) FROM main."{new}"').fetchone()[0]
        want = conn.execute(f'SELECT COUNT(*) FROM old."{src}"').fetchone()[0] - sum(report.skipped[r] for r in reasons)
        if have != want:
            problems.append(f"{new}: {have} rows, expected {want} (from {src})")
    if "messages" in old.tables:
        have = conn.execute("SELECT COUNT(*) FROM main.message_attachments").fetchone()[0]
        want = conn.execute(
            "SELECT COUNT(*) FROM old.messages m WHERE media_type IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM old.chats c WHERE c.telegram_id = m.chat_id)"
        ).fetchone()[0]
        if have != want:
            problems.append(f"message_attachments: {have} rows, expected {want}")
    if problems:
        raise LegacyImportError("Import verification failed: " + "; ".join(problems))
