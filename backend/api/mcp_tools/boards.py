"""Workspaces and their kanban boards: tag-scoped contexts whose columns are
pipeline stages (each column is a stage tag on the conversation)."""

from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.mcp_tools.common import (
    int_arg,
    mcp_conversation,
    require_visible_conversation,
    ToolError,
)
from backend.db import crud
from backend.ws.manager import ws_manager


def _stage_key(name: str) -> str:
    """Matches a stage name the agent gives against this board's columns."""
    return " ".join(name.split()).casefold()


def _card(c) -> dict:
    return mcp_conversation(c, with_last_message=True)


async def tool_list_workspaces(db: AsyncSession, args: dict, ctx: dict) -> dict:
    rows = await crud.get_workspaces_with_counts(db)
    return {
        "workspaces": [
            {"workspace_id": r["workspace"].id, "name": r["workspace"].name, "emoji": r["workspace"].emoji,
             "view_mode": r["workspace"].view_mode, "match_mode": r["workspace"].match_mode,
             "tag_ids": r["tag_ids"], "member_count": r["member_count"],
             "unallocated_count": r["unallocated_count"]}
            for r in rows
        ],
        "note": (
            "A workspace groups the conversations carrying its tags (match_mode any/all). Its boards are "
            "pipelines: get_workspace_board shows each stage (column) and the conversations in it."
        ),
    }


async def _board(db: AsyncSession, workspace_id: int, board_id: Optional[int]) -> dict:
    board = await crud.get_board(db, workspace_id, board_id)
    if board is None:
        raise ToolError(f"Workspace {workspace_id} not found")
    if board_id is not None and board["board_id"] != board_id:
        raise ToolError(f"Board {board_id} is not on workspace {workspace_id}")
    return board


async def tool_get_workspace_board(db: AsyncSession, args: dict, ctx: dict) -> dict:
    workspace_id = int_arg(args, "workspace_id")
    board_id = int_arg(args, "board_id") if args.get("board_id") is not None else None
    board = await _board(db, workspace_id, board_id)
    ws = board["workspace"]
    return {
        "workspace": {"workspace_id": ws.id, "name": ws.name, "view_mode": ws.view_mode},
        "boards": [
            {"board_id": b["id"], "name": b["name"], "scope_tag_name": b["scope_tag_name"],
             "unallocated_count": b["unallocated_count"]}
            for b in board["boards"]
        ],
        "board_id": board["board_id"],
        "stages": [
            {"stage": col["name"], "tag_id": col["tag_id"], "position": col["position"],
             "conversations": [_card(c) for c in col["conversations"]]}
            for col in board["columns"]
        ],
        "unallocated": [_card(c) for c in board["unallocated"]],
        "backlog": {section: [_card(c) for c in convs] for section, convs in board["backlog"].items()},
        "note": (
            "stages are this board's columns in order. unallocated: members with new incoming activity "
            "waiting to be placed; backlog: members on no stage, by recency (active / cooling / dormant). "
            "move_conversation_stage places a conversation (write tools)."
        ),
    }


async def tool_move_conversation_stage(db: AsyncSession, args: dict, ctx: dict) -> dict:
    workspace_id, board_id = int_arg(args, "workspace_id"), int_arg(args, "board_id")
    conv = await require_visible_conversation(db, args.get("conversation_id"))
    board = await _board(db, workspace_id, board_id)
    stage = args.get("stage")
    to_tag_id = None
    if stage is not None:
        wanted = _stage_key(str(stage))
        match = [c for c in board["columns"] if _stage_key(c["name"]) == wanted]
        if not match:
            names = ", ".join(c["name"] for c in board["columns"]) or "none"
            raise ToolError(f"No stage named {stage!r} on this board (stages: {names})")
        to_tag_id = match[0]["tag_id"]
    try:
        moved = await crud.move_board_conversation(db, workspace_id, board_id, conv.id, to_tag_id)
    except IntegrityError:
        await db.rollback()
        raise ToolError("The board changed meanwhile; read it again with get_workspace_board and retry")
    if not moved:
        raise ToolError("Could not move the conversation on this board")
    await ws_manager.broadcast({"type": "conversations_synced", "conversation_ids": [conv.id]})
    return {
        "conversation_id": conv.id,
        "workspace_id": workspace_id,
        "board_id": board_id,
        "stage": match[0]["name"] if stage is not None else None,
    }
