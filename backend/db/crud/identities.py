"""Identities (one handle on one channel) and the people they belong to.

``store_identity`` writes an identity row (a new identity gets its own new
person). Connectors go through ``backend.people.identities.upsert_identity``,
which adds cross-channel linking on top.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Tuple

import phonenumbers
from sqlalchemy import delete, or_, select, union, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import ContactPoint, Identity, Person, PersonMerge, PersonNote, PersonPosition
from backend import prefs
from backend.db.database import Base

__all__ = [
    "PROFILE_FIELDS", "IdentityChange", "normalize_phone", "normalize_email", "store_identity",
    "sync_contact_points", "get_identity",
    "get_identities", "get_identity_by_external", "identity_ids_by_external", "get_person", "get_people",
    "identities_for_person", "identities_for_people", "identity_ids_for_person", "self_person",
    "collect_orphans_after_account_removal", "profile_differs", "identities_by_external",
]

# Profile columns a connector may set; None = unknown, keep the stored value.
PROFILE_FIELDS = (
    "display_name", "first_name", "last_name", "username", "phone", "bio", "avatar_path",
    "is_contact", "is_bot", "is_deleted", "is_self",
)


def normalize_phone(raw: Optional[str], region: Optional[str] = None) -> Optional[str]:
    """E.164 (``+15551234567``) or None when ``raw`` can't be a phone number.

    Telegram reports international numbers without the '+', so a number
    without one is read as international unless ``region`` (ISO 3166 code
    for national-format numbers, e.g. from a local address book) is given."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("00"):
        text = "+" + text[2:]
    if not text.startswith("+") and region is None:
        text = "+" + re.sub(r"\D", "", text)
    try:
        number = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def normalize_email(raw: Optional[str]) -> Optional[str]:
    if not raw or "@" not in raw:
        return None
    return raw.strip().lower()


async def sync_contact_points(db: AsyncSession, identity: Identity) -> bool:
    """Make the identity's contact points match its handles. Returns whether
    any point was added (a new key for cross-channel linking)."""
    wanted = set()
    phone = normalize_phone(identity.phone)
    if phone:
        wanted.add(("phone", phone))
    # Any channel whose handle is an email address (Gmail, iMessage by email);
    # Telegram / Signal handles never contain "@".
    email = normalize_email(identity.external_id)
    if email:
        wanted.add(("email", email))
    current = {
        (cp.kind, cp.value): cp for cp in (await db.execute(
            select(ContactPoint).where(ContactPoint.identity_id == identity.id)
        )).scalars().all()
    }
    added = wanted - set(current)
    for key in added:
        db.add(ContactPoint(identity_id=identity.id, kind=key[0], value=key[1]))
    stale = [cp.id for key, cp in current.items() if key not in wanted]
    if stale:
        await db.execute(delete(ContactPoint).where(ContactPoint.id.in_(stale)))
    return bool(added)


@dataclass(frozen=True)
class IdentityChange:
    """What ``store_identity`` did: a new row, which profile fields changed,
    and whether the username is new (a linking hint)."""
    created: bool
    changed: frozenset
    new_username: bool

    @property
    def any(self) -> bool:
        return self.created or bool(self.changed)


async def store_identity(
    db: AsyncSession, *, channel: str, external_id: str, **profile
) -> Tuple[Identity, IdentityChange]:
    """Create or update the identity row ``(channel, external_id)``; no commit.

    Profile fields (see PROFILE_FIELDS) that are not None take the new
    value. A new identity gets a new person (marked ``is_self`` when the
    identity is). Cross-channel linking is the caller's business
    (``backend.people.identities.upsert_identity``). Flushes when anything
    changed.
    """
    unknown = set(profile) - set(PROFILE_FIELDS)
    if unknown:
        raise TypeError(f"store_identity: unknown fields {sorted(unknown)}")
    given = {k: v for k, v in profile.items() if v is not None}

    identity = await get_identity_by_external(db, channel, external_id)
    new_username = bool(given.get("username")) and (identity is None or identity.username != given["username"])
    created = identity is None
    changed: set = set()
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
        # Write (and bump updated_at) only when the channel told us something new
        changed = {k for k, v in given.items() if getattr(identity, k) != v}
        for k in changed:
            setattr(identity, k, given[k])
        if changed:
            identity.updated_at = datetime.utcnow()
    if given.get("is_self") and (created or "is_self" in changed):
        await db.execute(update(Person).where(Person.id == identity.person_id).values(is_self=True))
    change = IdentityChange(created=created, changed=frozenset(changed), new_username=new_username)
    if change.any:
        await db.flush()
    return identity, change


