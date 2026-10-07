import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db import crud

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tag_dict(t, chat_count: Optional[int] = None) -> dict:
    d = {"id": t.id, "name": t.name, "color": t.color, "category": t.category, "is_default": t.is_default}
    if chat_count is not None:
        d["chat_count"] = chat_count
    return d


# ---------------------------------------------------------------------------
# Global tag management — /api/tags
# ---------------------------------------------------------------------------

class CreateTagRequest(BaseModel):
    name: str
    color: Optional[str] = "slate"
    category: Optional[str] = None


class UpdateTagRequest(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    category: Optional[str] = None


def _locked(e: "crud.StageTagLocked") -> HTTPException:
    return HTTPException(status_code=409, detail={"code": e.code, "message": str(e)})


@router.get("/tags")
async def list_tags(include_stage: bool = False, db: AsyncSession = Depends(get_db)):
    """User tags. Board-column stage tags are internal and only listed with
    include_stage=true (e.g. column-name autocomplete)."""
    rows = await crud.get_all_tags_with_counts(db, include_stage=include_stage)
    return [_tag_dict(t, chat_count=count) for t, count in rows]


@router.post("/tags")
async def create_tag(req: CreateTagRequest, db: AsyncSession = Depends(get_db)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Tag name cannot be empty")
    color = req.color or "slate"
    category = req.category.strip() if req.category else None
    try:
        tag = await crud.create_tag(db, name=name, color=color, category=category)
    except crud.NameConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _tag_dict(tag)


@router.put("/tags/{tag_id}")
async def update_tag(tag_id: int, req: UpdateTagRequest, db: AsyncSession = Depends(get_db)):
    try:
        tag = await crud.update_tag(db, tag_id, name=req.name, color=req.color, category=req.category)
    except crud.StageTagLocked as e:
        raise _locked(e)
    except crud.NameConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    return _tag_dict(tag)


@router.delete("/tags/{tag_id}")
async def delete_tag(tag_id: int, db: AsyncSession = Depends(get_db)):
    try:
        deleted = await crud.delete_tag(db, tag_id)
    except crud.StageTagLocked as e:
        raise _locked(e)
    if not deleted:
        raise HTTPException(status_code=404, detail="Tag not found")
    return {"success": True}


# ---------------------------------------------------------------------------
# Per-chat tag management — /api/chats/{telegram_id}/tags
# ---------------------------------------------------------------------------

class AddTagRequest(BaseModel):
    tag_id: int


@router.get("/chats/{telegram_id}/tags")
async def get_chat_tags(telegram_id: int, db: AsyncSession = Depends(get_db)):
    # Board placement (stage tags) is shown on boards, not as chat tags
    tags = await crud.get_tags_for_chat(db, telegram_id, include_stage=False)
    return [_tag_dict(t) for t in tags]


@router.post("/chats/{telegram_id}/tags")
async def add_tag(telegram_id: int, req: AddTagRequest, db: AsyncSession = Depends(get_db)):
    try:
        ct = await crud.add_tag_to_chat(db, telegram_id, req.tag_id)
    except crud.StageTagLocked as e:
        raise _locked(e)
    if ct is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    return {"success": True}


@router.delete("/chats/{telegram_id}/tags/{tag_id}")
async def remove_tag(telegram_id: int, tag_id: int, db: AsyncSession = Depends(get_db)):
    try:
        removed = await crud.remove_tag_from_chat(db, telegram_id, tag_id)
    except crud.StageTagLocked as e:
        raise _locked(e)
    if not removed:
        raise HTTPException(status_code=404, detail="Tag not assigned to this chat")
    return {"success": True}


# ---------------------------------------------------------------------------
# LLM tag suggestion — /api/chats/{telegram_id}/tags/suggest
# ---------------------------------------------------------------------------

@router.post("/chats/{telegram_id}/tags/suggest")
async def suggest_tags(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """Ask the LLM to suggest tags for a chat based on its name and recent messages."""
    # Gather context
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Stage tags are board internals: never offered, never suggested
    all_tags = [t for t in await crud.get_all_tags(db) if t.category != crud.STAGE_CATEGORY]
    chat_tags = await crud.get_tags_for_chat(db, telegram_id, include_stage=False)
    chat_tag_names = {t.name for t in chat_tags}
    chat_tag_keys = {t.name.strip().lower() for t in chat_tags}
    all_tag_names = [t.name for t in all_tags]

    # Fetch last 20 messages for context
    rows = await crud.get_messages(db, telegram_id, limit=20, offset=0)
    messages_text = "\n".join(
        f"{m.sender_name or 'Me'}: {(m.text or '').strip()[:120]}"
        for m, *_ in rows if (m.text or "").strip()
    ) or "(no messages yet)"

    system_prompt = (
        "You are a chat organizer. Given a Telegram chat's name and recent messages, "
        "If the chat is a group, typically the chat name can infer a lot of the tags."
        "suggest 1–3 relevant tags from the provided list or propose new short ones. "
        "Respond ONLY with a JSON array: "
        '[{"name": "...", "color": "...", "reason": "..."}]. '
        "Use color values from: indigo, emerald, rose, amber, violet, sky, slate. "
        "Do not include tags already assigned to the chat."
    )
    user_prompt = (
        f"Chat name: {chat.name}\n"
        f"Already assigned tags: {', '.join(chat_tag_names) or 'none'}\n"
        f"Available tags: {', '.join(all_tag_names) or 'none'}\n"
        f"Recent messages:\n{messages_text}"
    )

    await db.commit()  # end the read transaction before the (slow) LLM call

    # Try LLM — fall back gracefully
    try:
        from backend.llm.base import ChatMessage
        from backend.llm.resolve import LLMUnavailable, get_provider

        try:
            provider = get_provider()
        except LLMUnavailable:
            return []
        response = await provider.complete(
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_prompt),
            ],
            max_tokens=256,
            temperature=0.4,
        )
        raw = response.content.strip()

        # Extract JSON array from response (handle markdown code blocks)
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return []

        suggestions = json.loads(match.group())
        valid_colors = {"indigo", "emerald", "rose", "amber", "violet", "sky", "slate"}
        result = []
        for s in suggestions:
            if not isinstance(s, dict) or "name" not in s:
                continue
            name = str(s.get("name", "")).strip()
            color = str(s.get("color", "slate")).strip()
            reason = str(s.get("reason", "")).strip()
            if not name or name.lower() in chat_tag_keys:
                continue
            if color not in valid_colors:
                color = "slate"
            result.append({"name": name, "color": color, "reason": reason})
        return result[:3]

    except Exception:
        return []
