"""Workspaces: tag-defined groups of conversations, optionally shown as
kanban boards.

A workspace's members are the conversations carrying any of its include
tags. Each board column owns one fresh stage tag (``STAGE_CATEGORY``);
placing a card on a column assigns that tag. Boards may be scoped by a
stream tag. Membership and stream tags are *protected*: board mutations
never strip or delete them.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.db.crud.tags import STAGE_CATEGORY, find_tag_by_name, free_name_from_stage_tag
from backend.db.crud.text import name_key
from backend.db.models import (
    Conversation, ConversationTag, Tag, Workspace, WorkspaceBoard, WorkspaceColumn,
    WorkspaceConversationOverride, WorkspaceTag,
)

__all__ = [
    "get_workspaces_with_counts",
    "workspace_member_ids",
    "create_workspace",
    "update_workspace",
    "delete_workspace",
    "get_workspace_boards",
    "create_workspace_board",
    "update_workspace_board",
    "delete_workspace_board",
    "get_board",
    "add_board_column",
    "update_board_column",
    "reorder_board_columns",
    "delete_board_column",
    "move_board_conversation",
    "mark_workspace_triage",
]


def _live_filter():
    """Visibility exclusions for workspace members (archived ones stay)."""
    return (
        (Conversation.is_hidden == False)  # noqa: E712
        & (Conversation.is_briefing == False)  # noqa: E712
        & (Conversation.is_deactivated == False)  # noqa: E712
    )


def workspace_member_ids(workspace_id: int):
    """Subquery of conversation ids carrying any of a workspace's include tags."""
    include_tags = select(WorkspaceTag.tag_id).where(
        WorkspaceTag.workspace_id == workspace_id,
        WorkspaceTag.is_exclusion == False,  # noqa: E712
    )
    return select(ConversationTag.conversation_id).where(ConversationTag.tag_id.in_(include_tags))


def _workspace_stage_tag_ids(workspace_id: int):
    return select(WorkspaceColumn.tag_id).where(WorkspaceColumn.workspace_id == workspace_id)


def _board_stage_tag_ids(board_id: int):
    return select(WorkspaceColumn.tag_id).where(WorkspaceColumn.board_id == board_id)


def _protected_tag_ids():
    """Tags with meaning beyond one board column — workspace membership rules
    and board stream scopes. Board mutations never strip or delete these."""
    return select(WorkspaceTag.tag_id).union(
        select(WorkspaceBoard.scope_tag_id).where(WorkspaceBoard.scope_tag_id.isnot(None))
    )


async def _drop_unused_stage_tags(db: AsyncSession, tag_ids: list[int]) -> None:
    """A column's stage tag lives and dies with its last column: delete it
    (its card placements cascade) once no column uses it. Never touches
    protected or non-stage tags."""
    if not tag_ids:
        return
    doomed = list((await db.execute(select(Tag.id).where(
        Tag.id.in_(tag_ids),
        Tag.category == STAGE_CATEGORY,
        Tag.id.notin_(select(WorkspaceColumn.tag_id)),
        Tag.id.notin_(_protected_tag_ids()),
    ))).scalars().all())
    if doomed:
        await db.execute(delete(ConversationTag).where(ConversationTag.tag_id.in_(doomed)))
        await db.execute(delete(Tag).where(Tag.id.in_(doomed)))


async def _validate_membership_tags(db: AsyncSession, tag_ids: list[int]) -> list[int]:
    """De-duplicated include tags; stage tags (board internals) are rejected."""
    unique_ids = list(dict.fromkeys(tag_ids))
    if not unique_ids:
        return []
    found = dict((await db.execute(select(Tag.id, Tag.category).where(Tag.id.in_(unique_ids)))).all())
    missing = [tid for tid in unique_ids if tid not in found]
    if missing:
        raise ValueError(f"Unknown tag id(s): {', '.join(map(str, missing))}")
    if any(cat == STAGE_CATEGORY for cat in found.values()):
        raise ValueError("Board column tags cannot be used as workspace tags")
    return unique_ids


