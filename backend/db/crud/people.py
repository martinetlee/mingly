"""People CRM: person records, positions, relationship status, address-book contacts.

A person's interaction history is every ``direct`` conversation whose peer
identity belongs to them, across all channels (``_direct_conversations``).
"""

from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import desc, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.crud.organizations import get_or_create_organization
from backend.db.crud.text import contains_ci
from backend.db.crud.conversations import one_to_one_filter
from backend.db.models import Conversation, Identity, Message, Organization, Person, PersonPosition
from backend.db.results import affected_rows

__all__ = [
    "VALID_STATUSES",
    "PositionConflict",
    "explain_relationship_status",
    "interaction_stats",
    "status_from_stats",
    "compute_relationship_status",
    "refresh_person_status",
    "refresh_all_person_statuses",
    "get_important_people",
    "list_people",
    "apply_person_fields",
    "set_manual_relationship_status",
    "get_person_positions",
    "positions_for_people",
    "get_person_position",
    "add_person_position",
    "direct_conversation_ids_for_person",
    "set_address_book",
]

VALID_STATUSES = {"new", "active", "nurturing", "stalled", "cold", "needs_reply"}


class PositionConflict(ValueError):
    """The person already has a position at this organization."""


# ── Relationship status ──────────────────────────────────────────────────────

def explain_relationship_status(
    status: Optional[str],
    is_manual: bool,
    last_interaction: Optional[datetime],
    last_outgoing: Optional[datetime],
    last_incoming: Optional[datetime],
    is_important: bool = False,
) -> dict:
    """Human-readable explanation, recommended action and metadata about why a
    person has their current relationship status."""
    now = datetime.utcnow()

    def _days_ago(dt: Optional[datetime]) -> Optional[int]:
        return None if dt is None else (now - dt).days

    days_since_interaction = _days_ago(last_interaction)
    days_since_outgoing = _days_ago(last_outgoing)
    days_since_incoming = _days_ago(last_incoming)

    if status == "new":
        if not last_interaction:
            explanation = "No message history found with this person."
            recommended_action = "Send an introductory message to start the conversation."
        elif not last_outgoing:
            explanation = "They have messaged you, but you have never replied."
            recommended_action = "Send a first reply to establish the relationship."
        else:
            explanation = "This person was recently added."
            recommended_action = "Reach out to start building the relationship."
    elif status == "needs_reply":
        wait_days = days_since_incoming if days_since_incoming is not None else 0
        if wait_days == 0:
            explanation = "They sent a message today that you haven't replied to yet."
        elif wait_days == 1:
            explanation = "They sent a message yesterday that you haven't replied to yet."
        else:
            explanation = f"They sent a message {wait_days} days ago that you haven't replied to yet."
        recommended_action = "Reply to their latest message to keep the conversation going."
    elif status == "active":
        explanation = f"You exchanged messages within the last week ({days_since_interaction} days ago)."
        recommended_action = "Relationship is healthy. Continue engaging naturally."
    elif status == "nurturing":
        explanation = f"Last interaction was {days_since_interaction} days ago (1-4 weeks)."
        recommended_action = "Consider reaching out soon to maintain the connection."
    elif status == "stalled":
        explanation = (
            f"Last interaction was {days_since_interaction} days ago (1-3 months). "
            "The relationship is losing momentum."
        )
        recommended_action = "Send a check-in message to re-engage before the relationship goes cold."
    elif status == "cold":
        explanation = (
            f"Last interaction was {days_since_interaction} days ago (over 3 months). "
            "The relationship has gone quiet."
        )
        recommended_action = "Reach out with a personal note or relevant update to reconnect."
    else:
        explanation = "Status has not been determined yet."
        recommended_action = "Sync messages to compute the relationship status."

    if is_manual:
        explanation = f"Manually set. {explanation}"

    attention_reason = ""
    if is_important:
        attention_reason = {
            "needs_reply": "Important contact is waiting for your reply.",
            "stalled": "Important contact relationship is losing momentum.",
            "cold": "Important contact has gone quiet -- consider reconnecting.",
            "nurturing": "Important contact may need a check-in soon.",
        }.get(status or "", "Marked as an important contact.")

    return {
        "status_explanation": explanation,
        "recommended_action": recommended_action,
        "status_source": "manual" if is_manual else "auto",
        "attention_reason": attention_reason,
        "days_since_interaction": days_since_interaction,
        "days_since_outgoing": days_since_outgoing,
        "days_since_incoming": days_since_incoming,
    }


