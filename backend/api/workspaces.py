from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db import crud

router = APIRouter()


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


def _board_chat_dict(c) -> dict:
    from backend.api.chats import _chat_dict
    return _chat_dict(c, c.last_message_text, c.last_message_sender, c.last_message_date)


class WorkspaceIn(BaseModel):
    name: str
    emoji: Optional[str] = None
    color: str = "indigo"
    tag_ids: List[int] = []
    view_mode: str = "list"


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = None
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
    chat_id: int
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
    ws = await crud.create_workspace(db, name=name, emoji=req.emoji, color=req.color,
                                     tag_ids=req.tag_ids, view_mode=req.view_mode)
    boards = await crud.get_workspace_boards(db, ws.id)
    result = _ws_dict(ws, req.tag_ids, 0, 0)
    result["main_board_id"] = boards[0][0].id if boards else None
    return result


@router.put("/{workspace_id}")
async def update_workspace(workspace_id: int, req: WorkspaceUpdate, db: AsyncSession = Depends(get_db)):
    if req.view_mode is not None and req.view_mode not in ("list", "kanban"):
        raise HTTPException(status_code=400, detail="view_mode must be 'list' or 'kanban'")
    ws = await crud.update_workspace(
        db, workspace_id,
        name=req.name.strip() if req.name is not None else None,
        emoji=req.emoji, color=req.color, tag_ids=req.tag_ids, view_mode=req.view_mode,
    )
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"status": "ok"}


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: int, db: AsyncSession = Depends(get_db)):
    if not await crud.delete_workspace(db, workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")


@router.get("/{workspace_id}/board")
async def get_board(workspace_id: int, board_id: Optional[int] = None, db: AsyncSession = Depends(get_db)):
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
            {**col, "chats": [_board_chat_dict(c) for c in col["chats"]]}
            for col in board["columns"]
        ],
        "unallocated": [_board_chat_dict(c) for c in board["unallocated"]],
        "backlog": {
            section: [_board_chat_dict(c) for c in chats]
            for section, chats in board["backlog"].items()
        },
        "classified_chat_ids": board["classified_chat_ids"],
    }


@router.post("/{workspace_id}/boards")
async def add_board(workspace_id: int, req: BoardIn, db: AsyncSession = Depends(get_db)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Board name cannot be empty")
    board = await crud.create_workspace_board(db, workspace_id, name, req.scope_tag_name)
    if board is None:
        raise HTTPException(status_code=409, detail="A board with this name already exists in this workspace")
    return {"id": board.id, "name": board.name, "scope_tag_id": board.scope_tag_id, "position": board.position}


@router.put("/{workspace_id}/boards/{board_id}")
async def update_board(workspace_id: int, board_id: int, req: BoardUpdate, db: AsyncSession = Depends(get_db)):
    ok = await crud.update_workspace_board(
        db, workspace_id, board_id,
        name=req.name.strip() if req.name is not None else None,
        scope_tag_name=req.scope_tag_name, clear_scope=req.clear_scope, position=req.position,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Board not found")
    return {"status": "ok"}


@router.delete("/{workspace_id}/boards/{board_id}")
async def delete_board(workspace_id: int, board_id: int, remove_tags: bool = True, db: AsyncSession = Depends(get_db)):
    if not await crud.delete_workspace_board(db, workspace_id, board_id, remove_tags=remove_tags):
        raise HTTPException(status_code=404, detail="Board not found")
    return {"status": "ok"}


@router.post("/{workspace_id}/boards/{board_id}/columns")
async def add_column(workspace_id: int, board_id: int, req: ColumnIn, db: AsyncSession = Depends(get_db)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Column name cannot be empty")
    col = await crud.add_board_column(db, workspace_id, board_id, name, req.color)
    if col is None:
        raise HTTPException(status_code=409, detail=f'This board already has a column named "{name}"')
    return col


@router.put("/{workspace_id}/boards/{board_id}/columns/{tag_id}")
async def update_column(
    workspace_id: int, board_id: int, tag_id: int, req: ColumnUpdate, db: AsyncSession = Depends(get_db)
):
    outcome = await crud.update_board_column(
        db, workspace_id, board_id, tag_id,
        name=req.name.strip() if req.name is not None else None,
        color=req.color, position=req.position,
    )
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Column not found")
    return {"status": "ok"}


@router.delete("/{workspace_id}/boards/{board_id}/columns/{tag_id}")
async def delete_column(
    workspace_id: int, board_id: int, tag_id: int, remove_tags: bool = True, db: AsyncSession = Depends(get_db)
):
    if not await crud.delete_board_column(db, workspace_id, board_id, tag_id, remove_tags=remove_tags):
        raise HTTPException(status_code=404, detail="Column not found")
    return {"status": "ok"}


@router.post("/{workspace_id}/move")
async def move_chat(workspace_id: int, req: MoveRequest, db: AsyncSession = Depends(get_db)):
    if not await crud.move_board_chat(db, workspace_id, req.board_id, req.chat_id, req.to_tag_id):
        raise HTTPException(status_code=400, detail="Target column does not belong to this board")
    return {"status": "ok"}