async def get_workspaces_with_counts(db: AsyncSession) -> list[dict]:
    """All workspaces ordered by position, each with tag_ids, member and
    unallocated counts. Constant number of queries regardless of workspace count."""
    workspaces = list((await db.execute(
        select(Workspace).order_by(Workspace.position, Workspace.id)
    )).scalars().all())
    if not workspaces:
        return []

    tag_ids_by_ws: dict[int, list[int]] = {}
    for ws_id, tag_id in (await db.execute(
        select(WorkspaceTag.workspace_id, WorkspaceTag.tag_id)
        .where(WorkspaceTag.is_exclusion == False)  # noqa: E712
        .order_by(WorkspaceTag.id)
    )).all():
        tag_ids_by_ws.setdefault(ws_id, []).append(tag_id)

    member_counts = dict((await db.execute(
        select(WorkspaceTag.workspace_id, func.count(func.distinct(Conversation.id)))
        .join(ConversationTag, ConversationTag.tag_id == WorkspaceTag.tag_id)
        .join(Conversation, Conversation.id == ConversationTag.conversation_id)
        .where(WorkspaceTag.is_exclusion == False, _live_filter())  # noqa: E712
        .group_by(WorkspaceTag.workspace_id)
    )).all())

    member_ct = aliased(ConversationTag)
    stage_ct = aliased(ConversationTag)
    is_member = (
        select(member_ct.id)
        .join(WorkspaceTag, WorkspaceTag.tag_id == member_ct.tag_id)
        .where(
            member_ct.conversation_id == Conversation.id,
            WorkspaceTag.workspace_id == WorkspaceConversationOverride.workspace_id,
            WorkspaceTag.is_exclusion == False,  # noqa: E712
        )
        .exists()
    )
    is_staged = (
        select(stage_ct.id)
        .join(WorkspaceColumn, WorkspaceColumn.tag_id == stage_ct.tag_id)
        .where(
            stage_ct.conversation_id == Conversation.id,
            WorkspaceColumn.workspace_id == WorkspaceConversationOverride.workspace_id,
        )
        .exists()
    )
    unallocated_counts = dict((await db.execute(
        select(WorkspaceConversationOverride.workspace_id, func.count(func.distinct(Conversation.id)))
        .join(Conversation, Conversation.id == WorkspaceConversationOverride.conversation_id)
        .join(Workspace, Workspace.id == WorkspaceConversationOverride.workspace_id)
        .where(
            Workspace.view_mode == "kanban",
            WorkspaceConversationOverride.needs_triage_since.isnot(None),
            _live_filter(),
            is_member,
            ~is_staged,
        )
        .group_by(WorkspaceConversationOverride.workspace_id)
    )).all())

    return [
        {"workspace": ws, "tag_ids": tag_ids_by_ws.get(ws.id, []),
         "member_count": member_counts.get(ws.id, 0),
         "unallocated_count": unallocated_counts.get(ws.id, 0)}
        for ws in workspaces
    ]


async def create_workspace(
    db: AsyncSession, name: str, emoji: Optional[str], color: str, tag_ids: list[int], view_mode: str = "list"
) -> Workspace:
    """Raises ValueError for unknown or stage tag_ids."""
    tag_ids = await _validate_membership_tags(db, tag_ids)
    max_pos = (await db.execute(select(func.max(Workspace.position)))).scalar()
    ws = Workspace(
        name=name, emoji=emoji or None, color=color, view_mode=view_mode,
        position=(max_pos or 0) + 1,
        board_watermark=datetime.utcnow() if view_mode == "kanban" else None,
    )
    db.add(ws)
    await db.flush()
    for tid in tag_ids:
        db.add(WorkspaceTag(workspace_id=ws.id, tag_id=tid))
    if view_mode == "kanban":
        db.add(WorkspaceBoard(workspace_id=ws.id, name="Main", position=0))
    await db.commit()
    await db.refresh(ws)
    return ws