def _direct_conversations():
    """(conversation_id, person_id) of every one-to-one conversation: a direct
    chat, or an email thread with a single counterpart (peer identity set)."""
    return (
        select(Conversation.id.label("conversation_id"), Identity.person_id.label("person_id"))
        .join(Identity, Identity.id == Conversation.peer_identity_id)
        .where(one_to_one_filter())
    )


async def interaction_stats(db: AsyncSession, person_ids: Iterable[int]) -> dict[int, tuple]:
    """``{person_id: (last_outgoing, last_incoming, latest_incoming_reacted)}``
    over all the person's direct conversations, for people with any message.
    Service messages don't count."""
    ids = list(set(person_ids))
    out: dict[int, tuple] = {}
    for i in range(0, len(ids), 500):
        direct = _direct_conversations().where(Identity.person_id.in_(ids[i:i + 500])).subquery()
        msgs = (
            select(
                direct.c.person_id,
                Message.is_outgoing,
                Message.date,
                Message.my_reaction,
                func.row_number().over(
                    partition_by=(direct.c.person_id, Message.is_outgoing),
                    order_by=(desc(Message.date), desc(Message.id)),
                ).label("rn"),
            )
            .join(direct, direct.c.conversation_id == Message.conversation_id)
            .where(Message.kind == "message")
            .subquery()
        )
        rows = (await db.execute(
            select(msgs.c.person_id, msgs.c.is_outgoing, msgs.c.date, msgs.c.my_reaction).where(msgs.c.rn == 1)
        )).all()
        for pid, outgoing, date, reaction in rows:
            last_out, last_in, reacted = out.get(pid, (None, None, False))
            if outgoing:
                last_out = date
            else:
                last_in, reacted = date, bool(reaction)
            out[pid] = (last_out, last_in, reacted)
    return out


def status_from_stats(stats: Optional[tuple], now: Optional[datetime] = None) -> tuple:
    """(status, last_interaction, last_outgoing, last_incoming) from interaction_stats."""
    if stats is None:
        return ("new", None, None, None)
    last_outgoing, last_incoming, latest_incoming_reacted = stats
    if not last_outgoing and not last_incoming:
        return ("new", None, None, None)
    last_interaction = max(d for d in (last_outgoing, last_incoming) if d)
    if not last_outgoing:
        return ("new", last_interaction, None, last_incoming)
    # They wrote more recently than we did — unless we reacted to it (acknowledged)
    if last_incoming and last_incoming > last_outgoing and not latest_incoming_reacted:
        return ("needs_reply", last_interaction, last_outgoing, last_incoming)
    days_since = ((now or datetime.utcnow()) - last_interaction).days
    if days_since <= 7:
        status = "active"
    elif days_since <= 30:
        status = "nurturing"
    elif days_since <= 90:
        status = "stalled"
    else:
        status = "cold"
    return (status, last_interaction, last_outgoing, last_incoming)


async def compute_relationship_status(db: AsyncSession, person: Person) -> tuple:
    """(status, last_interaction, last_outgoing, last_incoming) from the
    person's message history across all their direct conversations."""
    return status_from_stats((await interaction_stats(db, [person.id])).get(person.id))


def _apply_status(person: Person, computed: tuple) -> None:
    status, last_interaction, last_outgoing, last_incoming = computed
    person.relationship_status = status
    person.last_interaction_at = last_interaction
    person.last_outgoing_at = last_outgoing
    person.last_incoming_at = last_incoming


async def refresh_person_status(db: AsyncSession, person_id: int) -> Optional[Person]:
    """Recompute and save a person's relationship status (manual overrides are kept)."""
    person = await db.get(Person, person_id)
    if person is None:
        return None
    if person.relationship_status_manual:
        return person
    _apply_status(person, await compute_relationship_status(db, person))
    await db.commit()
    await db.refresh(person)
    return person


