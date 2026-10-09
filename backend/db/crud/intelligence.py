"""Cross-conversation queries: mentions, needs-reply, keyword/title search, member counts."""

from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import and_, desc, func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.crud.conversations import one_to_one_filter
from backend.db.models import (
    Account, CatchUpItem, Conversation, ConversationTag, Identity, Message, Person, Tag,
)

__all__ = [
    "channel_filter",
    "get_mentions",
    "check_replied_after",
    "get_first_reply_after",
    "get_mention_contexts",
    "get_needs_reply",
    "search_messages_by_keyword",
    "title_match",
    "search_conversations_by_title",
    "find_conversations_by_member_count",
]


def channel_filter(channel: Optional[str]):
    """Conversations on ``channel`` (all when None)."""
    if not channel:
        return Conversation.id.isnot(None)
    return Conversation.account_id.in_(select(Account.id).where(Account.channel == channel))


async def _self_identities(db: AsyncSession) -> list[tuple[int, Identity]]:
    """(account_id, self identity) of every signed-in account."""
    rows = (await db.execute(
        select(Account.id, Identity).join(Identity, Identity.id == Account.self_identity_id)
    )).all()
    return [(acc_id, ident) for acc_id, ident in rows]


async def get_mentions(
    db: AsyncSession,
    limit: int = 50,
    days: int = 7,
    group_name_filter: str = "",
    tag_name_filter: str = "",
    channel: Optional[str] = None,
) -> list:
    """Incoming messages in group/channel conversations that mention the
    user: ``@username`` in the text, or an inline mention link to the user's
    identity on that account (``text_html`` links ``/mention/<channel>/<external_id>``).
    Rows are ``(Message, Conversation)``, newest first."""
    per_account = []
    for account_id, ident in await _self_identities(db):
        conds = [Message.text_html.like(f'%/mention/{ident.channel}/{ident.external_id}"%')]
        if ident.username:
            conds.append(Message.text.ilike(f"%@{ident.username}%"))
        per_account.append(and_(Conversation.account_id == account_id, or_(*conds)))
    if not per_account:
        return []

    since = datetime.utcnow() - timedelta(days=days)
    q = (
        select(Message, Conversation)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Message.is_outgoing == False,  # noqa: E712
            Conversation.kind.in_(["group", "channel"]),
            or_(*per_account),
            Message.date >= since,
            channel_filter(channel),
        )
    )
    if group_name_filter:
        q = q.where(Conversation.title.ilike(f"%{group_name_filter}%"))
    if tag_name_filter:
        q = (
            q.join(ConversationTag, Conversation.id == ConversationTag.conversation_id)
            .join(Tag, ConversationTag.tag_id == Tag.id)
            .where(Tag.name.ilike(f"%{tag_name_filter}%"))
        )
    q = q.order_by(desc(Message.date)).limit(limit)
    return list((await db.execute(q)).all())


async def check_replied_after(db: AsyncSession, conversation_id: int, after_date: datetime) -> bool:
    """True if the user sent a message in the conversation after ``after_date``."""
    return (await db.execute(
        select(Message.id).where(
            Message.conversation_id == conversation_id,
            Message.is_outgoing == True,  # noqa: E712
            Message.date > after_date,
        ).limit(1)
    )).first() is not None


async def get_first_reply_after(db: AsyncSession, message: Message) -> Optional[Message]:
    """The user's first outgoing message after ``message`` in its conversation."""
    return (await db.execute(
        select(Message)
        .where(
            Message.conversation_id == message.conversation_id,
            Message.seq > message.seq,
            Message.is_outgoing == True,  # noqa: E712
        )
        .order_by(Message.seq)
        .limit(1)
    )).scalar_one_or_none()