async def update_workspace(
    db: AsyncSession, workspace_id: int,
    name: Optional[str] = None, emoji: Optional[str] = None, color: Optional[str] = None,
    tag_ids: Optional[list[int]] = None, view_mode: Optional[str] = None,
    clear_emoji: bool = False,
) -> Optional[Workspace]:
    """None = unchanged; ``clear_emoji`` removes the emoji. Raises ValueError
    for unknown or stage tag_ids."""
    ws = await db.get(Workspace, workspace_id)
    if ws is None:
        return None
    if tag_ids is not None:
        tag_ids = await _validate_membership_tags(db, tag_ids)
    if name is not None:
        ws.name = name
    if clear_emoji:
        ws.emoji = None
    elif emoji is not None:
        ws.emoji = emoji
    if color is not None:
        ws.color = color
    if view_mode is not None:
        if view_mode == "kanban" and ws.view_mode != "kanban":
            if ws.board_watermark is None:
                ws.board_watermark = datetime.utcnow()  # board starts clean; only future activity triages
            has_board = (await db.execute(
                select(func.count()).select_from(WorkspaceBoard).where(WorkspaceBoard.workspace_id == workspace_id)
            )).scalar() or 0
            if not has_board:
                db.add(WorkspaceBoard(workspace_id=workspace_id, name="Main", position=0))
        ws.view_mode = view_mode
    if tag_ids is not None:
        await db.execute(delete(WorkspaceTag).where(
            WorkspaceTag.workspace_id == workspace_id,
            WorkspaceTag.is_exclusion == False,  # noqa: E712
        ))
        for tid in tag_ids:
            db.add(WorkspaceTag(workspace_id=workspace_id, tag_id=tid))
    await db.commit()
    await db.refresh(ws)
    return ws


async def delete_workspace(db: AsyncSession, workspace_id: int) -> bool:
    ws = await db.get(Workspace, workspace_id)
    if ws is None:
        return False
    stage_ids = list((await db.execute(_workspace_stage_tag_ids(workspace_id))).scalars().all())
    await db.execute(delete(WorkspaceTag).where(WorkspaceTag.workspace_id == workspace_id))
    await db.execute(delete(WorkspaceColumn).where(WorkspaceColumn.workspace_id == workspace_id))
    await db.execute(delete(WorkspaceBoard).where(WorkspaceBoard.workspace_id == workspace_id))
    await db.execute(delete(WorkspaceConversationOverride).where(
        WorkspaceConversationOverride.workspace_id == workspace_id
    ))
    # The columns' stage tags (and their card placements) die with the workspace
    await _drop_unused_stage_tags(db, stage_ids)
    await db.delete(ws)
    await db.commit()
    return True


async def get_workspace_boards(db: AsyncSession, workspace_id: int) -> list[tuple]:
    """(WorkspaceBoard, scope_tag_name|None) ordered by position."""
    return list((await db.execute(
        select(WorkspaceBoard, Tag.name)
        .outerjoin(Tag, Tag.id == WorkspaceBoard.scope_tag_id)
        .where(WorkspaceBoard.workspace_id == workspace_id)
        .order_by(WorkspaceBoard.position, WorkspaceBoard.id)
    )).all())


async def _resolve_stream_tag(db: AsyncSession, name: str) -> Tag:
    """Stream (scope) tag by name: reuses a user/stream tag, never a stage
    tag; creates a 'stream' tag otherwise."""
    name = name.strip()
    tag = await find_tag_by_name(db, name)
    if tag is None:
        await free_name_from_stage_tag(db, name)
        tag = Tag(name=name, color="slate", category="stream", is_default=False)
        db.add(tag)
        await db.flush()
    return tag


async def create_workspace_board(
    db: AsyncSession, workspace_id: int, name: str, scope_tag_name: Optional[str] = None
) -> Optional[WorkspaceBoard]:
    """Returns None on duplicate name within the workspace."""
    dup = (await db.execute(select(func.count()).select_from(WorkspaceBoard).where(
        WorkspaceBoard.workspace_id == workspace_id, name_key(WorkspaceBoard.name) == name_key(name)
    ))).scalar() or 0
    if dup:
        return None
    scope_tag_id = None
    if scope_tag_name and scope_tag_name.strip():
        scope_tag_id = (await _resolve_stream_tag(db, scope_tag_name)).id
    max_pos = (await db.execute(
        select(func.max(WorkspaceBoard.position)).where(WorkspaceBoard.workspace_id == workspace_id)
    )).scalar()
    board = WorkspaceBoard(workspace_id=workspace_id, name=name, scope_tag_id=scope_tag_id,
                           position=(max_pos or 0) + 1)
    db.add(board)
    await db.commit()
    await db.refresh(board)
    return board


async def _get_board_row(db: AsyncSession, workspace_id: int, board_id: int) -> Optional[WorkspaceBoard]:
    return (await db.execute(select(WorkspaceBoard).where(
        WorkspaceBoard.id == board_id, WorkspaceBoard.workspace_id == workspace_id
    ))).scalar_one_or_none()


