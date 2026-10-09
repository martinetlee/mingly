"""Tags and their assignment to conversations.

Board-column *stage* tags (``category == STAGE_CATEGORY``) are workspace
internals owned by one column each; the generic tag APIs refuse them
(``StageTagLocked``) and user-facing lists hide them.
"""

from typing import Iterable, Optional

from sqlalchemy import delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.crud.conversations import visible_filter
from backend.db.crud.text import contains_ci, name_key
from backend.db.models import (
    Conversation, ConversationTag, Message, Tag, WorkspaceBoard, WorkspaceColumn, WorkspaceTag,
)

__all__ = [
    "STAGE_CATEGORY",
    "StageTagLocked",
    "NameConflict",
    "not_stage",
    "get_all_tags",
    "get_all_tags_with_counts",
    "find_tag_by_name",
    "free_name_from_stage_tag",
    "create_tag",
    "update_tag",
    "delete_tag",
    "find_or_create_tag",
    "get_tags_for_conversation",
    "get_tags_for_conversations",
    "add_tag_to_conversation",
    "add_tag_to_conversations",
    "remove_tag_from_conversation",
    "get_conversations_by_tag_name",
    "search_messages_in_tagged_conversations",
    "get_tag_sync_coverage",
]

STAGE_CATEGORY = "stage"  # board-column-owned tags; internal, never user-assigned


class StageTagLocked(ValueError):
    """A board-column stage tag was targeted through a generic tag API; it can
    only be changed through its column (rename/delete column, move cards)."""

    code = "stage_tag_locked"

    def __init__(self, message: str = "This tag belongs to a board column; change it from the board"):
        super().__init__(message)


class NameConflict(ValueError):
    """A tag/organization with the same case/whitespace-insensitive name exists."""

    def __init__(self, message: str, existing_id: Optional[int] = None):
        super().__init__(message)
        self.existing_id = existing_id


def not_stage():
    return or_(Tag.category.is_(None), Tag.category != STAGE_CATEGORY)


async def get_all_tags(db: AsyncSession) -> list[Tag]:
    result = await db.execute(select(Tag).order_by(Tag.is_default.desc(), Tag.name))
    return list(result.scalars().all())


async def get_all_tags_with_counts(db: AsyncSession, include_stage: bool = True) -> list[tuple[Tag, int]]:
    """All tags plus the number of conversations each would show in the inbox
    (same exclusions as crud.list_conversations). ``include_stage=False``
    hides board-column stage tags (user-facing lists)."""
    q = (
        select(Tag, func.count(Conversation.id))
        .outerjoin(ConversationTag, ConversationTag.tag_id == Tag.id)
        .outerjoin(Conversation, (Conversation.id == ConversationTag.conversation_id) & visible_filter())
        .group_by(Tag.id)
        .order_by(Tag.is_default.desc(), Tag.name)
    )
    if not include_stage:
        q = q.where(not_stage())
    return [(tag, count) for tag, count in (await db.execute(q)).all()]


async def find_tag_by_name(db: AsyncSession, name: str, include_stage: bool = False) -> Optional[Tag]:
    """User-facing tag lookup by name. Stage tags are board internals and are
    skipped unless asked for, so a column can never be resolved by name."""
    q = select(Tag).where(name_key(Tag.name) == name_key(name.strip()))
    if not include_stage:
        q = q.where(not_stage())
    return (await db.execute(q.order_by(Tag.id).limit(1))).scalar_one_or_none()


async def free_name_from_stage_tag(db: AsyncSession, name: str) -> None:
    """Stage tags may hold plain names ("Done"); rename one out of the way so a
    user/stream tag can take the name. Column labels are unaffected."""
    holder = (await db.execute(select(Tag).where(
        name_key(Tag.name) == name_key(name.strip()), Tag.category == STAGE_CATEGORY,
    ).limit(1))).scalar_one_or_none()
    if holder is None:
        return
    candidate = f"{holder.name.strip()} · stage {holder.id}"
    n = 1
    while (await db.execute(select(func.count()).select_from(Tag).where(
        name_key(Tag.name) == name_key(candidate)
    ))).scalar():
        n += 1
        candidate = f"{holder.name.strip()} · stage {holder.id}.{n}"
    holder.name = candidate
    await db.flush()


async def create_tag(db: AsyncSession, name: str, color: str = "slate", category: Optional[str] = None) -> Tag:
    """Create a user/stream tag. Raises ValueError on an empty name or the
    reserved stage category, NameConflict if the name is taken."""
    name = name.strip()
    if not name:
        raise ValueError("Tag name cannot be empty")
    if category == STAGE_CATEGORY:
        raise ValueError("Stage tags are managed by board columns")
    existing = await find_tag_by_name(db, name)
    if existing is not None:
        raise NameConflict(f'A tag named "{existing.name}" already exists', existing.id)
    await free_name_from_stage_tag(db, name)
    tag = Tag(name=name, color=color, category=category, is_default=False)
    db.add(tag)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise NameConflict(f'A tag named "{name}" already exists')
    await db.refresh(tag)
    return tag


