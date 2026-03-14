"""Catch Up triage API -- snooze, remind-later, mark-handled, add note."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc, asc, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import CatchUpItem, Contact, Message
from backend.db import crud

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class SnoozeRequest(BaseModel):
    hours: int = 24


class NoteRequest(BaseModel):
    note: str


class RelationshipStatusRequest(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item_dict(item: CatchUpItem) -> dict:
    return {
        "id": item.id,
        "chat_telegram_id": item.chat_telegram_id,
        "contact_id": item.contact_id,
        "status": item.status,
        "note": item.note,
        "snoozed_until": item.snoozed_until.isoformat() + "Z" if item.snoozed_until else None,
        "handled_at": item.handled_at.isoformat() + "Z" if item.handled_at else None,
        "created_at": item.created_at.isoformat() + "Z" if item.created_at else None,
        "updated_at": item.updated_at.isoformat() + "Z" if item.updated_at else None,
    }


async def _get_or_create_item(
    db: AsyncSession, chat_telegram_id: int,
) -> CatchUpItem:
    result = await db.execute(
        select(CatchUpItem).where(CatchUpItem.chat_telegram_id == chat_telegram_id)
    )
    item = result.scalar_one_or_none()
    if item:
        return item
    item = CatchUpItem(
        chat_telegram_id=chat_telegram_id,
        status="pending",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/items")
async def list_catchup_items(db: AsyncSession = Depends(get_db)):
    """Return active (pending or un-snoozed) catchup items."""
    now = datetime.utcnow()
    result = await db.execute(
        select(CatchUpItem).where(
            or_(
                CatchUpItem.status == "pending",
                and_(
                    CatchUpItem.status == "snoozed",
                    CatchUpItem.snoozed_until <= now,
                ),
            )
        ).order_by(desc(CatchUpItem.updated_at))
    )
    return [_item_dict(i) for i in result.scalars().all()]


@router.get("/all")
async def list_all_catchup_items(db: AsyncSession = Depends(get_db)):
    """Return all catchup items regardless of status."""
    result = await db.execute(
        select(CatchUpItem).order_by(desc(CatchUpItem.updated_at))
    )
    return [_item_dict(i) for i in result.scalars().all()]


@router.post("/{chat_telegram_id}/snooze")
async def snooze_item(
    chat_telegram_id: int,
    req: SnoozeRequest,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, chat_telegram_id)
    item.status = "snoozed"
    item.snoozed_until = datetime.utcnow() + timedelta(hours=req.hours)
    item.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(item)
    return _item_dict(item)


@router.post("/{chat_telegram_id}/handled")
async def mark_handled(
    chat_telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, chat_telegram_id)
    item.status = "handled"
    item.handled_at = datetime.utcnow()
    item.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(item)
    return _item_dict(item)


@router.post("/{chat_telegram_id}/reopen")
async def reopen_item(
    chat_telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, chat_telegram_id)
    item.status = "pending"
    item.snoozed_until = None
    item.handled_at = None
    item.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(item)
    return _item_dict(item)


@router.post("/{chat_telegram_id}/note")
async def add_note(
    chat_telegram_id: int,
    req: NoteRequest,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, chat_telegram_id)
    item.note = req.note.strip() or None
    item.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(item)
    return _item_dict(item)


@router.post("/{chat_telegram_id}/relationship-status")
async def update_relationship_from_catchup(
    chat_telegram_id: int,
    req: RelationshipStatusRequest,
    db: AsyncSession = Depends(get_db),
):
    if req.status not in crud.VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(crud.VALID_STATUSES))}",
        )
    contact = (await db.execute(
        select(Contact).where(Contact.telegram_id == chat_telegram_id)
    )).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found for this chat")
    contact.relationship_status = req.status
    contact.relationship_status_manual = True
    await db.commit()
    await db.refresh(contact)
    return {
        "contact_id": contact.id,
        "relationship_status": contact.relationship_status,
    }


def _msg_dict(msg: Message) -> dict:
    return {
        "sender_name": msg.sender_name,
        "text": (msg.text[:200] if msg.text else None),
        "date": msg.date.isoformat() + "Z" if msg.date else None,
        "is_outgoing": msg.is_outgoing,
        "media_type": msg.media_type,
    }


# Short messages that look like chit-chat / filler
_CHITCHAT_WORDS = {
    "hi", "hey", "hello", "haha", "lol", "ok", "okay", "thanks", "thank you",
    "ty", "np", "sure", "yes", "no", "yeah", "yep", "nope", "cool", "nice",
    "great", "k", "kk", "hehe", "hmm", "ah", "oh", "wow",
}


def _is_chitchat(text: str | None) -> bool:
    if not text:
        return True
    stripped = text.strip().lower().rstrip("!?.,:;")
    # Very short messages or single-word filler
    if stripped in _CHITCHAT_WORDS:
        return True
    # Emoji-only messages (no alphanumeric)
    if not any(c.isalnum() for c in stripped):
        return True
    return False


@router.get("/{chat_telegram_id}/context")
async def get_catchup_context(
    chat_telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return context for a catch-up card: messages since user's last activity
    (last outgoing message or last reaction), filtering out chit-chat,
    plus the last 3 messages from the chat."""

    # 1. Find the user's last outgoing message or last reaction in this chat
    last_outgoing_q = (
        select(Message.date)
        .where(
            Message.chat_id == chat_telegram_id,
            Message.is_outgoing == True,  # noqa: E712
        )
        .order_by(desc(Message.date))
        .limit(1)
    )
    last_outgoing_row = (await db.execute(last_outgoing_q)).scalar_one_or_none()

    last_reaction_q = (
        select(Message.date)
        .where(
            Message.chat_id == chat_telegram_id,
            Message.my_reaction != None,  # noqa: E711
        )
        .order_by(desc(Message.date))
        .limit(1)
    )
    last_reaction_row = (await db.execute(last_reaction_q)).scalar_one_or_none()

    # Pick the most recent of the two
    anchor_date = None
    if last_outgoing_row and last_reaction_row:
        anchor_date = max(last_outgoing_row, last_reaction_row)
    elif last_outgoing_row:
        anchor_date = last_outgoing_row
    elif last_reaction_row:
        anchor_date = last_reaction_row

    # 2. Get messages after anchor (incoming only), filter chit-chat
    summary_messages: list[dict] = []
    if anchor_date:
        after_msgs_q = (
            select(Message)
            .where(
                Message.chat_id == chat_telegram_id,
                Message.date > anchor_date,
                Message.is_outgoing == False,  # noqa: E712
            )
            .order_by(asc(Message.date))
            .limit(20)
        )
        after_msgs = (await db.execute(after_msgs_q)).scalars().all()

        for m in after_msgs:
            if not _is_chitchat(m.text):
                summary_messages.append(_msg_dict(m))
    else:
        # No anchor -- show recent incoming messages
        recent_incoming_q = (
            select(Message)
            .where(
                Message.chat_id == chat_telegram_id,
                Message.is_outgoing == False,  # noqa: E712
            )
            .order_by(desc(Message.date))
            .limit(5)
        )
        recent_incoming = (await db.execute(recent_incoming_q)).scalars().all()
        for m in reversed(recent_incoming):
            if not _is_chitchat(m.text):
                summary_messages.append(_msg_dict(m))

    # 3. Get last 3 messages from the chat (any direction)
    last_3_q = (
        select(Message)
        .where(Message.chat_id == chat_telegram_id)
        .order_by(desc(Message.date))
        .limit(3)
    )
    last_3 = (await db.execute(last_3_q)).scalars().all()
    recent_messages = [_msg_dict(m) for m in reversed(last_3)]

    # 4. Build a text summary from the non-chitchat messages
    summary_text = ""
    if summary_messages:
        count = len(summary_messages)
        senders = list({m["sender_name"] for m in summary_messages if m["sender_name"]})
        sender_str = ", ".join(senders[:3]) if senders else "Someone"
        summary_text = f"{count} message{'s' if count != 1 else ''} from {sender_str} since your last activity."
    elif anchor_date:
        summary_text = "Only casual messages since your last activity."
    else:
        summary_text = "New conversation awaiting your first reply."

    return {
        "summary": summary_text,
        "summary_messages": summary_messages[:5],  # cap at 5 most relevant
        "recent_messages": recent_messages,
        "last_activity_at": anchor_date.isoformat() + "Z" if anchor_date else None,
    }
