from contextlib import contextmanager
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.database import get_db
from backend.db.models import Workspace
from backend.serializers import conversation_out

router = APIRouter()


@contextmanager
def _conflicts_as_409(detail: str = "Conflicts with existing data"):
    """A concurrent write that trips a unique constraint is a 409, not a 500.
    (The request session is rolled back when get_db closes it.)"""
    try:
        yield
    except IntegrityError:
        raise HTTPException(status_code=409, detail=detail)


def _ws_dict(ws, tag_ids: list[int], member_count: int, unallocated_count: int) -> dict:
    return {
        "id": ws.id,
        "name": ws.name,
        "emoji": ws.emoji,
        "color": ws.color,
        "match_mode": ws.match_mode,
        "view_mode": ws.view_mode,
        "position": ws.position,
        "tag_ids": tag_ids,
        "member_count": member_count,
        "unallocated_count": unallocated_count,
    }



async def _board_payload(db: AsyncSession, workspace_id: int, board_id: Optional[int]) -> dict:
    board = await crud.get_board(db, workspace_id, board_id)
    if board is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    ws = board["workspace"]
    return {
        "workspace": {"id": ws.id, "name": ws.name, "emoji": ws.emoji, "color": ws.color,
                      "view_mode": ws.view_mode},
        "boards": board["boards"],
        "board_id": board["board_id"],
        "columns": [
            {**col, "conversations": [conversation_out(c) for c in col["conversations"]]}
            for col in board["columns"]
        ],
        "unallocated": [conversation_out(c) for c in board["unallocated"]],
        "backlog": {
            section: [conversation_out(c) for c in convs]
            for section, convs in board["backlog"].items()
        },
        "classified_conversation_ids": board["classified_conversation_ids"],
    }


async def _require_workspace(db: AsyncSession, workspace_id: int) -> None:
    if await db.get(Workspace, workspace_id) is None:
        raise HTTPException(status_code=404, detail="Workspace not found")


class WorkspaceIn(BaseModel):
    name: str
    emoji: Optional[str] = None
    color: str = "indigo"
    tag_ids: List[int] = []
    view_mode: str = "list"


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = None
    # Omitted = unchanged; explicit null or "" = clear the emoji
    emoji: Optional[str] = None
    color: Optional[str] = None
    tag_ids: Optional[List[int]] = None
    view_mode: Optional[str] = None


class ColumnIn(BaseModel):
    name: str
    color: Optional[str] = None


class ColumnUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    position: Optional[int] = None


class ColumnOrder(BaseModel):
    tag_ids: List[int]  # every column of the board, left to right


class BoardIn(BaseModel):
    name: str
    scope_tag_name: Optional[str] = None  # stream tag; None = unscoped board


class BoardUpdate(BaseModel):
    name: Optional[str] = None
    scope_tag_name: Optional[str] = None
    clear_scope: bool = False
    position: Optional[int] = None


class MoveRequest(BaseModel):
    board_id: int
    conversation_id: int
    to_tag_id: Optional[int] = None  # None = back to backlog / dismiss triage


@router.get("")
async def list_workspaces(db: AsyncSession = Depends(get_db)):
    rows = await crud.get_workspaces_with_counts(db)
    return [_ws_dict(r["workspace"], r["tag_ids"], r["member_count"], r["unallocated_count"]) for r in rows]


