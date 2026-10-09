"""AI assistant conversations (Intelligence page chat history)."""

from datetime import datetime
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import AIConversation, AIConversationMessage

__all__ = ["get_ai_conversation_messages", "add_ai_conversation_message"]


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
