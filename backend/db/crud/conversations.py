"""Conversations (chats, groups, channels, email threads) and their history
coverage. Keyed by internal ``conversation_id``; a channel's own id is only
used through ``get_conversation_by_external`` / ``upsert_conversation``."""

from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import asc, case, desc, func, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Account, Conversation, ConversationTag, HistoryCoverage, Message

__all__ = [
    "ONE_TO_ONE_KINDS",
    "one_to_one_filter",
    "visible_filter",
    "list_conversations",
    "conversation_labels",
    "get_tracked_conversations",
    "get_briefing_conversations",
    "get_hidden_conversations",
    "get_archived_conversations",
    "get_conversation",
    "get_conversations",
    "get_conversation_by_external",
    "get_conversation_by_title",
    "existing_external_ids",
    "upsert_conversation",
    "set_conversation_flags",
    "mark_read",
    "set_pinned_message",
    "get_pinned_message",
    "touch_synced",
    "refresh_preview",
    "recompute_preview",
    "get_coverage",
    "get_or_create_coverage",
    "CONVERSATION_FLAGS",
]


# Flags a user (or a sync) may set through ``set_conversation_flags``.
CONVERSATION_FLAGS = (
    "is_monitored", "is_hidden", "is_briefing", "is_tracked", "is_pinned", "is_archived",
    "is_muted", "muted_until", "is_marked_unread",
)


# A conversation with exactly one other party: a chat with a person, or an
# email thread with a single counterpart. These are "the person's
# conversations" for relationship status, needs-reply and the person page.
ONE_TO_ONE_KINDS = ("direct", "email_thread")


def one_to_one_filter():
    return Conversation.kind.in_(ONE_TO_ONE_KINDS) & Conversation.peer_identity_id.isnot(None)


def visible_filter():
    """Conversations that appear in the inbox: not hidden, briefing-only,
    archived or deactivated."""
    return (
        (Conversation.is_hidden == False)  # noqa: E712
        & (Conversation.is_briefing == False)  # noqa: E712
        & (Conversation.is_archived == False)  # noqa: E712
        & (Conversation.is_deactivated == False)  # noqa: E712
    )


async def list_conversations(
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "recent",
    tag_id: Optional[int] = None,
    member_ids=None,
    channel: Optional[str] = None,
    label: Optional[str] = None,
    kind: Optional[str] = None,
    exclude_kind: Optional[str] = None,
    account_id: Optional[int] = None,
    archived: Optional[bool] = False,
) -> list[Conversation]:
    """The inbox. ``member_ids`` is an optional subquery/collection of
    conversation ids to restrict to (e.g. a workspace's members);
    ``channel`` restricts to one channel's accounts, ``account_id`` to one
    account, ``kind`` / ``exclude_kind`` to (or away from) one conversation
    kind, ``label`` to conversations carrying a channel label (Gmail).
    ``archived``: False = the inbox (default), True = archived only, None =
    both (a mail folder view)."""
    query = select(Conversation).where(
        Conversation.is_hidden == False,  # noqa: E712
        Conversation.is_briefing == False,  # noqa: E712
        Conversation.is_deactivated == False,  # noqa: E712
    )
    if archived is not None:
        query = query.where(Conversation.is_archived == archived)
    if label is not None:
        query = query.where(_has_label(label))
    if kind is not None:
        query = query.where(Conversation.kind == kind)
    if exclude_kind is not None:
        query = query.where(Conversation.kind != exclude_kind)
    if account_id is not None:
        query = query.where(Conversation.account_id == account_id)
    if channel is not None:
        query = query.where(Conversation.account_id.in_(select(Account.id).where(Account.channel == channel)))
    if tag_id is not None:
        query = query.join(ConversationTag, ConversationTag.conversation_id == Conversation.id).where(
            ConversationTag.tag_id == tag_id
        )
    if member_ids is not None:
        query = query.where(Conversation.id.in_(member_ids))

    if sort_by == "unread":
        query = query.order_by(
            desc(Conversation.is_pinned), desc(Conversation.unread_count),
            desc(Conversation.last_activity_at), Conversation.title,
        )
    elif sort_by == "alpha":
        query = query.order_by(desc(Conversation.is_pinned), asc(Conversation.title))
    else:  # recent
        query = query.order_by(desc(Conversation.is_pinned), desc(Conversation.last_activity_at), Conversation.title)
    return list((await db.execute(query.limit(limit).offset(offset))).scalars().all())


