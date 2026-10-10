"""AI assistant conversations (Intelligence page chat history)."""

from datetime import datetime
from typing import Optional

from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import AIConversation, AIConversationMessage
from backend.db.results import affected_rows

__all__ = [
    "get_ai_conversation_messages", "add_ai_conversation_message", "list_ai_conversations",
    "delete_ai_conversation", "set_ai_title_if_untitled",
]

# Tool traces are stored as role "tool": LLM context only, never shown.
VISIBLE = AIConversationMessage.role != "tool"


async def get_ai_conversation_messages(
    db: AsyncSession, ai_conversation_id: int, limit: Optional[int] = None
) -> list[AIConversationMessage]:
    """Messages of an AI conversation, oldest-first. With ``limit``, only the
    newest ``limit`` messages (still oldest-first)."""
    q = select(AIConversationMessage).where(AIConversationMessage.ai_conversation_id == ai_conversation_id)
    if limit is not None:
        newest = (
            q.with_only_columns(AIConversationMessage.id)
            .order_by(desc(AIConversationMessage.created_at), desc(AIConversationMessage.id))
            .limit(limit)
        )
        q = select(AIConversationMessage).where(AIConversationMessage.id.in_(newest.scalar_subquery()))
    result = await db.execute(q.order_by(AIConversationMessage.created_at, AIConversationMessage.id))
    return list(result.scalars().all())


async def add_ai_conversation_message(
    db: AsyncSession, ai_conversation_id: int, role: str, content: str
) -> AIConversationMessage:
    msg = AIConversationMessage(ai_conversation_id=ai_conversation_id, role=role, content=content)
    db.add(msg)
    conv = (await db.execute(
        select(AIConversation).where(AIConversation.id == ai_conversation_id)
    )).scalar_one_or_none()
    if conv:
        conv.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(msg)
    return msg


async def list_ai_conversations(db: AsyncSession) -> list[tuple[AIConversation, int]]:
    """``(conversation, visible message count)``, most recently updated first."""
    counts = (
        select(AIConversationMessage.ai_conversation_id, func.count(AIConversationMessage.id).label("cnt"))
        .where(VISIBLE)
        .group_by(AIConversationMessage.ai_conversation_id)
        .subquery()
    )
    rows = await db.execute(
        select(AIConversation, counts.c.cnt)
        .outerjoin(counts, AIConversation.id == counts.c.ai_conversation_id)
        .order_by(desc(AIConversation.updated_at))
    )
    return [(conv, cnt or 0) for conv, cnt in rows.all()]


async def delete_ai_conversation(db: AsyncSession, ai_conversation_id: int) -> None:
    """Delete a conversation and its messages (messages first: SQLite may not
    enforce the FK cascade)."""
    await db.execute(delete(AIConversationMessage).where(AIConversationMessage.ai_conversation_id == ai_conversation_id))
    await db.execute(delete(AIConversation).where(AIConversation.id == ai_conversation_id))
    await db.commit()


async def set_ai_title_if_untitled(db: AsyncSession, ai_conversation_id: int, title: str) -> tuple[bool, Optional[str]]:
    """Compare-and-set: write ``title`` only while the conversation is still
    untitled, so it never overwrites a manual rename. Returns (applied, the
    title it now has)."""
    res = await db.execute(
        update(AIConversation)
        .where(AIConversation.id == ai_conversation_id, AIConversation.title.is_(None))
        .values(title=title, updated_at=datetime.utcnow())
    )
    await db.commit()
    if affected_rows(res):
        return True, title
    current = (await db.execute(select(AIConversation.title).where(AIConversation.id == ai_conversation_id))).scalar()
    return False, current
