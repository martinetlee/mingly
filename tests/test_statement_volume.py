"""Statement volume of storing one Telegram history page (rev-telegram #4):
identities are prefetched in one query and only written when something
changed, and new messages without files skip the attachment lookup."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from telethon.tl import types

from backend.db import crud
from backend.telegram.convert import rows_for
from tests import factories as f

UTC = timezone.utc
PAGE = 100
SENDERS = 10
# Ceilings for one 100-message page from 10 senders. Measured before the
# fix: 282 (first sight of the senders), 231 (same page again); after: 183 /
# 103 / 103 (one upsert per message plus a few batch reads; first sight adds
# the new identities and people).
FIRST_PAGE_MAX = 190
KNOWN_SENDERS_PAGE_MAX = 110
RESYNC_PAGE_MAX = 110


def _page(start: int):
    users = [types.User(id=500 + i, first_name=f"U{i}", username=f"user{i}", access_hash=1) for i in range(SENDERS)]
    msgs = []
    for k in range(PAGE):
        u = users[k % SENDERS]
        m = types.Message(id=start + k, peer_id=types.PeerChannel(42), from_id=types.PeerUser(u.id),
                          date=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=k), message=f"m{k}")
        m._sender, m._sender_id = u, u.id
        msgs.append(m)
    return msgs


class _Counter:
    def __init__(self, engine):
        self.n = 0
        self.engine = engine.sync_engine

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._count)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._count)

    def _count(self, *args, **kw):
        self.n += 1


async def _store(db, conv, msgs) -> None:
    await crud.save_messages(db, await rows_for(db, conv.id, msgs, forum=False))


@pytest.mark.asyncio
async def test_one_history_page_stays_under_a_statement_ceiling(db, db_engine):
    conv = await f.conversation(db, "-42", title="Group", kind="group")
    with _Counter(db_engine) as first:
        await _store(db, conv, _page(1))
    with _Counter(db_engine) as known:
        await _store(db, conv, _page(1 + PAGE))  # new messages, senders already stored
    with _Counter(db_engine) as again:
        await _store(db, conv, _page(1))  # the same page synced again: nothing changed
    print(f"\nstatements: first page {first.n}, known senders {known.n}, re-sync {again.n}")
    assert first.n <= FIRST_PAGE_MAX
    assert known.n <= KNOWN_SENDERS_PAGE_MAX
    assert again.n <= RESYNC_PAGE_MAX