def _has_label(label: str):
    return text(
        "EXISTS (SELECT 1 FROM json_each(conversations.labels_json) WHERE json_each.value = :label)"
    ).bindparams(label=label)


async def conversation_labels(db: AsyncSession, account_id: Optional[int] = None) -> list[tuple[str, int]]:
    """Every channel label in use (optionally on one account), with how many
    visible conversations carry it."""
    labels = func.json_each(Conversation.labels_json).table_valued("value").alias("labels")
    rows = (await db.execute(
        select(labels.c.value, func.count())
        .select_from(Conversation)
        .join(labels, text("1 = 1"))
        .where(
            Conversation.labels_json.isnot(None),
            Conversation.is_hidden == False,  # noqa: E712
            Conversation.is_deactivated == False,  # noqa: E712
            *((Conversation.account_id == account_id,) if account_id is not None else ()),
        )
        .group_by(labels.c.value)
        .order_by(labels.c.value)
    )).all()
    return [(str(v), int(n)) for v, n in rows]


async def _where(db: AsyncSession, *conds, order=None) -> list[Conversation]:
    q = select(Conversation).where(*conds)
    q = q.order_by(*(order or (Conversation.title,)))
    return list((await db.execute(q)).scalars().all())


async def get_tracked_conversations(db: AsyncSession) -> list[Conversation]:
    return await _where(
        db, Conversation.is_tracked == True,  # noqa: E712
        order=(desc(Conversation.last_activity_at), Conversation.title),
    )


async def get_briefing_conversations(db: AsyncSession) -> list[Conversation]:
    return await _where(db, Conversation.is_briefing == True)  # noqa: E712


async def get_hidden_conversations(db: AsyncSession) -> list[Conversation]:
    return await _where(db, Conversation.is_hidden == True)  # noqa: E712


async def get_archived_conversations(db: AsyncSession) -> list[Conversation]:
    return await _where(db, Conversation.is_archived == True)  # noqa: E712


async def get_conversation(db: AsyncSession, conversation_id: int) -> Optional[Conversation]:
    return (await db.execute(select(Conversation).where(Conversation.id == conversation_id))).scalar_one_or_none()


async def get_conversations(db: AsyncSession, conversation_ids: Iterable[int]) -> dict[int, Conversation]:
    ids = list(set(conversation_ids))
    out: dict[int, Conversation] = {}
    for i in range(0, len(ids), 500):
        res = await db.execute(select(Conversation).where(Conversation.id.in_(ids[i:i + 500])))
        out.update({c.id: c for c in res.scalars().all()})
    return out


async def get_conversation_by_external(
    db: AsyncSession, account_id: int, external_id: str
) -> Optional[Conversation]:
    return (await db.execute(
        select(Conversation).where(Conversation.account_id == account_id, Conversation.external_id == external_id)
    )).scalar_one_or_none()


async def get_conversation_by_title(db: AsyncSession, title: str) -> Optional[Conversation]:
    """Exact (case-insensitive) title match first, then a substring match.
    Deactivated conversations never match."""
    live = Conversation.is_deactivated == False  # noqa: E712
    exact = (await db.execute(
        select(Conversation).where(live, Conversation.title.ilike(title)).limit(1)
    )).scalar_one_or_none()
    if exact is not None:
        return exact
    return (await db.execute(
        select(Conversation).where(live, Conversation.title.ilike(f"%{title}%"))
        .order_by(desc(Conversation.last_activity_at)).limit(1)
    )).scalar_one_or_none()