async def update_tag(
    db: AsyncSession,
    tag_id: int,
    name: Optional[str] = None,
    color: Optional[str] = None,
    category: Optional[str] = None,
) -> Optional[Tag]:
    """Raises ValueError / NameConflict like create_tag, StageTagLocked for a
    board-column stage tag (rename the column instead)."""
    tag = await db.get(Tag, tag_id)
    if tag is None:
        return None
    if tag.category == STAGE_CATEGORY:
        raise StageTagLocked()
    if category is not None:
        new_category = category.strip() or None
        if new_category == STAGE_CATEGORY:
            raise ValueError("Stage tags are managed by board columns")
        tag.category = new_category
    if name is not None:
        new_name = name.strip()
        if not new_name:
            raise ValueError("Tag name cannot be empty")
        clash = (await db.execute(select(Tag).where(
            name_key(Tag.name) == name_key(new_name), Tag.id != tag_id, not_stage(),
        ).limit(1))).scalar_one_or_none()
        if clash is not None:
            raise NameConflict(f'A tag named "{clash.name}" already exists', clash.id)
        await free_name_from_stage_tag(db, new_name)
        tag.name = new_name
    if color is not None:
        tag.color = color
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise NameConflict("A tag with that name already exists")
    await db.refresh(tag)
    return tag


async def delete_tag(db: AsyncSession, tag_id: int) -> bool:
    """Raises StageTagLocked for a board-column stage tag."""
    tag = await db.get(Tag, tag_id)
    if tag is None:
        return False
    if tag.category == STAGE_CATEGORY:
        raise StageTagLocked()
    # Assignments, workspace rules and columns cascade by FK; board scopes are SET NULL.
    # Explicit here too so the session never holds stale rows.
    await db.execute(delete(ConversationTag).where(ConversationTag.tag_id == tag_id))
    await db.execute(delete(WorkspaceTag).where(WorkspaceTag.tag_id == tag_id))
    await db.execute(delete(WorkspaceColumn).where(WorkspaceColumn.tag_id == tag_id))
    await db.execute(update(WorkspaceBoard).where(WorkspaceBoard.scope_tag_id == tag_id).values(scope_tag_id=None))
    await db.delete(tag)
    await db.commit()
    return True


async def find_or_create_tag(db: AsyncSession, tag_name: str):
    """User tag by name (never a board stage tag), created if missing.
    Returns (tag, None) or (None, readable error)."""
    tag = await find_tag_by_name(db, tag_name)
    if tag is not None:
        return tag, None
    try:
        return await create_tag(db, name=tag_name), None
    except NameConflict as exc:  # created concurrently
        await db.rollback()
        tag = await find_tag_by_name(db, tag_name)
        return (tag, None) if tag is not None else (None, str(exc))
    except ValueError as exc:
        await db.rollback()
        return None, str(exc)


# ── Conversation tags ────────────────────────────────────────────────────────

async def get_tags_for_conversation(
    db: AsyncSession, conversation_id: int, include_stage: bool = True
) -> list[Tag]:
    q = (
        select(Tag)
        .join(ConversationTag, Tag.id == ConversationTag.tag_id)
        .where(ConversationTag.conversation_id == conversation_id)
        .order_by(Tag.name)
    )
    if not include_stage:
        q = q.where(not_stage())
    return list((await db.execute(q)).scalars().all())


async def get_tags_for_conversations(
    db: AsyncSession, conversation_ids: Iterable[int], include_stage: bool = True
) -> dict[int, list[Tag]]:
    """Tags of many conversations, keyed by conversation id (every id present)."""
    ids = list(dict.fromkeys(conversation_ids))
    out: dict[int, list[Tag]] = {cid: [] for cid in ids}
    for i in range(0, len(ids), 500):
        q = (
            select(ConversationTag.conversation_id, Tag)
            .join(Tag, ConversationTag.tag_id == Tag.id)
            .where(ConversationTag.conversation_id.in_(ids[i:i + 500]))
            .order_by(Tag.name)
        )
        if not include_stage:
            q = q.where(not_stage())
        for cid, tag in (await db.execute(q)).all():
            out[cid].append(tag)
    return out


async def add_tag_to_conversation(db: AsyncSession, conversation_id: int, tag_id: int) -> Optional[ConversationTag]:
    """None if the tag or conversation doesn't exist; StageTagLocked for a
    stage tag (cards are placed with move_board_conversation)."""
    tag = await db.get(Tag, tag_id)
    if tag is None or await db.get(Conversation, conversation_id) is None:
        return None
    if tag.category == STAGE_CATEGORY:
        raise StageTagLocked()
    lookup = select(ConversationTag).where(
        ConversationTag.conversation_id == conversation_id, ConversationTag.tag_id == tag_id
    )
    existing = (await db.execute(lookup)).scalar_one_or_none()
    if existing is not None:
        return existing
    ct = ConversationTag(conversation_id=conversation_id, tag_id=tag_id)
    db.add(ct)
    try:
        await db.commit()
    except IntegrityError:
        # Concurrent add of the same tag: the row exists, which is the goal
        await db.rollback()
        return (await db.execute(lookup)).scalar_one_or_none()
    await db.refresh(ct)
    return ct


