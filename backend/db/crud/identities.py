"""Identities (one handle on one channel) and the people they belong to.

Every connector records the users it sees through ``upsert_identity``; a new
identity gets its own new person. Linking identities of the same human into
one person is ``backend.people.linking``'s job (Phase 2).
"""

import re
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import ContactPoint, Identity, Person

__all__ = [
    "PROFILE_FIELDS", "normalize_phone", "normalize_email", "upsert_identity", "get_identity",
    "get_identities", "get_identity_by_external", "identity_ids_by_external", "get_person", "get_people",
    "identities_for_person", "identities_for_people", "identity_ids_for_person", "self_person",
]

# Profile columns a connector may set; None = unknown, keep the stored value.
PROFILE_FIELDS = (
    "display_name", "first_name", "last_name", "username", "phone", "bio", "avatar_path",
    "is_contact", "is_bot", "is_deleted", "is_self",
)


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Digits with a leading '+' (Telegram reports international numbers
    without it). None when there are too few digits to be a phone number."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return f"+{digits}" if len(digits) >= 6 else None


def normalize_email(raw: Optional[str]) -> Optional[str]:
    if not raw or "@" not in raw:
        return None
    return raw.strip().lower()


async def _sync_contact_points(db: AsyncSession, identity: Identity) -> None:
    wanted = set()
    phone = normalize_phone(identity.phone)
    if phone:
        wanted.add(("phone", phone))
    if identity.channel in ("gmail", "email"):
        email = normalize_email(identity.external_id)
        if email:
            wanted.add(("email", email))
    current = {
        (cp.kind, cp.value): cp for cp in (await db.execute(
            select(ContactPoint).where(ContactPoint.identity_id == identity.id)
        )).scalars().all()
    }
    for key in wanted - set(current):
        db.add(ContactPoint(identity_id=identity.id, kind=key[0], value=key[1]))
    stale = [cp.id for key, cp in current.items() if key not in wanted]
    if stale:
        await db.execute(delete(ContactPoint).where(ContactPoint.id.in_(stale)))


async def upsert_identity(
    db: AsyncSession, *, channel: str, external_id: str, commit: bool = True, **profile
) -> Identity:
    """Create or update the identity ``(channel, external_id)``.

    Profile fields (see PROFILE_FIELDS) that are not None take the new
    value. A new identity gets a new person (marked ``is_self`` when the
    identity is). Returns the stored identity.
    """
    unknown = set(profile) - set(PROFILE_FIELDS)
    if unknown:
        raise TypeError(f"upsert_identity: unknown fields {sorted(unknown)}")
    given = {k: v for k, v in profile.items() if v is not None}

    identity = await get_identity_by_external(db, channel, external_id)
    if identity is None:
        person = Person(is_self=bool(given.get("is_self")))
        db.add(person)
        await db.flush()
        inserted = (await db.execute(
            sqlite_insert(Identity)
            .values(person_id=person.id, channel=channel, external_id=external_id, **given)
            .on_conflict_do_nothing(index_elements=[Identity.channel, Identity.external_id])
            .returning(Identity.id)
        )).scalar()
        if inserted is None:  # created concurrently: drop our person, update theirs
            await db.delete(person)
        identity = (await db.execute(
            select(Identity).where(Identity.channel == channel, Identity.external_id == external_id)
            .execution_options(populate_existing=True)
        )).scalar_one()
        if inserted is None:
            for k, v in given.items():
                setattr(identity, k, v)
    else:
        for k, v in given.items():
            if getattr(identity, k) != v:
                setattr(identity, k, v)
        if given:
            identity.updated_at = datetime.utcnow()
    if given.get("is_self"):
        await db.execute(update(Person).where(Person.id == identity.person_id).values(is_self=True))
    await db.flush()
    await _sync_contact_points(db, identity)
    if commit:
        await db.commit()
    return identity


async def get_identity(db: AsyncSession, identity_id: int) -> Optional[Identity]:
    return (await db.execute(select(Identity).where(Identity.id == identity_id))).scalar_one_or_none()


async def get_identities(db: AsyncSession, identity_ids: Iterable[int]) -> dict[int, Identity]:
    ids = list(set(identity_ids))
    out: dict[int, Identity] = {}
    for i in range(0, len(ids), 500):
        res = await db.execute(select(Identity).where(Identity.id.in_(ids[i:i + 500])))
        out.update({x.id: x for x in res.scalars().all()})
    return out


async def get_identity_by_external(db: AsyncSession, channel: str, external_id: str) -> Optional[Identity]:
    return (await db.execute(
        select(Identity).where(Identity.channel == channel, Identity.external_id == external_id)
    )).scalar_one_or_none()


async def identity_ids_by_external(db: AsyncSession, channel: str, external_ids: Iterable[str]) -> dict[str, int]:
    ext = list(set(external_ids))
    out: dict[str, int] = {}
    for i in range(0, len(ext), 500):
        res = await db.execute(
            select(Identity.external_id, Identity.id).where(
                Identity.channel == channel, Identity.external_id.in_(ext[i:i + 500])
            )
        )
        out.update({e: iid for e, iid in res.all()})
    return out


async def get_person(db: AsyncSession, person_id: int) -> Optional[Person]:
    return (await db.execute(select(Person).where(Person.id == person_id))).scalar_one_or_none()


async def get_people(db: AsyncSession, person_ids: Iterable[int]) -> dict[int, Person]:
    ids = list(set(person_ids))
    out: dict[int, Person] = {}
    for i in range(0, len(ids), 500):
        res = await db.execute(select(Person).where(Person.id.in_(ids[i:i + 500])))
        out.update({p.id: p for p in res.scalars().all()})
    return out


async def identities_for_person(db: AsyncSession, person_id: int) -> list[Identity]:
    return list((await db.execute(
        select(Identity).where(Identity.person_id == person_id).order_by(Identity.id)
    )).scalars().all())


async def identities_for_people(db: AsyncSession, person_ids: Iterable[int]) -> dict[int, list[Identity]]:
    ids = list(set(person_ids))
    out: dict[int, list[Identity]] = {pid: [] for pid in ids}
    for i in range(0, len(ids), 500):
        res = await db.execute(
            select(Identity).where(Identity.person_id.in_(ids[i:i + 500])).order_by(Identity.id)
        )
        for ident in res.scalars().all():
            out[ident.person_id].append(ident)
    return out


async def identity_ids_for_person(db: AsyncSession, person_id: int) -> list[int]:
    return list((await db.execute(
        select(Identity.id).where(Identity.person_id == person_id)
    )).scalars().all())


async def self_person(db: AsyncSession) -> Optional[Person]:
    return (await db.execute(
        select(Person).where(Person.is_self == True).order_by(Person.id).limit(1)  # noqa: E712
    )).scalar_one_or_none()
