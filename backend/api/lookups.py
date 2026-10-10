"""Lookups shared by the routes: 404 for unknown ids, and the conversation's
connector (or 409 ``unsupported_channel`` when it can't do what's asked)."""

from typing import Any, Awaitable, Optional, Type, TypeVar

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.base import Capability, Connector, UnsupportedOperation
from backend.connectors.manager import manager
from backend.db import crud
from backend.db.models import Conversation, Message, Person
from backend.errors import DomainError
from backend.serializers import channel_of

R = TypeVar("R")


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


def connector_for(conv: Conversation) -> Optional[Connector]:
    """The connector serving the conversation's account (None when that
    account isn't connected)."""
    return manager.for_account(channel_of(conv), conv.account_id)


def connector_with(conv: Conversation, role: Type[R], what: str,
                   capability: Optional[Capability] = None) -> R:
    """The conversation's connector as ``role`` (a ``connectors.roles``
    interface, or ``Connector`` itself), or 409 ``unsupported_channel`` when
    it has no such role / ``capability`` in this conversation."""
    connector = connector_for(conv)
    if connector is None:
        raise unsupported_channel(conv, what)
    if capability is not None and capability not in connector.conversation_capabilities(conv):
        raise unsupported_channel(conv, what)
    if not isinstance(connector, role):
        raise unsupported_channel(conv, what)
    return connector


async def channel_call(conv: Conversation, call: Awaitable[Any], failure: Optional[str] = None,
                       status: int = 400, passthrough: tuple = ()) -> Any:
    """Await a connector operation and map its errors: domain errors (503 /
    429 / 404 …), HTTP errors and ``passthrough`` types pass through,
    ``UnsupportedOperation`` is 409 ``unsupported_channel``, anything else is
    the channel refusing it: ``status`` with ``"<failure>: <error>"`` (or just
    the error)."""
    try:
        return await call
    except (HTTPException, DomainError, *passthrough):
        raise
    except UnsupportedOperation as e:
        raise unsupported_channel(conv, str(e))
    except Exception as e:
        raise HTTPException(status_code=status, detail=f"{failure}: {e}" if failure else str(e))
