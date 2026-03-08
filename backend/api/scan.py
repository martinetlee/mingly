from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.crud import get_chat_by_telegram_id
from backend.db.database import get_db
from backend.db.models import Message
from backend.llm.base import LLMError
from backend.ai.importance import score_message_importance

router = APIRouter()


@router.post("/chat/{telegram_id}")
async def scan_chat_importance(
    telegram_id: int,
    limit: int = 50,
    instance_name: str = "",
    db: AsyncSession = Depends(get_db),
):
    """
    Score importance of unscored messages in a chat.
    Runs sequentially to avoid overwhelming the local LLM.

    instance_name: which registered LLM instance to use (empty = any available).
    """
    chat = await get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    result = await db.execute(
        select(Message)
        .where(
            Message.chat_id == telegram_id,
            Message.importance_score.is_(None),
            Message.text.is_not(None),
        )
        .order_by(Message.date.desc())
        .limit(limit)
    )
    messages = list(result.scalars().all())

    scored = 0
    errors = 0
    for msg in messages:
        try:
            score, reason = await score_message_importance(
                text=msg.text,
                sender_name=msg.sender_name,
                chat_name=chat.name,
                instance_name=instance_name or None,
            )
            msg.importance_score = score
            msg.importance_reason = reason
            scored += 1
        except LLMError:
            errors += 1
            continue

    await db.commit()
    return {"scored": scored, "errors": errors, "total": len(messages)}