async def add_tag_to_conversations(db: AsyncSession, conversation_ids: list[int], tag_id: int) -> int:
    """Bulk-assign a tag. Returns the number of conversations newly tagged.
    Raises StageTagLocked for a stage tag."""
    if not conversation_ids:
        return 0
    category = (await db.execute(select(Tag.category).where(Tag.id == tag_id))).scalar()
    if category == STAGE_CATEGORY:
        raise StageTagLocked()
    ids = list(dict.fromkeys(conversation_ids))
    already = set((await db.execute(
        select(ConversationTag.conversation_id).where(
            ConversationTag.conversation_id.in_(ids), ConversationTag.tag_id == tag_id
        )
    )).scalars().all())
    missing = [cid for cid in ids if cid not in already]
    db.add_all(ConversationTag(conversation_id=cid, tag_id=tag_id) for cid in missing)
    await db.commit()
    return len(missing)


async def remove_tag_from_conversation(db: AsyncSession, conversation_id: int, tag_id: int) -> bool:
    """Raises StageTagLocked for a stage tag (use move_board_conversation)."""
    category = (await db.execute(select(Tag.category).where(Tag.id == tag_id))).scalar()
    if category == STAGE_CATEGORY:
        raise StageTagLocked()
    ct = (await db.execute(select(ConversationTag).where(
        ConversationTag.conversation_id == conversation_id, ConversationTag.tag_id == tag_id
    ))).scalar_one_or_none()
    if ct is None:
        return False
    await db.delete(ct)
    await db.commit()
    return True


# ── Tag-scoped queries (intelligence tools) ──────────────────────────────────

def _tagged_conversation_ids(tag_name: str):
    """Conversation ids carrying a user tag whose name contains ``tag_name``."""
    return (
        select(ConversationTag.conversation_id)
        .join(Tag, ConversationTag.tag_id == Tag.id)
        .where(contains_ci(Tag.name, tag_name), not_stage())
    )


async def get_conversations_by_tag_name(db: AsyncSession, tag_name: str) -> list[Conversation]:
    """Live conversations carrying a tag whose name contains ``tag_name``
    (case-insensitive), by title."""
    return list((await db.execute(
        select(Conversation)
        .where(Conversation.id.in_(_tagged_conversation_ids(tag_name)), Conversation.is_deactivated == False)  # noqa: E712
        .order_by(Conversation.title)
    )).scalars().all())


async def search_messages_in_tagged_conversations(db: AsyncSession, tag_name: str, keyword: str) -> list:
    """Per-conversation rollup of keyword matches within conversations carrying a tag.

    Rows ``(conversation_id, title, match_count, latest_snippet, latest_date)``,
    most matches first.
    """
    row_num = func.row_number().over(
        partition_by=Message.conversation_id, order_by=(desc(Message.date), desc(Message.id)),
    ).label("rn")
    matches = (
        select(Message.conversation_id, Message.text.label("snippet"), Message.date.label("msg_date"), row_num)
        .where(contains_ci(Message.text, keyword), Message.conversation_id.in_(_tagged_conversation_ids(tag_name)))
        .subquery()
    )
    return list((await db.execute(
        select(
            matches.c.conversation_id,
            Conversation.title,
            func.count().label("match_count"),
            func.max(matches.c.snippet).filter(matches.c.rn == 1).label("latest_snippet"),
            func.max(matches.c.msg_date).label("latest_date"),
        )
        .select_from(matches)
        .join(Conversation, Conversation.id == matches.c.conversation_id)
        .group_by(matches.c.conversation_id, Conversation.title)
        .order_by(desc("match_count"))
    )).all())


async def get_tag_sync_coverage(db: AsyncSession, tag_name: str) -> tuple[int, int, list[int]]:
    """Sync coverage for live conversations carrying a tag:
    ``(total_tagged, with_messages, unsynced_conversation_ids)``."""
    tagged = (
        select(Conversation.id, Conversation.last_synced_at)
        .where(Conversation.id.in_(_tagged_conversation_ids(tag_name)), Conversation.is_deactivated == False)  # noqa: E712
        .subquery()
    )
    total = (await db.execute(select(func.count()).select_from(tagged))).scalar() or 0
    with_messages = (await db.execute(
        select(func.count(func.distinct(Message.conversation_id)))
        .where(Message.conversation_id.in_(select(tagged.c.id)))
    )).scalar() or 0
    unsynced = list((await db.execute(
        select(tagged.c.id).where(tagged.c.last_synced_at.is_(None))
    )).scalars().all())
    return total, with_messages, unsynced