def _crm_people_filter():
    """People the CRM tracks a relationship with: address-book contacts,
    important people and anyone with a direct conversation."""
    has_direct = exists(
        _direct_conversations().where(Identity.person_id == Person.id).correlate(Person)
    )
    return or_(Person.is_contact, Person.is_important == True, has_direct)  # noqa: E712


async def refresh_all_person_statuses(db: AsyncSession) -> int:
    """Recompute every non-manual tracked person's status. Returns the count."""
    people = list((await db.execute(
        select(Person).where(
            Person.relationship_status_manual == False,  # noqa: E712
            Person.is_self == False,  # noqa: E712
            _crm_people_filter(),
        )
    )).scalars().all())
    stats = await interaction_stats(db, [p.id for p in people])
    now = datetime.utcnow()
    for person in people:
        _apply_status(person, status_from_stats(stats.get(person.id), now))
    await db.commit()
    return len(people)


# ── Reads ────────────────────────────────────────────────────────────────────

async def get_important_people(db: AsyncSession, limit: Optional[int] = None) -> list[Person]:
    q = select(Person).where(Person.is_important == True).order_by(Person.name, Person.id)  # noqa: E712
    if limit is not None:
        q = q.limit(limit)
    return list((await db.execute(q)).scalars().all())


async def list_people(
    db: AsyncSession,
    *,
    search: str = "",
    status: str = "",
    important: bool = False,
    contacts_only: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Paginated people (never the user themself), filtered in SQL.

    Returns {people, total, status_counts, important_count}: ``total`` counts
    matches of every filter; ``status_counts`` matches of search + important +
    contacts_only per relationship status ("none" = undetermined);
    ``important_count`` important matches of search + contacts_only.
    """
    filters = [Person.is_self == False]  # noqa: E712
    if contacts_only:
        filters.append(Person.is_contact)
    term = search.strip()
    if term:
        filters.append(or_(
            contains_ci(Person.display_name, term),
            exists(select(Identity.id).where(
                Identity.person_id == Person.id,
                or_(
                    contains_ci(Identity.display_name, term),
                    contains_ci(Identity.username, term),
                    contains_ci(Identity.phone, term),
                ),
            ).correlate(Person)),
        ))
    important_count = (await db.execute(
        select(func.count()).select_from(Person).where(*filters, Person.is_important == True)  # noqa: E712
    )).scalar() or 0
    if important:
        filters.append(Person.is_important == True)  # noqa: E712
    status_counts = {
        (st or "none"): n for st, n in (await db.execute(
            select(Person.relationship_status, func.count()).where(*filters).group_by(Person.relationship_status)
        )).all()
    }
    if status and status != "all":
        filters.append(Person.relationship_status == status)
    total = (await db.execute(select(func.count()).select_from(Person).where(*filters))).scalar() or 0
    people = list((await db.execute(
        select(Person).where(*filters)
        .order_by(Person.name.is_(None), func.lower(Person.name), Person.id)
        .limit(limit).offset(offset)
    )).scalars().all())
    return {"people": people, "total": total, "status_counts": status_counts, "important_count": important_count}


async def direct_conversation_ids_for_person(db: AsyncSession, person_id: int) -> list[int]:
    """The person's direct conversations on every channel, most recent first."""
    direct = _direct_conversations().where(Identity.person_id == person_id).subquery()
    return list((await db.execute(
        select(Conversation.id)
        .join(direct, direct.c.conversation_id == Conversation.id)
        .order_by(Conversation.last_activity_at.is_(None), desc(Conversation.last_activity_at), Conversation.id)
    )).scalars().all())


# ── Writes ───────────────────────────────────────────────────────────────────

def apply_person_fields(
    person: Person,
    *,
    display_name: Optional[str] = None,
    twitter_handle: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    notes: Optional[str] = None,
    is_important: Optional[bool] = None,
) -> None:
    """Set the given fields (None = leave alone, "" = clear; a cleared
    display_name falls back to the identities' names). Raises ValueError for
    an invalid LinkedIn URL before touching anything. Doesn't commit."""
    from backend.api.validators import validate_linkedin_url

    linkedin = None
    if linkedin_url is not None:
        raw = linkedin_url.strip()
        linkedin = validate_linkedin_url(raw) if raw else ""
    if display_name is not None:
        person.display_name = display_name.strip() or None
    if twitter_handle is not None:
        person.twitter_handle = twitter_handle.strip() or None
    if linkedin is not None:
        person.linkedin_url = linkedin or None
    if notes is not None:
        person.notes = notes.strip() or None
    if is_important is not None:
        person.is_important = is_important


def set_manual_relationship_status(person: Person, status: str) -> None:
    """Pin a relationship status by hand. Raises ValueError for an unknown status."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status. Must be one of: {', '.join(sorted(VALID_STATUSES))}")
    person.relationship_status = status
    person.relationship_status_manual = True


# ── Positions ────────────────────────────────────────────────────────────────

def _positions_query():
    # Current first, then by org name — the first active one is the "primary" position
    return select(PersonPosition).order_by(
        PersonPosition.is_active.desc(), func.lower(PersonPosition.organization), PersonPosition.id
    )


async def get_person_positions(db: AsyncSession, person_id: int) -> list[PersonPosition]:
    return list((await db.execute(_positions_query().where(PersonPosition.person_id == person_id))).scalars().all())


async def positions_for_people(db: AsyncSession, person_ids: Iterable[int]) -> dict[int, list[PersonPosition]]:
    ids = list(set(person_ids))
    out: dict[int, list[PersonPosition]] = {pid: [] for pid in ids}
    for i in range(0, len(ids), 500):
        res = await db.execute(_positions_query().where(PersonPosition.person_id.in_(ids[i:i + 500])))
        for p in res.scalars().all():
            out[p.person_id].append(p)
    return out


async def get_person_position(db: AsyncSession, person_id: int, position_id: int) -> Optional[PersonPosition]:
    return (await db.execute(select(PersonPosition).where(
        PersonPosition.id == position_id, PersonPosition.person_id == person_id,
    ))).scalar_one_or_none()


async def add_person_position(
    db: AsyncSession,
    person_id: int,
    *,
    organization: Optional[str] = None,
    organization_id: Optional[int] = None,
    title: Optional[str] = None,
    is_active: bool = True,
    ended_at: Optional[datetime] = None,
) -> PersonPosition:
    """Add a position; the org is given by id or by name (matched
    case/whitespace-insensitively, created if new). Commits. Raises
    LookupError for an unknown person or organization_id, ValueError for an
    empty name and PositionConflict when the person already has a position there."""
    if await db.get(Person, person_id) is None:
        raise LookupError("Person not found")
    if organization_id is not None:
        org = await db.get(Organization, organization_id)
        if org is None:
            raise LookupError("Organization not found")
    else:
        org, _created = await get_or_create_organization(db, organization or "")

    duplicate = (await db.execute(select(PersonPosition.id).where(
        PersonPosition.person_id == person_id, PersonPosition.organization_id == org.id,
    ))).first()
    if duplicate:
        await db.commit()  # keep a newly created org / re-attached history
        raise PositionConflict(f"Already has a position at {org.name}")

    pos = PersonPosition(
        person_id=person_id,
        organization_id=org.id,
        organization=org.name,
        title=(title or "").strip() or None,
        is_active=is_active,
        ended_at=ended_at if not is_active else None,
    )
    db.add(pos)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise PositionConflict("Position already exists")
    await db.refresh(pos)
    return pos


# ── Address book ─────────────────────────────────────────────────────────────

async def set_address_book(db: AsyncSession, channel: str, contact_identity_ids: Iterable[int]) -> int:
    """After a full address-book sync of ``channel``: identities on it that
    are no longer in the address book stop being contacts. Returns how many
    were cleared. Doesn't commit."""
    keep = list(set(contact_identity_ids))
    q = update(Identity).where(Identity.channel == channel, Identity.is_contact == True)  # noqa: E712
    if keep:
        q = q.where(Identity.id.notin_(keep))
    res = await db.execute(q.values(is_contact=False, updated_at=datetime.utcnow()))
    return affected_rows(res)