async def existing_external_ids(db: AsyncSession, account_id: int, external_ids: list[str]) -> set[str]:
    if not external_ids:
        return set()
    out: set[str] = set()
    for i in range(0, len(external_ids), 500):
        res = await db.execute(
            select(Conversation.external_id).where(
                Conversation.account_id == account_id, Conversation.external_id.in_(external_ids[i:i + 500])
            )
        )
        out.update(r[0] for r in res.all())
    return out


# Columns the channel owns: an upsert takes the new value whenever provided.
_CHANNEL_OWNED = (
    "unread_count", "members_count", "is_forum", "slow_mode_seconds", "is_deactivated", "is_archived",
    "is_bot", "is_deleted_account", "is_self", "peer_identity_id",
)


async def upsert_conversation(
    db: AsyncSession,
    *,
    account_id: int,
    external_id: str,
    title: Optional[str],
    kind: str,
    username: Optional[str] = None,
    last_message_at: Optional[datetime] = None,
    commit: bool = True,
    **channel_fields: Any,
) -> Conversation:
    """Insert or update a conversation in one statement (race-free).

    kind/username always take the new value, title unless None (unknown:
    keep the stored one); the channel-owned fields
    in ``_CHANNEL_OWNED`` only when passed (not None). ``last_activity_at``
    never moves backwards. Pass ``commit=False`` to batch many upserts.
    """
    unknown = set(channel_fields) - set(_CHANNEL_OWNED)
    if unknown:
        raise TypeError(f"upsert_conversation: unknown fields {sorted(unknown)}")
    given = {k: v for k, v in channel_fields.items() if v is not None}
    values = dict(
        account_id=account_id,
        external_id=external_id,
        title=title,
        kind=kind,
        username=username,
        last_activity_at=last_message_at,
        unread_count=0,
        members_count=None,
        is_forum=False,
        slow_mode_seconds=0,
        is_deactivated=False,
        is_archived=False,
        is_bot=False,
        is_deleted_account=False,
        is_self=False,
        peer_identity_id=None,
        created_at=datetime.utcnow(),
    )
    values.update(given)
    stmt = sqlite_insert(Conversation).values(**values)
    ex = stmt.excluded
    set_ = {"title": func.coalesce(ex.title, Conversation.title), "kind": ex.kind, "username": ex.username}
    for col in given:
        set_[col] = getattr(ex, col)
    if last_message_at is not None:
        set_["last_activity_at"] = case(
            (
                or_(Conversation.last_activity_at.is_(None), Conversation.last_activity_at < ex.last_activity_at),
                ex.last_activity_at,
            ),
            else_=Conversation.last_activity_at,
        )
    await db.execute(stmt.on_conflict_do_update(
        index_elements=[Conversation.account_id, Conversation.external_id], set_=set_,
    ))
    if commit:
        await db.commit()
    return (await db.execute(
        select(Conversation)
        .where(Conversation.account_id == account_id, Conversation.external_id == external_id)
        .execution_options(populate_existing=True)
    )).scalar_one()


async def set_conversation_flags(db: AsyncSession, conversation_id: int, **flags: Any) -> Optional[Conversation]:
    """Set user-controlled flags (see CONVERSATION_FLAGS). Hiding a
    conversation also stops monitoring it; unmuting clears ``muted_until``."""
    unknown = set(flags) - set(CONVERSATION_FLAGS)
    if unknown:
        raise TypeError(f"set_conversation_flags: unknown flags {sorted(unknown)}")
    conv = await get_conversation(db, conversation_id)
    if conv is None:
        return None
    for name, value in flags.items():
        setattr(conv, name, value)
    if flags.get("is_hidden"):
        conv.is_monitored = False
    if flags.get("is_muted") is False:
        conv.muted_until = None
    await db.commit()
    await db.refresh(conv)
    return conv


async def mark_read(db: AsyncSession, conversation_id: int) -> Optional[Conversation]:
    conv = await get_conversation(db, conversation_id)
    if conv is None:
        return None
    conv.unread_count = 0
    await db.commit()
    await db.refresh(conv)
    return conv


