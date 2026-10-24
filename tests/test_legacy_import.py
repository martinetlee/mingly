"""Generation-1 databases are imported at startup (backend/db/legacy_import.py):
every row lands in the generation-2 schema the way Telegram sync writes it,
files move into the generation-2 layout, and a failed or interrupted import
leaves the old database usable and retries on the next start.
"""

import json
import os
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend import media_paths
from backend.db import database, legacy_import, startup

SCHEMA = Path(__file__).parent / "fixtures" / "gen1_schema.sql"
T0 = "2026-01-01 10:00:00.000000"

ME, ANN, BOB = 1000, 1001, 1002
GROUP, FORUM, CHANNEL = -500, -1001234, -1009999


def _gen1(data_dir: Path) -> Path:
    """A generation-1 data dir: DB (real gen-1 DDL), media and profile photos."""
    path = data_dir / "mingly.db"
    db = sqlite3.connect(str(path))
    db.executescript(SCHEMA.read_text())

    def add(table: str, **row):
        db.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))

    chat_defaults = dict(is_monitored=0, is_hidden=0, is_briefing=0, is_tracked=0, is_forum=0, slow_mode_seconds=0,
                         is_pinned=0, is_archived=0, is_muted=0, is_marked_unread=0, unread_count=0, created_at=T0)
    add("chats", id=1, telegram_id=ME, name="Me", type="private", username="me", **{**chat_defaults}, is_self=1)
    add("chats", id=2, telegram_id=ANN, name="Ann Lee", type="private", username="ann",
        **{**chat_defaults, "is_tracked": 1, "unread_count": 2}, pinned_message_id=10,
        last_message_date="2026-01-02 09:00:00.000000", last_message_text="hi")
    add("chats", id=3, telegram_id=GROUP, name="Builders", type="group", **chat_defaults)
    add("chats", id=4, telegram_id=FORUM, name="Forum", type="group", **{**chat_defaults, "is_forum": 1})
    add("chats", id=5, telegram_id=CHANNEL, name="News", type="channel", **chat_defaults)
    add("contacts", id=7, telegram_id=ANN, first_name="Ann", last_name="Lee", username="ann", phone="15551234567",
        twitter_handle="annlee", notes="met at KBW", is_important=1, relationship_status="warm",
        relationship_status_manual=1, created_at=T0)

    msg_defaults = dict(is_outgoing=0, user_flagged=0, is_pinned=0, is_edited=0, scam_synced_to_cloud=0, created_at=T0)
    add("messages", id=1, chat_id=ANN, tg_message_id=10, sender_id=ANN, sender_name="Ann", text="photo for you",
        date=T0, media_type="photo", media_path=f"media/{ANN}/10.jpg", **msg_defaults)
    add("messages", id=2, chat_id=ANN, tg_message_id=11, sender_id=ME, sender_name="Me", text="thanks",
        date=T0, reply_to_tg_message_id=10, media_type="document", media_filename="deck.pdf", media_size=99,
        reactions_json='[{"emoji": "👍", "count": 1}]', **{**msg_defaults, "is_outgoing": 1})
    add("messages", id=3, chat_id=GROUP, tg_message_id=5, sender_id=BOB, sender_name="Bob", text="gm",
        date=T0, scam_status="completed", scam_score=0.9, **msg_defaults)
    add("messages", id=4, chat_id=GROUP, tg_message_id=6, sender_id=GROUP, sender_name="Builders",
        text="anon admin", date=T0, **msg_defaults)
    add("messages", id=5, chat_id=CHANNEL, tg_message_id=7, sender_id=CHANNEL, sender_name="News", text="post",
        date=T0, **msg_defaults)
    add("messages", id=6, chat_id=FORUM, tg_message_id=40, sender_id=BOB, sender_name="Bob", text="in topic",
        date=T0, topic_id=30, **msg_defaults)
    add("messages", id=7, chat_id=ANN, tg_message_id=12, sender_id=ANN, sender_name="Ann",
        text="https://example.com", date=T0, media_path=f"media/{ANN}/12.jpg", **msg_defaults)  # link preview

    add("organizations", id=3, name="Acme", created_at=T0)
    add("contact_positions", id=4, telegram_id=ANN, organization_id=3, organization="Acme", title="CTO", is_active=1)
    add("person_notes", id=9, telegram_id=BOB, kind="due_diligence", title="Bob", body="checked",
        citations_json=json.dumps([{"chat_id": GROUP, "tg_message_id": 5}, {"chat_id": ANN, "tg_message_id": 999}]),
        author="agent", agent_label="claude-code", created_at=T0, updated_at=T0)
    add("tags", id=1, name="Work", color="indigo", is_default=1, created_at=T0)
    add("tags", id=7, name="Investor", color="amber", is_default=0, created_at=T0)
    add("tags", id=8, name="New", color="slate", category="stage", is_default=0, created_at=T0)
    add("chat_tags", id=1, chat_id=GROUP, tag_id=7)
    add("workspaces", id=1, name="Deals", color="indigo", match_mode="any", view_mode="kanban", position=0,
        created_at=T0)
    add("workspace_tags", id=1, workspace_id=1, tag_id=7, is_exclusion=0)
    add("workspace_boards", id=2, workspace_id=1, name="Pipeline", position=0)
    add("workspace_columns", id=3, workspace_id=1, tag_id=8, board_id=2, position=1, label="New")
    add("workspace_chat_overrides", id=4, workspace_id=1, chat_id=GROUP, triaged_at=T0)
    add("chat_history_coverage", chat_id=ANN, history_complete=1, oldest_tg_id=10, newest_contiguous_tg_id=12,
        walk_cursor_tg_id=10, attempts=0)
    add("agent_reviews", chat_id=ANN, reviewed_through_tg_id=11, reviewed_message_count=2, reviewed_at=T0,
        agent_label="claude-code")
    add("briefing_runs", id=1, generated_at=T0, days=1, status="done")
    add("briefing_entries", id=1, run_id=1, chat_id=GROUP, chat_name="Builders", category="tracked", summary="s",
        sources_json=json.dumps([{"chat_id": GROUP, "telegram_id": 5}]))
    add("briefing_entries", id=2, run_id=1, chat_id=ANN, chat_name="Ann Lee", category="important_contact",
        summary="s2")
    add("catchup_items", id=1, chat_telegram_id=ANN, contact_id=7, status="pending", created_at=T0, updated_at=T0)
    add("catchup_summaries", id=1, chat_telegram_id=ANN, summary="old", action_needed=0,
        total_messages_at_generation=1, generated_at=T0)
    add("conversations", id=1, title="Ask", created_at=T0, updated_at=T0)
    add("conversation_messages", id=1, conversation_id=1, role="user", content="who is Ann?", created_at=T0)
    add("scam_flags", id=1, telegram_id=BOB, source="local", confidence=0.9, flagged_message_chat_id=GROUP,
        flagged_message_id=5, flagged_at=T0, synced_to_cloud=0)
    add("scam_dismissals", id=1, dismissal_type="scammer", created_at=T0,
        payload_json=json.dumps({"telegram_id": BOB, "source": "local"}))
    add("scam_dismissals", id=2, dismissal_type="scan", created_at=T0, synced_at=T0,
        payload_json=json.dumps({"chat_id": GROUP, "tg_message_id": 5, "sender_id": BOB, "scam_score": 0.9}))
    add("scam_reports", id=1, report_id="r1", reported_telegram_id=BOB, reported_in_chat_id=GROUP,
        reporter_verdict="scam", status="submitted", created_at=T0, updated_at=T0,
        messages_json=json.dumps([{"position": 0, "telegram_id": BOB, "role": "reported", "included": True}]),
        local_detection_json=json.dumps({"scores": [{"tg_message_id": 5, "scam_score": 0.9}]}))
    add("scam_feedback", id=1, feedback_type="false_positive", message="not a scam", related_chat_id=GROUP,
        related_message_id=5, related_flag_telegram_id=BOB, status="pending", created_at=T0)
    analytics = {c[1]: 0 for c in db.execute("PRAGMA table_info(scam_analytics)") if c[3]}
    add("scam_analytics", **{**analytics, "id": 1, "period_start": T0, "scans_private": 3, "total_scanned": 4})
    add("admin_action_log", id=1, chat_telegram_id=GROUP, action="ban", target_user_id=BOB, created_at=T0)
    add("scam_check_requests", id=1, request_id="q", request_type="is_this_scam", status="pending",
        payload_json="{}", created_at=T0, updated_at=T0)
    db.commit()
    db.close()

    (data_dir / "media" / str(ANN)).mkdir(parents=True)
    (data_dir / "media" / str(ANN) / "10.jpg").write_bytes(b"photo-10")
    (data_dir / "media" / str(ANN) / "12.jpg").write_bytes(b"preview")
    (data_dir / "profile_photos").mkdir()
    for peer in (ANN, GROUP, 424242):
        (data_dir / "profile_photos" / f"{peer}.jpg").write_bytes(f"avatar {peer}".encode())
    return path