async def get_mention_contexts(
    db: AsyncSession,
    anchors: Iterable[Message],
    before: int = 2,
    after: int = 2,
) -> dict[int, dict]:
    """Batch ``get_messages_around`` + ``get_first_reply_after`` for many
    anchor messages in one compound query.

    Returns ``{message_id: {"before": [...], "after": [...], "reply": Message|None}}``
    with before/after oldest-first (by seq). Callers needing N "after"
    messages for display plus gap detection should request N+1.
    """
    anchors = list(anchors)
    out = {m.id: {"before": [], "after": [], "reply": None} for m in anchors}
    if not anchors:
        return out
    parts = []
    for m in anchors:
        scope = Message.conversation_id == m.conversation_id
        b = (
            select(Message.id.label("mid")).where(scope, Message.seq < m.seq)
            .order_by(desc(Message.seq)).limit(before).subquery()
        )
        a = (
            select(Message.id.label("mid")).where(scope, Message.seq > m.seq)
            .order_by(Message.seq).limit(after).subquery()
        )
        r = (
            select(Message.id.label("mid"))
            .where(scope, Message.seq > m.seq, Message.is_outgoing == True)  # noqa: E712
            .order_by(Message.seq).limit(1).subquery()
        )
        parts.extend([select(b.c.mid), select(a.c.mid), select(r.c.mid)])
    # SQLite caps compound SELECTs at 500 members; chunk to stay well below.
    ids: set = set()
    for i in range(0, len(parts), 300):
        chunk = parts[i:i + 300]
        stmt = chunk[0] if len(chunk) == 1 else union_all(*chunk)
        ids.update((await db.execute(stmt)).scalars().all())
    if not ids:
        return out
    rows = (await db.execute(
        select(Message).where(Message.id.in_(ids)).order_by(Message.conversation_id, Message.seq)
    )).scalars().all()
    by_conv: dict[int, list] = {}
    for m in rows:
        by_conv.setdefault(m.conversation_id, []).append(m)
    for m in anchors:
        ctx = out[m.id]
        msgs = by_conv.get(m.conversation_id, [])
        ctx["before"] = [x for x in msgs if x.seq < m.seq][-before:] if before else []
        ctx["after"] = [x for x in msgs if x.seq > m.seq][:after]
        ctx["reply"] = next((x for x in msgs if x.seq > m.seq and x.is_outgoing), None)
    return out


# ── Needs reply (dashboard + catch-up) ───────────────────────────────────────

async def get_needs_reply(
    db: AsyncSession,
    limit: int = 10,
    now: Optional[datetime] = None,
    handled_limit: int = 10,
) -> tuple[list, list]:
    """Conversations waiting on the user, with catch-up triage applied BEFORE the limit.

    A conversation needs a reply when its latest message is incoming and
    un-reacted, and it is tracked or a direct conversation with an important
    person (hidden, briefing-only and deactivated conversations excluded).
    Triage then removes:
      - handled items, unless a new message arrived after handled_at;
      - items snoozed until a future time.

    Returns (active, recently_handled); each row is
    (Conversation, last_sender_name, last_text, last_date, CatchUpItem | None).
    """
    now = now or datetime.utcnow()
    important_peers = (
        select(Identity.id).join(Person, Person.id == Identity.person_id)
        .where(Person.is_important == True)  # noqa: E712
    )
    relevant = or_(
        Conversation.is_tracked == True,  # noqa: E712
        and_(one_to_one_filter(), Conversation.peer_identity_id.in_(important_peers)),
    )
    last_msg = (
        select(
            Message.conversation_id,
            Message.sender_name,
            func.substr(func.coalesce(Message.text, Message.subject), 1, 200).label("text"),
            Message.date,
            Message.is_outgoing,
            Message.my_reaction,
            func.row_number().over(
                partition_by=Message.conversation_id,
                order_by=(desc(Message.date), desc(Message.seq), desc(Message.id)),
            ).label("rn"),
        )
        .where(Message.kind == "message")  # service lines (joins, calls…) need no reply
        .subquery()
    )
    base = (
        select(Conversation, last_msg.c.sender_name, last_msg.c.text, last_msg.c.date, CatchUpItem)
        .join(last_msg, and_(Conversation.id == last_msg.c.conversation_id, last_msg.c.rn == 1))
        .outerjoin(CatchUpItem, CatchUpItem.conversation_id == Conversation.id)
        .where(
            last_msg.c.is_outgoing == False,  # noqa: E712
            last_msg.c.my_reaction == None,  # noqa: E711
            Conversation.is_hidden == False,  # noqa: E712
            Conversation.is_briefing == False,  # noqa: E712
            Conversation.is_deactivated == False,  # noqa: E712
            relevant,
        )
    )
    handled_current = and_(
        CatchUpItem.status == "handled",
        or_(CatchUpItem.handled_at == None, CatchUpItem.handled_at >= last_msg.c.date),  # noqa: E711
    )
    snoozed_future = and_(
        CatchUpItem.status == "snoozed",
        CatchUpItem.snoozed_until != None,  # noqa: E711
        CatchUpItem.snoozed_until > now,
    )
    active = (await db.execute(
        base.where(or_(CatchUpItem.id == None, and_(~handled_current, ~snoozed_future)))  # noqa: E711
        .order_by(desc(last_msg.c.date))
        .limit(limit)
    )).all()
    handled = []
    if handled_limit:
        handled = (await db.execute(
            base.where(handled_current).order_by(desc(CatchUpItem.handled_at)).limit(handled_limit)
        )).all()
    return list(active), list(handled)


