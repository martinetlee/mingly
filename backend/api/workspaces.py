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


class MoveRequest(BaseModel):
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
    return _ws_dict(ws, req.tag_ids, 0, 0)


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
async def get_board(workspace_id: int, db: AsyncSession = Depends(get_db)):
    board = await crud.get_board(db, workspace_id)
    if board is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    ws = board["workspace"]
    return {
        "workspace": {"id": ws.id, "name": ws.name, "emoji": ws.emoji, "color": ws.color,
                      "view_mode": ws.view_mode},
        "columns": [
            {**col, "chats": [_board_chat_dict(c) for c in col["chats"]]}
            for col in board["columns"]
        ],
        "unallocated": [_board_chat_dict(c) for c in board["unallocated"]],
        "backlog": {
            section: [_board_chat_dict(c) for c in chats]
            for section, chats in board["backlog"].items()
        },
    }


@router.post("/{workspace_id}/columns")
async def add_column(workspace_id: int, req: ColumnIn, db: AsyncSession = Depends(get_db)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Column name cannot be empty")
    col = await crud.add_board_column(db, workspace_id, name, req.color)
    if col is None:
        raise HTTPException(status_code=409, detail="This tag already backs a column on this board")
    return col


@router.put("/{workspace_id}/columns/{tag_id}")
async def update_column(workspace_id: int, tag_id: int, req: ColumnUpdate, db: AsyncSession = Depends(get_db)):
    outcome = await crud.update_board_column(
        db, workspace_id, tag_id,
        name=req.name.strip() if req.name is not None else None,
        color=req.color, position=req.position,
    )
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Column not found")
    if outcome == "shared":
        raise HTTPException(
            status_code=409,
            detail="This stage tag also backs a column on another workspace's board — renaming would affect it too.",
        )
    return {"status": "ok"}


@router.delete("/{workspace_id}/columns/{tag_id}")
async def delete_column(workspace_id: int, tag_id: int, remove_tags: bool = True, db: AsyncSession = Depends(get_db)):
    if not await crud.delete_board_column(db, workspace_id, tag_id, remove_tags=remove_tags):
        raise HTTPException(status_code=404, detail="Column not found")
    return {"status": "ok"}


@router.post("/{workspace_id}/move")
async def move_chat(workspace_id: int, req: MoveRequest, db: AsyncSession = Depends(get_db)):
    if not await crud.move_board_chat(db, workspace_id, req.chat_id, req.to_tag_id):
        raise HTTPException(status_code=400, detail="Target column does not belong to this board")
    return {"status": "ok"}
