"""
AI conversations API (/api/ai/conversations) — persistent chat history for
the Intelligence page's assistant.
"""
from datetime import datetime
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, desc, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import AIConversation, AIConversationMessage
from backend.llm.base import ChatMessage, LLMError
from backend.llm.resolve import LLMUnavailable, complete_text
from backend.serialization import iso_utc
from backend.db.results import affected_rows

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
    return iso_utc(dt or datetime.utcnow())


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/", response_model=List[ConversationSummaryOut])
async def list_conversations(db: AsyncSession = Depends(get_db)):
    """List all conversations newest-first, with message counts."""
    msg_count_sub = (
        select(
            AIConversationMessage.ai_conversation_id,
            func.count(AIConversationMessage.id).label("cnt"),
        )
        .where(AIConversationMessage.role != "tool")  # internal tool traces aren't visible messages
        .group_by(AIConversationMessage.ai_conversation_id)
        .subquery()
    )
    rows = await db.execute(
        select(AIConversation, msg_count_sub.c.cnt)
        .outerjoin(msg_count_sub, AIConversation.id == msg_count_sub.c.ai_conversation_id)
        .order_by(desc(AIConversation.updated_at))
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
    conv = AIConversation(title=None, created_at=now, updated_at=now)
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


@router.get("/{ai_conversation_id}", response_model=ConversationDetailOut)
async def get_conversation(ai_conversation_id: int, db: AsyncSession = Depends(get_db)):
    """Get a conversation with all its messages."""
    conv = await db.get(AIConversation, ai_conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msgs_result = await db.execute(
        select(AIConversationMessage)
        .where(AIConversationMessage.ai_conversation_id == ai_conversation_id)
        .order_by(AIConversationMessage.created_at)
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
            if m.role != "tool"  # internal tool traces are LLM context only
        ],
    )


@router.put("/{ai_conversation_id}", response_model=ConversationDetailOut)
async def update_conversation_title(
    ai_conversation_id: int, body: UpdateTitleIn, db: AsyncSession = Depends(get_db)
):
    """Update the title of a conversation."""
    conv = await db.get(AIConversation, ai_conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv.title = body.title
    conv.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(conv)
    msgs_result = await db.execute(
        select(AIConversationMessage)
        .where(AIConversationMessage.ai_conversation_id == ai_conversation_id)
        .order_by(AIConversationMessage.created_at)
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
            if m.role != "tool"  # internal tool traces are LLM context only
        ],
    )


@router.delete("/{ai_conversation_id}", status_code=204)
async def delete_conversation(ai_conversation_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a conversation and all its messages."""
    conv = await db.get(AIConversation, ai_conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Delete messages first (SQLite may not enforce FK cascade without pragma)
    msgs_result = await db.execute(
        select(AIConversationMessage).where(AIConversationMessage.ai_conversation_id == ai_conversation_id)
    )
    for msg in msgs_result.scalars().all():
        await db.delete(msg)
    await db.delete(conv)
    await db.commit()


@router.post("/{ai_conversation_id}/messages", response_model=ConversationMessageOut, status_code=201)
async def append_message(
    ai_conversation_id: int, body: AppendMessageIn, db: AsyncSession = Depends(get_db)
):
    """Append a message to a conversation."""
    conv = await db.get(AIConversation, ai_conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if body.role not in ("user", "assistant"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'assistant'")
    now = datetime.utcnow()
    msg = AIConversationMessage(
        ai_conversation_id=ai_conversation_id,
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


@router.post("/{ai_conversation_id}/suggest-title")
async def suggest_title(ai_conversation_id: int, db: AsyncSession = Depends(get_db)):
    """
    Use the LLM to generate a short title for the conversation based on its
    first few messages and return {"title": ..., "applied": bool}.

    Compare-and-set: the title is written only while the conversation is still
    untitled (WHERE title IS NULL), so it never overwrites a manual rename;
    in that case the current title is returned with applied=false.
    """
    conv = await db.get(AIConversation, ai_conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.title:
        return {"title": conv.title, "applied": False}

    msgs_result = await db.execute(
        select(AIConversationMessage)
        .where(AIConversationMessage.ai_conversation_id == ai_conversation_id, AIConversationMessage.role != "tool")
        .order_by(AIConversationMessage.created_at)
        .limit(6)
    )
    msgs = msgs_result.scalars().all()
    if not msgs:
        return {"title": None, "applied": False}
    convo_text = "\n".join(f"{m.role.upper()}: {m.content[:300]}" for m in msgs)
    await db.commit()  # end the read transaction before the LLM call

    try:
        raw_title = await complete_text(
            [
                ChatMessage(role="system", content=(
                    "You are a helpful assistant. Given a conversation excerpt, "
                    "generate a very short title (4-7 words max) that summarises the topic. "
                    "Respond with ONLY the title text — no quotes, no punctuation at the end."
                )),
                ChatMessage(role="user", content=f"Generate a title for this conversation:\n\n{convo_text}"),
            ],
            max_tokens=32,
            temperature=0.5,
        )
    except LLMUnavailable:
        raise HTTPException(status_code=503, detail="No LLM configured. Set up an AI model in Settings.")
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {str(e)[:200]}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Could not reach LLM: {e}")

    title = raw_title.strip().strip('"\'').strip()[:60] or None
    if not title:
        return {"title": None, "applied": False}

    res = await db.execute(
        update(AIConversation)
        .where(AIConversation.id == ai_conversation_id, AIConversation.title.is_(None))
        .values(title=title, updated_at=datetime.utcnow())
    )
    await db.commit()
    if affected_rows(res):
        return {"title": title, "applied": True}
    current = (await db.execute(select(AIConversation.title).where(AIConversation.id == ai_conversation_id))).scalar()
    return {"title": current, "applied": False}