async def update_workspace_board(
    db: AsyncSession, workspace_id: int, board_id: int,
    name: Optional[str] = None, scope_tag_name: Optional[str] = None,
    clear_scope: bool = False, position: Optional[int] = None,
) -> str:
    """Returns 'ok', 'not_found' or 'conflict' (another board has that name)."""
    board = await _get_board_row(db, workspace_id, board_id)
    if board is None:
        return "not_found"
    if name is not None:
        dup = (await db.execute(select(func.count()).select_from(WorkspaceBoard).where(
            WorkspaceBoard.workspace_id == workspace_id, WorkspaceBoard.id != board_id,
            name_key(WorkspaceBoard.name) == name_key(name),
        ))).scalar() or 0
        if dup:
            return "conflict"
        board.name = name
    if clear_scope:
        board.scope_tag_id = None
    elif scope_tag_name and scope_tag_name.strip():
        board.scope_tag_id = (await _resolve_stream_tag(db, scope_tag_name)).id
    if position is not None:
        board.position = position
    await db.commit()
    return "ok"


async def delete_workspace_board(db: AsyncSession, workspace_id: int, board_id: int) -> bool:
    """Deletes the board, its columns and their stage tags (cards return to backlog)."""
    board = await _get_board_row(db, workspace_id, board_id)
    if board is None:
        return False
    stage_ids = list((await db.execute(_board_stage_tag_ids(board_id))).scalars().all())
    await db.execute(delete(WorkspaceColumn).where(WorkspaceColumn.board_id == board_id))
    await db.execute(delete(WorkspaceBoard).where(WorkspaceBoard.id == board_id))
    await _drop_unused_stage_tags(db, stage_ids)
    await db.commit()
    return True


