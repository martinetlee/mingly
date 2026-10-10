"""Path-id lookups shared by the conversation, message, person, compose and
admin routes: 404 for unknown ids, 409 ``unsupported_channel`` when an operation
needs a channel the conversation isn't on."""

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.models import Conversation, Message, Person
from backend.serializers import channel_of
from backend.telegram import ids


async def conversation_or_404(db: AsyncSession, conversation_id: int) -> Conversation:
    conv = await crud.get_conversation(db, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


async def message_or_404(db: AsyncSession, message_id: int) -> Message:
    msg = await crud.get_message(db, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


async def person_or_404(db: AsyncSession, person_id: int) -> Person:
    """404 for unknown ids; a person that was merged away answers
    ``{"detail", "merged_into": <surviving person id>}``."""
    person = await crud.get_person(db, person_id)
    if person is None:
        survivor = await crud.merged_into(db, person_id)
        if survivor is not None:
            raise HTTPException(status_code=404, detail={"message": "Person not found", "merged_into": survivor})
        raise HTTPException(status_code=404, detail="Person not found")
    return person


def unsupported_channel(conv: Conversation, what: str) -> HTTPException:
    channel = channel_of(conv)
    return HTTPException(status_code=409, detail={
        "message": f"{what} is not available for {channel or 'this'} conversations",
        "code": "unsupported_channel",
        "channel": channel,
    })


def telegram_peer(conv: Conversation, what: str = "This action") -> int:
    """The Telegram peer id of ``conv``, or 409 for other channels."""
    if not ids.is_telegram(conv):
        raise unsupported_channel(conv, what)
    return ids.peer_id_of(conv)