def profile_differs(identity: Identity, profile: dict) -> bool:
    """Whether ``upsert_identity(**profile)`` would change ``identity`` (None
    fields mean unknown and never count)."""
    return any(v is not None and getattr(identity, k) != v for k, v in profile.items())


async def identities_by_external(db: AsyncSession, channel: str, external_ids: Iterable[str]) -> dict[str, Identity]:
    """``{external_id: Identity}`` of the stored ones, in one query per 500."""
    ext = list(set(external_ids))
    out: dict[str, Identity] = {}
    for i in range(0, len(ext), 500):
        res = await db.execute(
            select(Identity).where(Identity.channel == channel, Identity.external_id.in_(ext[i:i + 500]))
        )
        out.update({x.external_id: x for x in res.scalars().all()})
    return out


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


# ── Account removal ──────────────────────────────────────────────────────────

# User-entered CRM data that keeps a person even without identities.
_PERSON_DATA_COLUMNS = (
    "display_name", "twitter_handle", "linkedin_url", "notes", "is_important", "relationship_status_manual",
)


def _identity_references() -> list:
    """Every column that points at an identity as evidence it is still in use
    (a conversation peer, a sender, a recipient, an attendee, an account
    owner, a Shield record…), derived from the schema so new references
    count automatically. Contact points are the identity's own data."""

    refs = []
    for table in Base.metadata.sorted_tables:
        if table.name in ("identities", "contact_points"):
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name == "identities":
                refs.append(fk.parent)
    return refs


async def collect_orphans_after_account_removal(db: AsyncSession, channel: str, account_id: int) -> dict:
    """Call after the account row of ``channel`` was deleted (its
    conversations, messages and events cascaded away). Deletes the
    identities on that channel nothing references any more (another account
    on the channel may still use them), then the people those identities
    belonged to who were left with no identity and no user-entered data
    (CRM fields, notes, positions, merge history). Also drops the account's
    entries from account-keyed prefs. Commits. Returns
    ``{"identities": n, "people": n, "prefs": [pref keys changed]}``."""

    in_use = [select(col).where(col.isnot(None)) for col in _identity_references()]
    orphan_q = select(Identity.id, Identity.person_id).where(Identity.channel == channel)
    for q in in_use:
        orphan_q = orphan_q.where(Identity.id.notin_(q))
    orphans = (await db.execute(orphan_q)).all()
    identity_ids = [iid for iid, _ in orphans]
    person_ids = list({pid for _, pid in orphans})
    for i in range(0, len(identity_ids), 500):
        await db.execute(delete(Identity).where(Identity.id.in_(identity_ids[i:i + 500])))

    people_deleted = 0
    if person_ids:
        has_data = [
            (getattr(Person, c) == True) if c in ("is_important", "relationship_status_manual")  # noqa: E712
            else getattr(Person, c).isnot(None)
            for c in _PERSON_DATA_COLUMNS
        ]
        keep = union(
            select(Person.id).where(Person.id.in_(person_ids), or_(*has_data)),
            select(Identity.person_id).where(Identity.person_id.in_(person_ids)),
            select(PersonNote.person_id).where(PersonNote.person_id.in_(person_ids)),
            select(PersonPosition.person_id).where(PersonPosition.person_id.in_(person_ids)),
            select(PersonMerge.into_person_id).where(PersonMerge.into_person_id.in_(person_ids)),
        )
        kept = set((await db.execute(keep)).scalars().all())
        doomed = [pid for pid in person_ids if pid not in kept]
        for i in range(0, len(doomed), 500):
            await db.execute(delete(Person).where(Person.id.in_(doomed[i:i + 500])))
        people_deleted = len(doomed)
    await db.commit()
    changed_prefs = prefs.forget_account(account_id)
    return {"identities": len(identity_ids), "people": people_deleted, "prefs": changed_prefs}