async def _start(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    database_engine = database.engine
    database.engine = engine
    try:
        await startup.init_db()
    finally:
        database.engine = database_engine
        await engine.dispose()


def _q(path: Path, sql: str, *args):
    with sqlite3.connect(str(path)) as db:
        return db.execute(sql, args).fetchall()


def _one(path: Path, sql: str, *args):
    rows = _q(path, sql, *args)
    assert len(rows) == 1, rows
    return rows[0]


@pytest.mark.asyncio
async def test_every_generation1_row_is_imported(tmp_path):
    path = _gen1(tmp_path)
    await _start(path)

    assert _q(path, "SELECT value FROM schema_meta") == [("2",)]
    assert _q(path, "PRAGMA foreign_key_check") == []
    assert {r[0] for r in _q(path, "SELECT name FROM sqlite_master WHERE type='table'")}.isdisjoint(
        {"chats", "contacts", "contact_positions", "chat_tags"})
    [backup] = (tmp_path / "backups").glob("mingly-retired-gen1-*.db")
    assert _q(backup, "SELECT COUNT(*) FROM messages") == [(7,)]  # the original, kept

    # Account + self, as runtime._record_account writes them
    account_id, external, label, self_identity = _one(path, "SELECT id, external_id, label, self_identity_id FROM accounts")
    assert (external, label) == (str(ME), "@me")
    assert _one(path, "SELECT external_id, is_self FROM identities WHERE id = ?", self_identity) == (str(ME), 1)

    # People: contacts' CRM fields, one identity per Telegram user, phone contact point
    ann = _one(path, "SELECT id, person_id, display_name, first_name, phone FROM identities WHERE external_id = ?",
               str(ANN))
    assert ann[2:] == ("Ann Lee", "Ann", "15551234567")
    assert _one(path, "SELECT twitter_handle, notes, is_important, relationship_status, relationship_status_manual "
                      "FROM people WHERE id = ?", ann[1]) == ("annlee", "met at KBW", 1, "warm", 1)
    assert _q(path, "SELECT kind, value FROM contact_points WHERE identity_id = ?", ann[0]) == [("phone", "+15551234567")]
    bob = _one(path, "SELECT id, person_id, display_name FROM identities WHERE external_id = ?", str(BOB))
    assert bob[2] == "Bob"  # from his latest message
    assert _q(path, "SELECT COUNT(*) FROM identities WHERE CAST(external_id AS INTEGER) < 0") == [(0,)]

    # Conversations keep their ids; kinds and peers as telegram/ids.py derives them
    convs = {r[0]: r[1:] for r in _q(path, "SELECT external_id, id, kind, peer_identity_id, account_id, is_tracked, "
                                           "unread_count, pinned_message_id, last_message_at FROM conversations")}
    assert convs[str(ANN)] == (2, "direct", ann[0], account_id, 1, 2, 1, "2026-01-02 09:00:00.000000")
    assert convs[str(GROUP)][:3] == (3, "group", None)
    assert convs[str(CHANNEL)][:2] == (5, "channel")
    assert _one(path, "SELECT is_self, peer_identity_id FROM conversations WHERE id = 1") == (1, self_identity)

    # Messages: ids kept; seq/external_id from the Telegram id; senders; reply; sender_peer only for send-as
    msgs = {r[0]: r[1:] for r in _q(path, "SELECT id, conversation_id, external_id, seq, sender_identity_id, "
                                          "reply_to_external_id, topic_id, meta_json FROM messages")}
    assert msgs[1][:4] == (2, "10", 10, ann[0])
    assert msgs[2][4] == "10"
    assert msgs[3][3] == bob[0]
    assert msgs[4][3] is None and json.loads(msgs[4][6]) == {"sender_peer": GROUP}
    assert msgs[5][3] is None and msgs[5][6] is None
    assert msgs[6][5] == 30
    assert _one(path, "SELECT reactions_json, is_outgoing FROM messages WHERE id = 2") == ('[{"emoji": "👍", "count": 1}]', 1)
    assert _one(path, "SELECT scam_status, scam_score FROM messages WHERE id = 3") == ("completed", 0.9)

    # Attachments: staged in the generation-2 layout when the file existed
    atts = {r[0]: r[1:] for r in _q(path, "SELECT message_id, kind, filename, mime_type, size, local_path "
                                          "FROM message_attachments")}
    photo_path = media_paths.attachment_file(2, 1, ".jpg", media_dir=Path("media"))
    assert atts[1] == ("photo", None, "image/jpeg", None, str(photo_path))
    assert (tmp_path / photo_path).read_bytes() == b"photo-10"
    assert atts[2] == ("document", "deck.pdf", "application/pdf", 99, None)  # not downloaded: fetched on demand
    assert 7 not in atts  # the link-preview file had no media type

    # Avatars: users on the identity, groups on the conversation; unknown peers ignored
    ann_avatar = _one(path, "SELECT avatar_path FROM identities WHERE id = ?", ann[0])[0]
    assert ann_avatar == str(media_paths.identity_avatar_file(ann[0], media_dir=Path("media")))
    assert (tmp_path / ann_avatar).read_bytes() == f"avatar {ANN}".encode()
    group_avatar = _one(path, "SELECT avatar_path FROM conversations WHERE id = 3")[0]
    assert (tmp_path / group_avatar).read_bytes() == f"avatar {GROUP}".encode()

    # Tags replace the seeded defaults; workspace graph intact
    assert _q(path, "SELECT id, name FROM tags ORDER BY id") == [(1, "Work"), (7, "Investor"), (8, "New")]
    assert _q(path, "SELECT conversation_id, tag_id FROM conversation_tags") == [(3, 7)]
    assert _q(path, "SELECT workspace_id, tag_id, board_id, label FROM workspace_columns") == [(1, 8, 2, "New")]
    assert _q(path, "SELECT workspace_id, conversation_id FROM workspace_conversation_overrides") == [(1, 3)]

    # Notes and positions on the person; citations re-keyed (the dangling one dropped)
    note = _one(path, "SELECT person_id, kind, citations_json FROM person_notes WHERE id = 9")
    assert note[:2] == (bob[1], "due_diligence")
    assert json.loads(note[2]) == [{"conversation_id": 3, "message_id": 3}]
    assert _q(path, "SELECT person_id, organization_id, title FROM person_positions") == [(ann[1], 3, "CTO")]

    # Coverage and reviews in seq terms
    assert _q(path, "SELECT conversation_id, history_complete, oldest_seq, newest_contiguous_seq "
                    "FROM history_coverage") == [(2, 1, 10, 12)]
    assert _q(path, "SELECT conversation_id, reviewed_through_seq, reviewed_message_count FROM agent_reviews") == [(2, 11, 2)]

    # Briefings, catch-up, AI chat
    entries = {r[0]: r[1:] for r in _q(path, "SELECT id, conversation_id, person_id, sources_json FROM briefing_entries")}
    assert entries[1][:2] == (3, None) and json.loads(entries[1][2]) == [{"conversation_id": 3, "message_id": 3}]
    assert entries[2][:2] == (None, ann[1])
    assert _q(path, "SELECT conversation_id, person_id FROM catchup_items") == [(2, ann[1])]
    assert _q(path, "SELECT COUNT(*) FROM catchup_summaries") == [(0,)]  # cache, regenerated
    assert _q(path, "SELECT ai_conversation_id, content FROM ai_conversation_messages") == [(1, "who is Ann?")]

    # Shield
    assert _q(path, "SELECT identity_id, flagged_conversation_id, flagged_message_id FROM scam_flags") == [(bob[0], 3, 3)]
    dismissals = {r[0]: (json.loads(r[1]), r[2]) for r in _q(path, "SELECT id, payload_json, cloud_status FROM scam_dismissals")}
    assert dismissals[1] == ({"identity_id": bob[0], "source": "local", "cloud_v1": {"telegram_id": BOB}}, "pending")
    assert dismissals[2][0]["message_id"] == 3 and dismissals[2][0]["cloud_v1"]["tg_message_id"] == 5
    assert dismissals[2][1] == "synced"
    report = _one(path, "SELECT reported_identity_id, reported_in_conversation_id, messages_json, local_detection_json "
                        "FROM scam_reports")
    assert report[:2] == (bob[0], 3)
    assert json.loads(report[2])[0]["identity_id"] == bob[0]
    assert json.loads(report[3])["scores"][0]["message_id"] == 3
    assert _q(path, "SELECT related_conversation_id, related_message_id, related_flag_identity_id "
                    "FROM scam_feedback") == [(3, 3, bob[0])]
    assert _q(path, "SELECT scans_direct, total_scanned FROM scam_analytics") == [(3, 4)]
    assert _q(path, "SELECT COUNT(*) FROM scam_check_requests") == [(0,)]
    assert _q(path, "SELECT conversation_id, target_identity_id FROM admin_action_log") == [(3, bob[0])]

    # The old files moved aside; nothing left over
    [retired] = [p for p in (tmp_path / "backups").iterdir() if p.is_dir()]
    assert (retired / "media" / str(ANN) / "12.jpg").exists()
    assert (retired / "profile_photos" / "424242.jpg").exists()
    assert not (tmp_path / "profile_photos").exists()
    assert not list(tmp_path.glob("*.importing*")) and not (tmp_path / "gen1-import.json").exists()

    # New ids continue after the kept ones
    assert _q(path, "SELECT seq FROM sqlite_sequence WHERE name = 'messages'") == [(7,)]

    # A generation-2 DB is left alone on the next start
    await _start(path)
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


@pytest.mark.asyncio
async def test_a_failed_import_leaves_the_old_database_in_place(tmp_path, monkeypatch):
    path = _gen1(tmp_path)
    before = path.read_bytes()

    def boom(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(legacy_import._Importer, "_shield", boom)
    with pytest.raises(RuntimeError, match="boom"):
        await _start(path)
    assert path.read_bytes() == before
    assert (tmp_path / "media" / str(ANN) / "10.jpg").read_bytes() == b"photo-10"
    assert (tmp_path / "profile_photos" / f"{ANN}.jpg").exists()
    assert not list(tmp_path.glob("*.importing*")) and not (tmp_path / "gen1-import.json").exists()

    # The next start imports it
    monkeypatch.undo()
    await _start(path)
    assert _q(path, "SELECT COUNT(*) FROM messages") == [(7,)]


@pytest.mark.asyncio
async def test_an_import_interrupted_mid_swap_completes_on_the_next_start(tmp_path, monkeypatch):
    path = _gen1(tmp_path)
    real_replace = os.replace

    def crash_before_db(src, dst):
        if Path(dst) == path:
            raise OSError("power cut")
        real_replace(src, dst)

    monkeypatch.setattr(legacy_import.os, "replace", crash_before_db)
    with pytest.raises(OSError, match="power cut"):
        await _start(path)
    monkeypatch.undo()
    # Files already swapped, the DB not: still generation 1, journal present
    assert (tmp_path / "gen1-import.json").exists()
    assert _q(path, "SELECT COUNT(*) FROM chats") == [(5,)]

    await _start(path)
    assert _q(path, "SELECT value FROM schema_meta") == [("2",)]
    local_path = _one(path, "SELECT local_path FROM message_attachments WHERE message_id = 1")[0]
    assert (tmp_path / local_path).read_bytes() == b"photo-10"
    ann_avatar = _one(path, "SELECT avatar_path FROM identities WHERE external_id = ?", str(ANN))[0]
    assert (tmp_path / ann_avatar).read_bytes() == f"avatar {ANN}".encode()
    assert not (tmp_path / "gen1-import.json").exists() and not list(tmp_path.glob("*.importing*"))
