"""The log of group / channel admin actions taken from the app."""

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import AdminActionLog, Identity

__all__ = ["log_admin_action", "admin_action_log"]


async def log_admin_action(db: AsyncSession, conversation_id: int, action: str,
                           target_identity_id: Optional[int] = None, details: Optional[str] = None) -> None:
    db.add(AdminActionLog(
        conversation_id=conversation_id, action=action, target_identity_id=target_identity_id,
        details=details, created_at=datetime.utcnow(),
    ))
    await db.commit()


async def admin_action_log(db: AsyncSession, conversation_id: int, limit: int) -> list[tuple[AdminActionLog, Optional[int]]]:
    """``(entry, target person id)``, newest first."""
    return list((await db.execute(
        select(AdminActionLog, Identity.person_id)
        .outerjoin(Identity, Identity.id == AdminActionLog.target_identity_id)
        .where(AdminActionLog.conversation_id == conversation_id)
        .order_by(AdminActionLog.created_at.desc())
        .limit(limit)
    )).all())
