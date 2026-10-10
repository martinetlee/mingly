"""
AI conversations API (/api/ai/conversations) — persistent chat history for
the Intelligence page's assistant.
"""
from datetime import datetime
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.database import get_db
from backend.db.models import AIConversation, AIConversationMessage
from backend.llm.base import ChatMessage, LLMError
from backend.llm.resolve import LLMUnavailable, complete_text
from backend.serialization import iso_utc

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


async def _conversation_or_404(db: AsyncSession, ai_conversation_id: int) -> AIConversation:
    conv = await db.get(AIConversation, ai_conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


def _message_out(m: AIConversationMessage) -> ConversationMessageOut:
    return ConversationMessageOut(id=m.id, role=m.role, content=m.content, created_at=_fmt(m.created_at))


async def _detail(db: AsyncSession, conv: AIConversation) -> ConversationDetailOut:
    msgs = await crud.get_ai_conversation_messages(db, conv.id)
    return ConversationDetailOut(
        id=conv.id,
        title=conv.title,
        created_at=_fmt(conv.created_at),
        updated_at=_fmt(conv.updated_at),
        messages=[_message_out(m) for m in msgs if m.role != "tool"],  # tool traces are LLM context only
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/", response_model=List[ConversationSummaryOut])
async def list_conversations(db: AsyncSession = Depends(get_db)):
    """List all conversations newest-first, with message counts."""
    return [
        ConversationSummaryOut(
            id=conv.id, title=conv.title, created_at=_fmt(conv.created_at), updated_at=_fmt(conv.updated_at),
            message_count=count,
        )
        for conv, count in await crud.list_ai_conversations(db)
    ]


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
    return await _detail(db, await _conversation_or_404(db, ai_conversation_id))


@router.put("/{ai_conversation_id}", response_model=ConversationDetailOut)
async def update_conversation_title(
    ai_conversation_id: int, body: UpdateTitleIn, db: AsyncSession = Depends(get_db)
):
    """Update the title of a conversation."""
    conv = await _conversation_or_404(db, ai_conversation_id)
    conv.title = body.title
    conv.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(conv)
    return await _detail(db, conv)


@router.delete("/{ai_conversation_id}", status_code=204)
async def delete_conversation(ai_conversation_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a conversation and all its messages."""
    await _conversation_or_404(db, ai_conversation_id)
    await crud.delete_ai_conversation(db, ai_conversation_id)


@router.post("/{ai_conversation_id}/messages", response_model=ConversationMessageOut, status_code=201)
async def append_message(
    ai_conversation_id: int, body: AppendMessageIn, db: AsyncSession = Depends(get_db)
):
    """Append a message to a conversation."""
    await _conversation_or_404(db, ai_conversation_id)
    if body.role not in ("user", "assistant"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'assistant'")
    return _message_out(await crud.add_ai_conversation_message(db, ai_conversation_id, body.role, body.content))


@router.post("/{ai_conversation_id}/suggest-title")
async def suggest_title(ai_conversation_id: int, db: AsyncSession = Depends(get_db)):
    """
    Use the LLM to generate a short title for the conversation based on its
    first few messages and return {"title": ..., "applied": bool}.

    Compare-and-set: the title is written only while the conversation is still
    untitled (WHERE title IS NULL), so it never overwrites a manual rename;
    in that case the current title is returned with applied=false.
    """
    conv = await _conversation_or_404(db, ai_conversation_id)
    if conv.title:
        return {"title": conv.title, "applied": False}

    msgs = [m for m in await crud.get_ai_conversation_messages(db, ai_conversation_id) if m.role != "tool"][:6]
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
    applied, current = await crud.set_ai_title_if_untitled(db, ai_conversation_id, title)
    return {"title": current, "applied": applied}