async def get_board(db: AsyncSession, workspace_id: int, board_id: Optional[int] = None) -> Optional[dict]:
    """One board of a workspace: its columns, unallocated (triage) and backlog
    conversations, plus the boards list for tab rendering (with per-tab
    unallocated counts). Conversations are ORM rows, most recent first."""
    ws = await db.get(Workspace, workspace_id)
    if ws is None:
        return None
    board_rows = await get_workspace_boards(db, workspace_id)
    active = None
    if board_rows:
        active = next((b for b, _n in board_rows if b.id == board_id), None) or board_rows[0][0]

    members = list((await db.execute(
        select(Conversation).where(Conversation.id.in_(workspace_member_ids(workspace_id)), _live_filter())
    )).scalars().all())
    member_ids = [c.id for c in members]

    # triage flags (workspace-level)
    triage: dict[int, datetime] = {}
    if member_ids:
        triage = dict((await db.execute(
            select(WorkspaceConversationOverride.conversation_id, WorkspaceConversationOverride.needs_triage_since)
            .where(
                WorkspaceConversationOverride.workspace_id == workspace_id,
                WorkspaceConversationOverride.needs_triage_since.isnot(None),
                WorkspaceConversationOverride.conversation_id.in_(member_ids),
            )
        )).all())

    # which conversations carry which of this workspace's stream (scope) tags
    scope_tag_ids = [b.scope_tag_id for b, _n in board_rows if b.scope_tag_id is not None]
    scope_carriers: dict[int, set[int]] = {tid: set() for tid in scope_tag_ids}
    if scope_tag_ids and member_ids:
        for cid, tid in (await db.execute(
            select(ConversationTag.conversation_id, ConversationTag.tag_id).where(
                ConversationTag.tag_id.in_(scope_tag_ids), ConversationTag.conversation_id.in_(member_ids)
            )
        )).all():
            scope_carriers[tid].add(cid)
    has_any_stream: set[int] = set().union(*scope_carriers.values()) if scope_carriers else set()

    # Conversations staged on ANY board of this workspace (dragging onto a
    # board is itself a classification decision, even before stream tags exist)
    staged_anywhere: set[int] = set()
    if member_ids:
        staged_anywhere = set((await db.execute(
            select(ConversationTag.conversation_id).where(
                ConversationTag.tag_id.in_(_workspace_stage_tag_ids(workspace_id)),
                ConversationTag.conversation_id.in_(member_ids),
            )
        )).scalars().all())

    def _unallocated_ids_for(board) -> set[int]:
        # stream-less triage conversations surface on every tab; streamed ones only on their own
        eligible = set()
        for cid in triage:
            if cid not in has_any_stream:
                eligible.add(cid)
            elif board.scope_tag_id is not None and cid in scope_carriers.get(board.scope_tag_id, set()):
                eligible.add(cid)
        return eligible

    boards_out = [
        {"id": b.id, "name": b.name, "scope_tag_id": b.scope_tag_id, "scope_tag_name": scope_name,
         "position": b.position, "unallocated_count": len(_unallocated_ids_for(b))}
        for b, scope_name in board_rows
    ]

    if active is None:
        return {"workspace": ws, "boards": boards_out, "board_id": None, "columns": [], "unallocated": [],
                "backlog": {"active": [], "cooling": [], "dormant": []}, "classified_conversation_ids": []}

    columns = list((await db.execute(
        select(WorkspaceColumn, Tag)
        .join(Tag, Tag.id == WorkspaceColumn.tag_id)
        .where(WorkspaceColumn.board_id == active.id)
        .order_by(WorkspaceColumn.position, WorkspaceColumn.id)
    )).all())
    stage_tag_ids = [col.tag_id for col, _tag in columns]

    # conversation -> stage tag (within this board)
    staged: dict[int, int] = {}
    if stage_tag_ids and member_ids:
        for cid, tid in (await db.execute(
            select(ConversationTag.conversation_id, ConversationTag.tag_id).where(
                ConversationTag.tag_id.in_(stage_tag_ids), ConversationTag.conversation_id.in_(member_ids)
            )
        )).all():
            staged.setdefault(cid, tid)

    unallocated_ids = _unallocated_ids_for(active)
    active_scope = scope_carriers.get(active.scope_tag_id, set()) if active.scope_tag_id is not None else None

    by_column: dict[int, list[Conversation]] = {tid: [] for tid in stage_tag_ids}
    unallocated: list[Conversation] = []
    backlog: list[Conversation] = []
    for c in members:
        if c.id in staged:
            by_column[staged[c.id]].append(c)
        elif c.id in unallocated_ids:
            unallocated.append(c)
        elif active_scope is None or c.id in active_scope:
            backlog.append(c)

    def _recent_first(convs: list[Conversation]) -> list[Conversation]:
        return sorted(convs, key=lambda c: c.last_activity_at or datetime.min, reverse=True)

    now = datetime.utcnow()
    b_active, b_cooling, b_dormant = [], [], []
    for c in _recent_first(backlog):
        age = (now - c.last_activity_at).days if c.last_activity_at else None
        if age is not None and age < 7:
            b_active.append(c)
        elif age is not None and age < 30:
            b_cooling.append(c)
        else:
            b_dormant.append(c)

    return {
        "workspace": ws,
        "boards": boards_out,
        "board_id": active.id,
        "columns": [
            {"tag_id": col.tag_id, "name": col.label or tag.name, "color": col.color or tag.color,
             "position": col.position, "conversations": _recent_first(by_column[col.tag_id])}
            for col, tag in columns
        ],
        "unallocated": _recent_first(unallocated),
        "backlog": {"active": b_active, "cooling": b_cooling, "dormant": b_dormant},
        # Members already classified — carrying a stream tag OR staged on any
        # of this workspace's boards ("unclassified only" backlog view).
        "classified_conversation_ids": sorted(has_any_stream | staged_anywhere),
    }


async def _label_taken(db: AsyncSession, board_id: int, label: str, except_col_id: Optional[int] = None) -> bool:
    """One label per board, case/whitespace-insensitively."""
    q = (
        select(func.count()).select_from(WorkspaceColumn)
        .join(Tag, Tag.id == WorkspaceColumn.tag_id)
        .where(WorkspaceColumn.board_id == board_id,
               name_key(func.coalesce(WorkspaceColumn.label, Tag.name)) == name_key(label))
    )
    if except_col_id is not None:
        q = q.where(WorkspaceColumn.id != except_col_id)
    return bool((await db.execute(q)).scalar())