# ── Search ───────────────────────────────────────────────────────────────────

async def search_messages_by_keyword(
    db: AsyncSession, keyword: str, limit: int = 30, channel: Optional[str] = None
) -> tuple[list, int]:
    """Case-insensitive search across every stored message (text and subject).

    Returns (rows, total): rows are ``(Message, Conversation)``, at most
    ``limit`` most-recent matches; total is the full match count.
    """
    like = f"%{keyword}%"
    match = and_(or_(Message.text.ilike(like), Message.subject.ilike(like)), channel_filter(channel))
    base = select(Message, Conversation).join(Conversation, Message.conversation_id == Conversation.id).where(match)
    total = (await db.execute(
        select(func.count()).select_from(base.with_only_columns(Message.id).subquery())
    )).scalar() or 0
    rows = (await db.execute(base.order_by(desc(Message.date)).limit(limit))).all()
    return list(rows), total


def title_match(keyword: str):
    """Space- and case-insensitive title filter: 'ETHTaipei' matches 'ETH Taipei'."""
    kw = keyword.lower().replace(" ", "")
    return func.replace(func.lower(Conversation.title), " ", "").like(f"%{kw}%")


async def search_conversations_by_title(
    db: AsyncSession, keyword: str, limit: int = 20, channel: Optional[str] = None
) -> tuple[list[Conversation], int]:
    """(conversations, total) whose title contains the keyword, ignoring
    spaces and case; at most ``limit`` rows (alphabetical)."""
    conds = (title_match(keyword), Conversation.is_deactivated == False, channel_filter(channel))  # noqa: E712
    total = (await db.execute(select(func.count()).select_from(Conversation).where(*conds))).scalar() or 0
    rows = (await db.execute(select(Conversation).where(*conds).order_by(Conversation.title).limit(limit))).scalars()
    return list(rows.all()), total


async def find_conversations_by_member_count(
    db: AsyncSession,
    count: int,
    operator: str = "more_than",
    limit: int = 50,
    channel: Optional[str] = None,
) -> list[Conversation]:
    """Conversations filtered by member count. operator: 'more_than' | 'exactly' | 'less_than'."""
    if operator == "more_than":
        condition = Conversation.members_count > count
    elif operator == "exactly":
        condition = Conversation.members_count == count
    elif operator == "less_than":
        condition = Conversation.members_count < count
    else:
        return []
    return list((await db.execute(
        select(Conversation)
        .where(Conversation.members_count.isnot(None), condition, channel_filter(channel))
        .order_by(Conversation.members_count.desc())
        .limit(limit)
    )).scalars().all())