async def set_pinned_message(
    db: AsyncSession, conversation_id: int, message_id: Optional[int]
) -> Optional[Conversation]:
    conv = await get_conversation(db, conversation_id)
    if conv is None:
        return None
    conv.pinned_message_id = message_id
    await db.commit()
    await db.refresh(conv)
    return conv


async def get_pinned_message(db: AsyncSession, conversation_id: int) -> Optional[Message]:
    conv = await get_conversation(db, conversation_id)
    if conv is None or conv.pinned_message_id is None:
        return None
    return (await db.execute(select(Message).where(Message.id == conv.pinned_message_id))).scalar_one_or_none()


async def touch_synced(db: AsyncSession, conversation_id: int) -> None:
    await db.execute(
        update(Conversation).where(Conversation.id == conversation_id).values(last_synced_at=datetime.utcnow())
    )
    await db.commit()


async def refresh_preview(
    db: AsyncSession, conversation_id: int, only_if_message_ids: Optional[set] = None
) -> None:
    """Re-derive the inbox's cached last-message preview from the newest
    stored message (when it is the previewed one, or among ``only_if_message_ids``)."""
    newest = (await db.execute(
        select(Message.id, Message.text, Message.subject, Message.sender_name, Message.date)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.date.desc(), Message.seq.desc())
        .limit(1)
    )).first()
    if newest is None or (only_if_message_ids is not None and newest.id not in only_if_message_ids):
        return
    await db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id, Conversation.last_message_at == newest.date)
        .values(last_message_text=newest.text or newest.subject, last_message_sender=newest.sender_name)
    )


async def recompute_preview(
    db: AsyncSession, conversation_id: int, removed_through: Optional[datetime] = None
) -> None:
    """Re-derive the cached last-message preview from the newest stored
    message, unconditionally (cleared when none is left) — for after
    messages were deleted. ``last_activity_at`` follows the newest remaining
    message too when its current value came from a removed message (it is
    not later than ``removed_through``, the newest removed message's date);
    activity known from elsewhere (a channel's dialog list) is kept."""
    newest = (await db.execute(
        select(Message.text, Message.subject, Message.sender_name, Message.date)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.date.desc(), Message.seq.desc(), Message.id.desc())
        .limit(1)
    )).first()
    values: dict = {
        "last_message_text": (newest.text or newest.subject) if newest else None,
        "last_message_sender": newest.sender_name if newest else None,
        "last_message_at": newest.date if newest else None,
    }
    await db.execute(update(Conversation).where(Conversation.id == conversation_id).values(**values))
    if removed_through is not None:
        await db.execute(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                or_(Conversation.last_activity_at.is_(None), Conversation.last_activity_at <= removed_through),
            )
            .values(last_activity_at=values["last_message_at"])
        )


# ── History coverage ─────────────────────────────────────────────────────────

async def get_coverage(db: AsyncSession, conversation_ids: Iterable[int]) -> dict[int, HistoryCoverage]:
    """Coverage rows by conversation id; absent = nothing known."""
    ids = list(set(conversation_ids))
    out: dict[int, HistoryCoverage] = {}
    for i in range(0, len(ids), 500):
        res = await db.execute(select(HistoryCoverage).where(HistoryCoverage.conversation_id.in_(ids[i:i + 500])))
        out.update({c.conversation_id: c for c in res.scalars().all()})
    return out


async def get_or_create_coverage(db: AsyncSession, conversation_id: int) -> HistoryCoverage:
    await db.execute(
        sqlite_insert(HistoryCoverage).values(conversation_id=conversation_id, history_complete=False, attempts=0)
        .on_conflict_do_nothing(index_elements=[HistoryCoverage.conversation_id])
    )
    return (await db.execute(
        select(HistoryCoverage).where(HistoryCoverage.conversation_id == conversation_id)
        .execution_options(populate_existing=True)
    )).scalar_one()