async def add_board_column(
    db: AsyncSession, workspace_id: int, board_id: int, name: str, color: Optional[str]
) -> tuple[str, Optional[dict]]:
    """Attach a column with display label ``name``. Returns (status, column)
    with status 'ok', 'not_found' or 'conflict' (label already on this board).

    Every column owns a freshly minted stage tag — never one resolved by name
    — so dragging cards or deleting the column only ever affects this
    column's own placements. The tag's internal name is unique ("Done", else
    "Done · <board>", ...); the label is what the board shows.
    """
    board = await _get_board_row(db, workspace_id, board_id)
    if board is None:
        return "not_found", None
    if await _label_taken(db, board_id, name):
        return "conflict", None

    candidate = name
    n = 1
    while (await db.execute(select(func.count()).select_from(Tag).where(
        name_key(Tag.name) == name_key(candidate)
    ))).scalar():
        n += 1
        candidate = f"{name} · {board.name}" if n == 2 else f"{name} · {board.name} {n - 1}"
    tag = Tag(name=candidate, color=color or "slate", category=STAGE_CATEGORY, is_default=False)
    db.add(tag)
    await db.flush()

    max_pos = (await db.execute(
        select(func.max(WorkspaceColumn.position)).where(WorkspaceColumn.board_id == board_id)
    )).scalar()
    col = WorkspaceColumn(workspace_id=workspace_id, tag_id=tag.id, board_id=board_id,
                          label=name, position=(max_pos or 0) + 1, color=color)
    db.add(col)
    await db.commit()
    return "ok", {"tag_id": tag.id, "name": name, "color": col.color or tag.color, "position": col.position}


async def _get_column(db: AsyncSession, workspace_id: int, board_id: int, tag_id: int) -> Optional[WorkspaceColumn]:
    return (await db.execute(select(WorkspaceColumn).where(
        WorkspaceColumn.workspace_id == workspace_id, WorkspaceColumn.tag_id == tag_id,
        WorkspaceColumn.board_id == board_id,
    ))).scalar_one_or_none()


async def update_board_column(
    db: AsyncSession, workspace_id: int, board_id: int, tag_id: int,
    name: Optional[str] = None, color: Optional[str] = None, position: Optional[int] = None,
) -> str:
    """Returns 'ok', 'not_found' or 'conflict' (label already on this board).
    Rename changes the display label; the backing stage tag follows when the
    name is free (it is internal, so a clash just leaves the old tag name)."""
    col = await _get_column(db, workspace_id, board_id, tag_id)
    if col is None:
        return "not_found"
    if name is not None:
        if await _label_taken(db, board_id, name, except_col_id=col.id):
            return "conflict"
        col.label = name
        tag = await db.get(Tag, tag_id)
        name_free = ((await db.execute(select(func.count()).select_from(Tag).where(
            name_key(Tag.name) == name_key(name), Tag.id != tag_id
        ))).scalar() or 0) == 0
        sole_user = ((await db.execute(select(func.count()).select_from(WorkspaceColumn).where(
            WorkspaceColumn.tag_id == tag_id, WorkspaceColumn.id != col.id
        ))).scalar() or 0) == 0
        if tag is not None and tag.category == STAGE_CATEGORY and sole_user and name_free:
            tag.name = name
    if color is not None:
        col.color = color
    if position is not None:
        col.position = position
    await db.commit()
    return "ok"


async def reorder_board_columns(db: AsyncSession, workspace_id: int, board_id: int, tag_ids: list[int]) -> str:
    """Set column order in one transaction. ``tag_ids`` must list exactly the
    board's columns. Returns 'ok', 'not_found' or 'mismatch'."""
    if await _get_board_row(db, workspace_id, board_id) is None:
        return "not_found"
    cols = list((await db.execute(select(WorkspaceColumn).where(
        WorkspaceColumn.workspace_id == workspace_id, WorkspaceColumn.board_id == board_id,
    ))).scalars().all())
    by_tag = {c.tag_id: c for c in cols}
    if len(tag_ids) != len(set(tag_ids)) or set(tag_ids) != set(by_tag):
        return "mismatch"
    for pos, tid in enumerate(tag_ids):
        by_tag[tid].position = pos
    await db.commit()
    return "ok"


