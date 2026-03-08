"""
Conversations API — persistent chat history for the Intelligence page.
"""
import json
from datetime import datetime
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import Conversation, ConversationMessage
from backend.llm.config import load_registry_config

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ConversationSummaryOut(BaseModel):
    id: int
    title: Optional[str]
    created_at: str
    updated_at: str
    message_count: int


class ConversationMessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: str


class ConversationDetailOut(BaseModel):
    id: int
    title: Optional[str]
    created_at: str
    updated_at: str
    messages: List[ConversationMessageOut]


class UpdateTitleIn(BaseModel):
    title: str


class AppendMessageIn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(dt: Optional[datetime]) -> str:
    if dt is None:
        return datetime.utcnow().isoformat()
    return dt.isoformat()


def _get_llm_config():
    cfg = load_registry_config()
    if not cfg.orchestrator_name:
        return None, None
    inst = next((i for i in cfg.instances if i.name == cfg.orchestrator_name), None)
    if not inst:
        return None, None
    return inst.base_url.rstrip("/"), inst.model


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/", response_model=List[ConversationSummaryOut])
async def list_conversations(db: AsyncSession = Depends(get_db)):
    """List all conversations newest-first, with message counts."""
    msg_count_sub = (
        select(
            ConversationMessage.conversation_id,
            func.count(ConversationMessage.id).label("cnt"),
        )
        .group_by(ConversationMessage.conversation_id)
        .subquery()
    )
    rows = await db.execute(
        select(Conversation, msg_count_sub.c.cnt)
        .outerjoin(msg_count_sub, Conversation.id == msg_count_sub.c.conversation_id)
        .order_by(desc(Conversation.updated_at))
    )
    result = []
    for conv, cnt in rows.all():
        result.append(
            ConversationSummaryOut(
                id=conv.id,
                title=conv.title,
                created_at=_fmt(conv.created_at),
                updated_at=_fmt(conv.updated_at),
                message_count=cnt or 0,
            )
        )
    return result


@router.post("/", response_model=ConversationDetailOut, status_code=201)
async def create_conversation(db: AsyncSession = Depends(get_db)):
    """Create a new empty conversation."""
    now = datetime.utcnow()
    conv = Conversation(title=None, created_at=now, updated_at=now)
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return ConversationDetailOut(
        id=conv.id,
        title=conv.title,
        created_at=_fmt(conv.created_at),
        updated_at=_fmt(conv.updated_at),
        messages=[],
    )


@router.get("/{conv_id}", response_model=ConversationDetailOut)
async def get_conversation(conv_id: int, db: AsyncSession = Depends(get_db)):
    """Get a conversation with all its messages."""
    conv = await db.get(Conversation, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msgs_result = await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conv_id)
        .order_by(ConversationMessage.created_at)
    )
    msgs = msgs_result.scalars().all()
    return ConversationDetailOut(
        id=conv.id,
        title=conv.title,
        created_at=_fmt(conv.created_at),
        updated_at=_fmt(conv.updated_at),
        messages=[
            ConversationMessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                created_at=_fmt(m.created_at),
            )
            for m in msgs
        ],
    )


@router.put("/{conv_id}", response_model=ConversationDetailOut)
async def update_conversation_title(
    conv_id: int, body: UpdateTitleIn, db: AsyncSession = Depends(get_db)
):
    """Update the title of a conversation."""
    conv = await db.get(Conversation, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv.title = body.title
    conv.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(conv)
    msgs_result = await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conv_id)
        .order_by(ConversationMessage.created_at)
    )
    msgs = msgs_result.scalars().all()
    return ConversationDetailOut(
        id=conv.id,
        title=conv.title,
        created_at=_fmt(conv.created_at),
        updated_at=_fmt(conv.updated_at),
        messages=[
            ConversationMessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                created_at=_fmt(m.created_at),
            )
            for m in msgs
        ],
    )


@router.delete("/{conv_id}", status_code=204)
async def delete_conversation(conv_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a conversation and all its messages."""
    conv = await db.get(Conversation, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Delete messages first (SQLite may not enforce FK cascade without pragma)
    msgs_result = await db.execute(
        select(ConversationMessage).where(ConversationMessage.conversation_id == conv_id)
    )
    for msg in msgs_result.scalars().all():
        await db.delete(msg)
    await db.delete(conv)
    await db.commit()


@router.post("/{conv_id}/messages", response_model=ConversationMessageOut, status_code=201)
async def append_message(
    conv_id: int, body: AppendMessageIn, db: AsyncSession = Depends(get_db)
):
    """Append a message to a conversation."""
    conv = await db.get(Conversation, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if body.role not in ("user", "assistant"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'assistant'")
    now = datetime.utcnow()
    msg = ConversationMessage(
        conversation_id=conv_id,
        role=body.role,
        content=body.content,
        created_at=now,
    )
    db.add(msg)
    conv.updated_at = now
    await db.commit()
    await db.refresh(msg)
    return ConversationMessageOut(
        id=msg.id,
        role=msg.role,
        content=msg.content,
        created_at=_fmt(msg.created_at),
    )


@router.post("/{conv_id}/suggest-title")
async def suggest_title(conv_id: int, db: AsyncSession = Depends(get_db)):
    """
    Use the LLM to generate a short title for the conversation based on its
    first few messages, then save it and return {"title": "..."}.
    """
    conv = await db.get(Conversation, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs_result = await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conv_id)
        .order_by(ConversationMessage.created_at)
        .limit(6)
    )
    msgs = msgs_result.scalars().all()

    if not msgs:
        return {"title": None}

    base_url, model = _get_llm_config()
    if not base_url:
        return {"title": None}

    # Build a minimal prompt for title generation
    convo_text = "\n".join(
        f"{m.role.upper()}: {m.content[:300]}" for m in msgs
    )
    prompt_messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful assistant. Given a conversation excerpt, "
                "generate a very short title (4-7 words max) that summarises the topic. "
                "Respond with ONLY the title text — no quotes, no punctuation at the end."
            ),
        },
        {
            "role": "user",
            "content": f"Generate a title for this conversation:\n\n{convo_text}",
        },
    ]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": prompt_messages,
                    "max_tokens": 32,
                    "temperature": 0.5,
                    "stream": False,
                },
            )
        if resp.status_code != 200:
            return {"title": None}
        data = resp.json()
        raw_title = data["choices"][0]["message"].get("content", "").strip()
        # Trim to at most 60 chars
        title = raw_title[:60] if raw_title else None
    except Exception:
        title = None

    if title:
        conv.title = title
        conv.updated_at = datetime.utcnow()
        await db.commit()

    return {"title": title}
