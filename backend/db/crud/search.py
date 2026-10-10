"""Local search across every channel (⌘K and the conversation finder):
case-insensitive substring matches, newest / most relevant first."""

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.crud.text import contains_ci
from backend.db.models import Conversation, Identity, Message, Organization, Person

__all__ = ["search_message_text", "search_conversations", "search_people", "search_organizations"]


async def search_message_text(db: AsyncSession, q: str, limit: int) -> list:
    """Messages whose text or email subject matches, newest first: rows of
    ``(id, conversation_id, conversation_title, sender_name, subject, text
    (first 200 chars), date)``."""
    return list((await db.execute(
        select(
            Message.id,
            Message.conversation_id,
            Conversation.title.label("conversation_title"),
            Message.sender_name,
            Message.subject,
            func.substr(Message.text, 1, 200).label("text"),
            Message.date,
        )
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(or_(contains_ci(Message.text, q), contains_ci(Message.subject, q)))
        .order_by(desc(Message.date))
        .limit(limit)
    )).all())


async def search_conversations(db: AsyncSession, q: str, limit: int) -> list[Conversation]:
    """Visible conversations by title or handle, most recently active first."""
    return list((await db.execute(
        select(Conversation)
        .where(
            Conversation.is_hidden == False,  # noqa: E712
            Conversation.is_deactivated == False,  # noqa: E712
            or_(contains_ci(Conversation.title, q), contains_ci(Conversation.username, q)),
        )
        .order_by(desc(Conversation.last_activity_at), Conversation.title)
        .limit(limit)
    )).scalars().all())


async def search_people(db: AsyncSession, q: str, limit: int) -> list[Person]:
    """People by any identity's name / handle / phone / id, or the person's
    own name / notes; important people first."""
    matching_identities = select(Identity.person_id).where(or_(
        contains_ci(Identity.display_name, q),
        contains_ci(Identity.username, q),
        contains_ci(Identity.phone, q),
        contains_ci(Identity.external_id, q),
    ))
    return list((await db.execute(
        select(Person)
        .where(or_(
            Person.id.in_(matching_identities),
            contains_ci(Person.display_name, q),
            contains_ci(Person.notes, q),
        ))
        .order_by(Person.is_important.desc(), Person.name)
        .limit(limit)
    )).scalars().all())


async def search_organizations(db: AsyncSession, q: str, limit: int) -> list[Organization]:
    return list((await db.execute(
        select(Organization)
        .where(or_(contains_ci(Organization.name, q), contains_ci(Organization.context, q)))
        .order_by(Organization.name)
        .limit(limit)
    )).scalars().all())