async def delete_board_column(db: AsyncSession, workspace_id: int, board_id: int, tag_id: int) -> bool:
    """Delete a column; its stage tag and card placements go with it (cards
    return to backlog). A protected or still-shared tag is never stripped."""
    col = await _get_column(db, workspace_id, board_id, tag_id)
    if col is None:
        return False
    await db.execute(delete(WorkspaceColumn).where(WorkspaceColumn.id == col.id))
    await _drop_unused_stage_tags(db, [tag_id])
    await db.commit()
    return True


async def _override(db: AsyncSession, workspace_id: int, conversation_id: int) -> Optional[WorkspaceConversationOverride]:
    return (await db.execute(select(WorkspaceConversationOverride).where(
        WorkspaceConversationOverride.workspace_id == workspace_id,
        WorkspaceConversationOverride.conversation_id == conversation_id,
    ))).scalar_one_or_none()


async def move_board_conversation(
    db: AsyncSession, workspace_id: int, board_id: int, conversation_id: int, to_tag_id: Optional[int]
) -> bool:
    """Move a conversation between one board's columns (None = back to
    backlog / dismiss triage). False for an unknown board/conversation or a
    column not on this board.

    Only THIS board's stage tags are touched — a conversation can sit in
    independent pipelines on different stream boards. Allocating on a
    stream-scoped board also assigns the stream tag. Protected tags are never
    stripped, so a move can never remove a conversation from a workspace.
    """
    board = await _get_board_row(db, workspace_id, board_id)
    if board is None or await db.get(Conversation, conversation_id) is None:
        return False
    stage_ids = list((await db.execute(_board_stage_tag_ids(board_id))).scalars().all())
    if to_tag_id is not None and to_tag_id not in stage_ids:
        return False
    strip_ids = [tid for tid in stage_ids if tid != to_tag_id]
    if strip_ids:
        await db.execute(delete(ConversationTag).where(
            ConversationTag.conversation_id == conversation_id,
            ConversationTag.tag_id.in_(strip_ids),
            ConversationTag.tag_id.notin_(_protected_tag_ids()),
        ))
    add_ids = [tid for tid in (to_tag_id, board.scope_tag_id if to_tag_id is not None else None)
               if tid is not None]
    if add_ids:
        have = set((await db.execute(select(ConversationTag.tag_id).where(
            ConversationTag.conversation_id == conversation_id, ConversationTag.tag_id.in_(add_ids)
        ))).scalars().all())
        for tid in add_ids:
            if tid not in have:
                db.add(ConversationTag(conversation_id=conversation_id, tag_id=tid))
    override = await _override(db, workspace_id, conversation_id)
    if override is None:
        override = WorkspaceConversationOverride(workspace_id=workspace_id, conversation_id=conversation_id)
        db.add(override)
    override.needs_triage_since = None
    override.triaged_at = datetime.utcnow()
    await db.commit()
    return True


async def mark_workspace_triage(db: AsyncSession, conversation_id: int, msg_date: datetime) -> None:
    """Surface a board-less workspace member in the Unallocated column after
    incoming activity. Called from live message events and post-sync (both,
    or wake-ups discovered while the app was closed would be missed)."""
    workspaces = list((await db.execute(
        select(Workspace).where(Workspace.view_mode == "kanban", Workspace.board_watermark.isnot(None))
    )).scalars().all())
    if not workspaces:
        return
    tag_ids = set((await db.execute(
        select(ConversationTag.tag_id).where(ConversationTag.conversation_id == conversation_id)
    )).scalars().all())
    if not tag_ids:
        return
    for ws in workspaces:
        if ws.board_watermark and msg_date <= ws.board_watermark:
            continue
        include = set((await db.execute(select(WorkspaceTag.tag_id).where(
            WorkspaceTag.workspace_id == ws.id, WorkspaceTag.is_exclusion == False,  # noqa: E712
        ))).scalars().all())
        if not (include & tag_ids):
            continue
        stages = set((await db.execute(_workspace_stage_tag_ids(ws.id))).scalars().all())
        if stages & tag_ids:
            continue
        override = await _override(db, ws.id, conversation_id)
        if override is None:
            db.add(WorkspaceConversationOverride(
                workspace_id=ws.id, conversation_id=conversation_id, needs_triage_since=msg_date,
            ))
        elif override.needs_triage_since is None:
            # Dismissed episodes resurface only on activity newer than the dismissal
            if override.triaged_at is None or msg_date > override.triaged_at:
                override.needs_triage_since = msg_date
    await db.commit()