@router.post("")
async def create_workspace(req: WorkspaceIn, db: AsyncSession = Depends(get_db)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Workspace name cannot be empty")
    if req.view_mode not in ("list", "kanban"):
        raise HTTPException(status_code=400, detail="view_mode must be 'list' or 'kanban'")
    try:
        with _conflicts_as_409():
            ws = await crud.create_workspace(db, name=name, emoji=(req.emoji or "").strip() or None,
                                             color=req.color, tag_ids=req.tag_ids, view_mode=req.view_mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    boards = await crud.get_workspace_boards(db, ws.id)
    result = _ws_dict(ws, list(dict.fromkeys(req.tag_ids)), 0, 0)
    result["main_board_id"] = boards[0][0].id if boards else None
    return result


@router.put("/{workspace_id}")
async def update_workspace(workspace_id: int, req: WorkspaceUpdate, db: AsyncSession = Depends(get_db)):
    if req.view_mode is not None and req.view_mode not in ("list", "kanban"):
        raise HTTPException(status_code=400, detail="view_mode must be 'list' or 'kanban'")
    if req.name is not None and not req.name.strip():
        raise HTTPException(status_code=400, detail="Workspace name cannot be empty")
    emoji = (req.emoji or "").strip() or None
    clear_emoji = "emoji" in req.model_fields_set and emoji is None
    try:
        with _conflicts_as_409():
            ws = await crud.update_workspace(
                db, workspace_id,
                name=req.name.strip() if req.name is not None else None,
                emoji=emoji, clear_emoji=clear_emoji,
                color=req.color, tag_ids=req.tag_ids, view_mode=req.view_mode,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"status": "ok"}


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: int, db: AsyncSession = Depends(get_db)):
    if not await crud.delete_workspace(db, workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")


@router.get("/{workspace_id}/board")
async def get_board(workspace_id: int, board_id: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    return await _board_payload(db, workspace_id, board_id)


@router.post("/{workspace_id}/boards")
async def add_board(workspace_id: int, req: BoardIn, db: AsyncSession = Depends(get_db)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Board name cannot be empty")
    await _require_workspace(db, workspace_id)
    with _conflicts_as_409("A board or stream tag with this name already exists"):
        board = await crud.create_workspace_board(db, workspace_id, name, req.scope_tag_name)
    if board is None:
        raise HTTPException(status_code=409, detail="A board with this name already exists in this workspace")
    return {"id": board.id, "name": board.name, "scope_tag_id": board.scope_tag_id, "position": board.position}


@router.put("/{workspace_id}/boards/{board_id}")
async def update_board(workspace_id: int, board_id: int, req: BoardUpdate, db: AsyncSession = Depends(get_db)):
    if req.name is not None and not req.name.strip():
        raise HTTPException(status_code=400, detail="Board name cannot be empty")
    with _conflicts_as_409("A board or stream tag with this name already exists"):
        outcome = await crud.update_workspace_board(
            db, workspace_id, board_id,
            name=req.name.strip() if req.name is not None else None,
            scope_tag_name=req.scope_tag_name, clear_scope=req.clear_scope, position=req.position,
        )
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Board not found")
    if outcome == "conflict":
        raise HTTPException(status_code=409, detail="A board with this name already exists in this workspace")
    return {"status": "ok"}


@router.delete("/{workspace_id}/boards/{board_id}")
async def delete_board(workspace_id: int, board_id: int, db: AsyncSession = Depends(get_db)):
    """Deletes the board with its columns and their stage tags."""
    if not await crud.delete_workspace_board(db, workspace_id, board_id):
        raise HTTPException(status_code=404, detail="Board not found")
    return {"status": "ok"}


@router.post("/{workspace_id}/boards/{board_id}/columns")
async def add_column(workspace_id: int, board_id: int, req: ColumnIn, db: AsyncSession = Depends(get_db)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Column name cannot be empty")
    with _conflicts_as_409(f'This board already has a column named "{name}"'):
        outcome, col = await crud.add_board_column(db, workspace_id, board_id, name, req.color)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Board not found")
    if outcome == "conflict":
        raise HTTPException(status_code=409, detail=f'This board already has a column named "{name}"')
    return col


# Registered before /columns/{tag_id} so "order" is never parsed as a tag id
@router.put("/{workspace_id}/boards/{board_id}/columns/order")
async def reorder_columns(
    workspace_id: int, board_id: int, req: ColumnOrder, db: AsyncSession = Depends(get_db)
):
    """Atomically set the left-to-right column order; returns the updated board."""
    outcome = await crud.reorder_board_columns(db, workspace_id, board_id, req.tag_ids)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Board not found")
    if outcome == "mismatch":
        raise HTTPException(status_code=400, detail="tag_ids must list every column of this board exactly once")
    return await _board_payload(db, workspace_id, board_id)


@router.put("/{workspace_id}/boards/{board_id}/columns/{tag_id}")
async def update_column(
    workspace_id: int, board_id: int, tag_id: int, req: ColumnUpdate, db: AsyncSession = Depends(get_db)
):
    if req.name is not None and not req.name.strip():
        raise HTTPException(status_code=400, detail="Column name cannot be empty")
    with _conflicts_as_409():
        outcome = await crud.update_board_column(
            db, workspace_id, board_id, tag_id,
            name=req.name.strip() if req.name is not None else None,
            color=req.color, position=req.position,
        )
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Column not found")
    if outcome == "conflict":
        raise HTTPException(status_code=409, detail="This board already has a column with that name")
    return {"status": "ok"}


@router.delete("/{workspace_id}/boards/{board_id}/columns/{tag_id}")
async def delete_column(
    workspace_id: int, board_id: int, tag_id: int, db: AsyncSession = Depends(get_db)
):
    """The column's stage tag is deleted with it (cards return to backlog)."""
    if not await crud.delete_board_column(db, workspace_id, board_id, tag_id):
        raise HTTPException(status_code=404, detail="Column not found")
    return {"status": "ok"}


@router.post("/{workspace_id}/move")
async def move_conversation(workspace_id: int, req: MoveRequest, db: AsyncSession = Depends(get_db)):
    """Move a card; returns the updated board (same shape as GET /board) so the
    client can replace its optimistic state with the committed one."""
    with _conflicts_as_409():
        moved = await crud.move_board_conversation(db, workspace_id, req.board_id, req.conversation_id, req.to_tag_id)
    if not moved:
        raise HTTPException(status_code=400, detail="Unknown board or conversation, or the column is not on this board")
    return await _board_payload(db, workspace_id, req.board_id)
