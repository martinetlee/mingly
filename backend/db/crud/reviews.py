"""MCP agent review progress per conversation."""

from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import AgentReview, Conversation, HistoryCoverage, Message

__all__ = ["newest_stored_seq", "review_up_to_date", "get_agent_reviews", "mark_conversation_reviewed"]


def newest_stored_seq():
    """Correlated scalar: the newest stored seq of Conversation (NULL if none)."""
    return (
        select(func.max(Message.seq))
        .where(Message.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )


def review_up_to_date():
    """SQL predicate over Conversation outer-joined to HistoryCoverage and
    AgentReview: history complete, reviewed through the newest stored message,
    and no older message has appeared since the review."""
    newest = newest_stored_seq()
    stored_le_through = (
        select(func.count()).select_from(Message)
        .where(Message.conversation_id == Conversation.id,
               Message.seq <= AgentReview.reviewed_through_seq)
        .correlate(Conversation, AgentReview)
        .scalar_subquery()
    )
    return and_(
        func.coalesce(HistoryCoverage.history_complete, False) == True,  # noqa: E712
        AgentReview.conversation_id.isnot(None),
        or_(newest.is_(None), AgentReview.reviewed_through_seq >= newest),
        stored_le_through <= AgentReview.reviewed_message_count,
    )


async def get_agent_reviews(db: AsyncSession, conversation_ids: Iterable[int]) -> dict[int, AgentReview]:
    ids = list(set(conversation_ids))
    out: dict[int, AgentReview] = {}
    for i in range(0, len(ids), 500):
        rows = (await db.execute(
            select(AgentReview).where(AgentReview.conversation_id.in_(ids[i:i + 500]))
        )).scalars().all()
        out.update({r.conversation_id: r for r in rows})
    return out


async def mark_conversation_reviewed(
    db: AsyncSession, conversation_id: int, through_message_id: Optional[int], agent_label: Optional[str] = None,
) -> AgentReview:
    """Record a review through the stored message ``through_message_id`` (never
    moves backwards). None is only valid for a conversation with no stored
    messages (ValueError otherwise, or when the message is not stored in this
    conversation). Commits."""
    if through_message_id is None:
        has_any = (await db.execute(
            select(Message.id).where(Message.conversation_id == conversation_id).limit(1)
        )).first()
        if has_any:
            raise ValueError(
                "through_message_id must be a stored message id (omit it only for an empty conversation)"
            )
        through_seq = 0
    else:
        through_seq = (await db.execute(
            select(Message.seq).where(Message.id == through_message_id, Message.conversation_id == conversation_id)
        )).scalar_one_or_none()
        if through_seq is None:
            raise ValueError(f"Message {through_message_id} is not stored in conversation {conversation_id}")

    review = await db.get(AgentReview, conversation_id)
    through = max(through_seq, review.reviewed_through_seq) if review else through_seq
    count = (await db.execute(
        select(func.count()).select_from(Message)
        .where(Message.conversation_id == conversation_id, Message.seq <= through)
    )).scalar() or 0
    if review is None:
        review = AgentReview(conversation_id=conversation_id)
        db.add(review)
    review.reviewed_through_seq = through
    review.reviewed_message_count = count
    review.reviewed_at = datetime.utcnow()
    review.agent_label = (agent_label or "").strip()[:100] or None
    await db.commit()
    await db.refresh(review)
    return review
