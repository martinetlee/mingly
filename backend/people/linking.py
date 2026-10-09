"""Linking one human's identities across channels into one person.

- Automatic: when an identity gains a phone / email contact point
  (``on_contact_points_added``, called by ``crud.upsert_identity``) and exactly
  one other person owns an identity with the same normalized value, the two
  people are merged (``PersonMerge.automatic``). Several matches become
  suggestions instead.
- Suggestions (never merged automatically), in ``person_merge_suggestions``:
  a shared phone / email that wasn't merged, the same display name plus a
  shared group conversation, a username equal to an email's local part.
- ``merge`` / ``undo`` / ``detach``: the user's tools (``backend/api/people.py``).
  Undoing a merge or detaching an identity records a dismissed suggestion for
  the pair, so neither is redone automatically. A merge carries the removed
  person's suggestions (dismissed ones included) over to the survivor; a
  detach recomputes both people's open suggestions.

The user themself (``is_self``) is never merged with anyone else. Nothing here
commits; the API routes broadcast ``person_changed`` after committing, and the
automatic path (inside ``upsert_identity``) broadcasts once its caller commits.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import DateTime, and_, delete as sa_delete, event, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.models import (
    BriefingEntry, CatchUpItem, ContactPoint, Conversation, Identity, Message, Organization, Person,
    PersonMerge, PersonMergeSuggestion, PersonNote, PersonPosition,
)
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

# Suggestion reasons → score (how likely the pair is one human)
SAME_PHONE = "same_phone"
SAME_EMAIL = "same_email"
SAME_NAME_SHARED_GROUP = "same_name_shared_group"
USERNAME_MATCHES_EMAIL = "username_matches_email"
# Dismissed-pair markers: the user split these people apart
DETACHED = "detached"
UNMERGED = "unmerged"
SCORES = {SAME_PHONE: 0.9, SAME_EMAIL: 0.9, SAME_NAME_SHARED_GROUP: 0.6, USERNAME_MATCHES_EMAIL: 0.5}

# CRM fields a merge fills on the surviving person when empty there
FILL_FIELDS = ("display_name", "twitter_handle", "linkedin_url", "notes")
_MIN_HANDLE = 3  # shorter usernames / names are too common to suggest anything
_PENDING_KEY = "linking_person_changed"
_tasks: set = set()  # strong refs to broadcast tasks until they finish


class LinkingError(ValueError):
    """A merge / undo / detach that isn't allowed (message is user-facing)."""


# ── Automatic linking (called from crud.upsert_identity) ─────────────────────

async def on_contact_points_added(db: AsyncSession, identity: Identity) -> None:
    """``identity`` gained a phone / email. Exactly one other person with the
    same value: merge (the older person survives). Several: suggest each.
    Then refresh the surviving person's other suggestions."""
    points = (await db.execute(
        select(ContactPoint.kind, ContactPoint.value).where(ContactPoint.identity_id == identity.id)
    )).all()
    person = await db.get(Person, identity.person_id)
    if not points or person is None:
        return
    await db.refresh(person)
    matches = await _contact_point_matches(db, person, points)
    survivor = person.id
    if len(matches) == 1:
        other_id, kind = next(iter(matches.items()))
        pair = await crud.merge_suggestion_for_pair(db, person.id, other_id)
        if pair is None or pair.status != "dismissed":
            into_id, from_id = sorted((person.id, other_id))
            await merge(db, into_id, from_id, automatic=True)
            logger.info("Linked person %s into %s (same %s)", from_id, into_id, kind)
            survivor = into_id
            _notify_after_commit(db, (into_id, from_id))
    else:
        for other_id, kind in matches.items():
            await crud.add_merge_suggestion(db, person.id, other_id, _point_reason(kind), SCORES[_point_reason(kind)])
    if await suggest_for_person(db, survivor):
        _notify_after_commit(db, (survivor,))


async def on_username_changed(db: AsyncSession, identity: Identity) -> None:
    """A new / changed username: suggest people with a matching email."""
    person = await db.get(Person, identity.person_id)
    if person is None:
        return
    await db.refresh(person)
    found = {pid: USERNAME_MATCHES_EMAIL for pid in await _username_email_matches(db, [identity])}
    if await _record_suggestions(db, person, found):
        _notify_after_commit(db, (person.id,))


def _point_reason(kind: str) -> str:
    return SAME_PHONE if kind == "phone" else SAME_EMAIL


async def _contact_point_matches(db: AsyncSession, person: Person, points: Iterable[tuple]) -> dict[int, str]:
    """{other person id: contact point kind} of people (with the same is_self)
    owning one of ``points``."""
    cond = or_(*[and_(ContactPoint.kind == k, ContactPoint.value == v) for k, v in points])
    rows = (await db.execute(
        select(Identity.person_id, ContactPoint.kind)
        .join(Identity, Identity.id == ContactPoint.identity_id)
        .join(Person, Person.id == Identity.person_id)
        .where(cond, Identity.person_id != person.id, Person.is_self == bool(person.is_self))
    )).all()
    out: dict[int, str] = {}
    for pid, kind in rows:
        out.setdefault(pid, kind)
    return out


def _notify_after_commit(db: AsyncSession, person_ids: Iterable[int]) -> None:
    """Broadcast person_changed for ``person_ids`` once ``db`` commits (the
    automatic path runs inside a caller's transaction)."""
    pending = db.info.setdefault(_PENDING_KEY, set())
    pending.update(person_ids)
    if db.info.get(_PENDING_KEY + "_hooked"):
        return
    db.info[_PENDING_KEY + "_hooked"] = True
    sync_session = db.sync_session

    def _after_commit(_session):
        ids = sorted(db.info.pop(_PENDING_KEY, set()))
        if not ids:
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        for pid in ids:
            task = loop.create_task(ws_manager.broadcast({"type": "person_changed", "person_id": pid}))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)

    def _after_rollback(_session):
        db.info.pop(_PENDING_KEY, None)

    event.listen(sync_session, "after_commit", _after_commit)
    event.listen(sync_session, "after_rollback", _after_rollback)


# ── Suggestions ──────────────────────────────────────────────────────────────

def _name_key(ident: Identity) -> Optional[str]:
    name = ident.display_name or " ".join(x for x in (ident.first_name, ident.last_name) if x)
    key = " ".join(name.lower().split()) if name else ""
    return key if len(key) >= _MIN_HANDLE else None


async def _group_senders(db: AsyncSession, conversation_ids: Optional[list] = None) -> list[tuple]:
    """Distinct (conversation_id, Identity) of senders in group conversations."""
    q = (
        select(Message.conversation_id, Message.sender_identity_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.kind == "group", Message.sender_identity_id.isnot(None))
        .distinct()
    )
    if conversation_ids is not None:
        if not conversation_ids:
            return []
        q = q.where(Message.conversation_id.in_(conversation_ids))
    rows = (await db.execute(q)).all()
    idents = await crud.get_identities(db, [iid for _, iid in rows])
    return [(cid, idents[iid]) for cid, iid in rows if iid in idents]


def _same_name_pairs(senders: list[tuple]) -> set[tuple]:
    """Person pairs (lo, hi) whose identities share a name in one group."""
    by_conv_name: dict[tuple, set] = {}
    for cid, ident in senders:
        key = _name_key(ident)
        if key and not ident.is_bot and not ident.is_deleted:
            by_conv_name.setdefault((cid, key), set()).add(ident.person_id)
    pairs = set()
    for pids in by_conv_name.values():
        ordered = sorted(pids)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                pairs.add((a, b))
    return pairs


async def _same_name_matches(db: AsyncSession, idents: list[Identity]) -> set[int]:
    ids = [i.id for i in idents]
    if not ids:
        return set()
    conv_ids = list((await db.execute(
        select(Message.conversation_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.kind == "group", Message.sender_identity_id.in_(ids))
        .distinct()
    )).scalars().all())
    mine = {i.person_id for i in idents}
    out = set()
    for a, b in _same_name_pairs(await _group_senders(db, conv_ids)):
        if a in mine and b not in mine:
            out.add(b)
        elif b in mine and a not in mine:
            out.add(a)
    return out


async def _username_email_matches(db: AsyncSession, idents: list[Identity]) -> set[int]:
    """People with an email whose local part is one of ``idents``' usernames,
    or with a username equal to one of ``idents``' email local parts."""
    mine = {i.person_id for i in idents}
    out: set[int] = set()
    for ident in idents:
        u = (ident.username or "").strip().lower()
        if len(u) < _MIN_HANDLE:
            continue
        rows = (await db.execute(
            select(Identity.person_id)
            .join(ContactPoint, ContactPoint.identity_id == Identity.id)
            .where(ContactPoint.kind == "email", ContactPoint.value >= u + "@", ContactPoint.value < u + "@￿")
        )).scalars().all()
        out.update(rows)
    locals_ = list({v.split("@", 1)[0] for v in (await db.execute(
        select(ContactPoint.value).where(
            ContactPoint.kind == "email", ContactPoint.identity_id.in_([i.id for i in idents])
        )
    )).scalars().all() if len(v.split("@", 1)[0]) >= _MIN_HANDLE})
    if locals_:
        out.update((await db.execute(
            select(Identity.person_id).where(func.lower(Identity.username).in_(locals_))
        )).scalars().all())
    return out - mine


async def _record_suggestions(db: AsyncSession, person: Person, found: dict) -> int:
    """Add suggestions ``{other person id: reason}`` for ``person`` (skipping
    people whose is_self differs). Returns how many were created."""
    found = {pid: r for pid, r in found.items() if pid != person.id}
    if not found:
        return 0
    others = await crud.get_people(db, found)
    created = 0
    for pid, reason in found.items():
        other = others.get(pid)
        if other is None or bool(other.is_self) != bool(person.is_self):
            continue
        created += await crud.add_merge_suggestion(db, person.id, pid, reason, SCORES[reason])
    return created


async def suggest_for_person(db: AsyncSession, person_id: int) -> int:
    """Recompute the suggestions involving one person (cheap: their own
    identities only). Returns how many were created."""
    person = await db.get(Person, person_id)
    if person is None:
        return 0
    await db.refresh(person)
    idents = await crud.identities_for_person(db, person_id)
    found: dict[int, str] = {}
    for pid in await _username_email_matches(db, idents):
        found[pid] = USERNAME_MATCHES_EMAIL
    for pid in await _same_name_matches(db, idents):
        found[pid] = SAME_NAME_SHARED_GROUP
    points = (await db.execute(
        select(ContactPoint.kind, ContactPoint.value).where(ContactPoint.identity_id.in_([i.id for i in idents]))
    )).all() if idents else []
    if points:
        for pid, kind in (await _contact_point_matches(db, person, points)).items():
            found[pid] = _point_reason(kind)
    return await _record_suggestions(db, person, found)


async def refresh_all_suggestions(db: AsyncSession) -> int:
    """Recompute suggestions across everyone. Returns how many were created."""
    found: dict[tuple, str] = {}
    for pair in _same_name_pairs(await _group_senders(db)):
        found[pair] = SAME_NAME_SHARED_GROUP

    emails = (await db.execute(
        select(Identity.person_id, ContactPoint.value)
        .join(ContactPoint, ContactPoint.identity_id == Identity.id)
        .where(ContactPoint.kind == "email")
    )).all()
    by_local: dict[str, set] = {}
    for pid, value in emails:
        local = value.split("@", 1)[0]
        if len(local) >= _MIN_HANDLE:
            by_local.setdefault(local, set()).add(pid)
    for pid, username in (await db.execute(
        select(Identity.person_id, Identity.username).where(Identity.username.isnot(None))
    )).all():
        for other in by_local.get(username.strip().lower(), ()):
            if other != pid:
                found[tuple(sorted((pid, other)))] = USERNAME_MATCHES_EMAIL

    points = (await db.execute(
        select(Identity.person_id, ContactPoint.kind, ContactPoint.value)
        .join(ContactPoint, ContactPoint.identity_id == Identity.id)
    )).all()
    by_point: dict[tuple, set] = {}
    for pid, kind, value in points:
        by_point.setdefault((kind, value), set()).add(pid)
    for (kind, _), pids in by_point.items():
        ordered = sorted(pids)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                found[(a, b)] = _point_reason(kind)

    people = await crud.get_people(db, {pid for pair in found for pid in pair})
    created = 0
    for (a, b), reason in found.items():
        pa, pb = people.get(a), people.get(b)
        if pa is None or pb is None or bool(pa.is_self) != bool(pb.is_self):
            continue
        created += await crud.add_merge_suggestion(db, a, b, reason, SCORES[reason])
    return created


# ── Merge / undo / detach ────────────────────────────────────────────────────

def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _dt(raw: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(raw) if raw else None


_PERSON_COLUMNS = [c.key for c in Person.__table__.columns if c.key != "id"]
_DATETIME_COLUMNS = {c.key for c in Person.__table__.columns if isinstance(c.type, DateTime)}


def _person_fields(p: Person) -> dict:
    out = {}
    for key in _PERSON_COLUMNS:
        v = getattr(p, key)
        out[key] = _iso(v) if isinstance(v, datetime) else v
    return out


def _position_fields(p: PersonPosition) -> dict:
    return {
        "organization_id": p.organization_id, "organization": p.organization, "title": p.title,
        "is_active": bool(p.is_active), "ended_at": _iso(p.ended_at), "created_at": _iso(p.created_at),
    }


def _position_rank(p: PersonPosition) -> tuple:
    """Which of two positions at one organization a merge keeps: the active
    one, else the one that ended / started most recently."""
    return (bool(p.is_active), p.ended_at or datetime.min, p.created_at or datetime.min, p.id)


async def _ids(db: AsyncSession, column, person_id: int) -> list[int]:
    model = column.class_
    return list((await db.execute(select(model.id).where(column == person_id))).scalars().all())


async def _move(db: AsyncSession, model, ids: list, from_id: int, to_id: int) -> None:
    """Re-point rows ``ids`` of ``model`` still on ``from_id`` to ``to_id``."""
    if ids:
        await db.execute(
            update(model).where(model.id.in_(ids), model.person_id == from_id).values(person_id=to_id)
        )


async def _refresh_status(db: AsyncSession, person: Person) -> None:
    if person.relationship_status_manual:
        return
    status, last, last_out, last_in = await crud.compute_relationship_status(db, person)
    person.relationship_status = status
    person.last_interaction_at, person.last_outgoing_at, person.last_incoming_at = last, last_out, last_in


async def merge(db: AsyncSession, into_id: int, from_id: int, *, automatic: bool = False) -> PersonMerge:
    """Merge person ``from_id`` into ``into_id`` and delete it. Moves its
    identities, positions (one per organization: the active / most recent is
    kept), notes and catch-up / briefing links; fills ``into``'s empty CRM
    fields; keeps is_important if either had it. Records a PersonMerge
    snapshot for ``undo``. Raises LookupError / LinkingError. Doesn't commit."""
    if into_id == from_id:
        raise LinkingError("A person can't be merged with themselves")
    into = await db.get(Person, into_id)
    src = await db.get(Person, from_id)
    if into is None or src is None:
        raise LookupError("Person not found")
    await db.refresh(into)
    await db.refresh(src)
    if bool(into.is_self) != bool(src.is_self):
        raise LinkingError("Your own profile can't be merged with someone else")

    identities = await crud.identities_for_person(db, from_id)
    snapshot: dict = {
        "person": _person_fields(src),
        "name": src.name,
        "identities": [{"id": i.id, "channel": i.channel, "label": i.display_name or i.username or i.external_id}
                       for i in identities],
        "identity_ids": [i.id for i in identities],
    }

    # Positions: UNIQUE(person_id, organization_id) — keep one per organization
    into_positions = {
        p.organization_id: p for p in await crud.get_person_positions(db, into_id) if p.organization_id is not None
    }
    moved_positions, dropped = [], []
    for p in await crud.get_person_positions(db, from_id):
        other = into_positions.get(p.organization_id) if p.organization_id is not None else None
        if other is None:
            moved_positions.append(p.id)
        elif _position_rank(p) > _position_rank(other):
            dropped.append({"side": "into", **_position_fields(other)})
            await db.delete(other)
            moved_positions.append(p.id)
        else:
            dropped.append({"side": "from", **_position_fields(p)})
            await db.delete(p)
    await db.flush()
    snapshot["position_ids"] = moved_positions
    snapshot["dropped_positions"] = dropped
    snapshot["note_ids"] = await _ids(db, PersonNote.person_id, from_id)
    snapshot["catchup_item_ids"] = await _ids(db, CatchUpItem.person_id, from_id)
    snapshot["briefing_entry_ids"] = await _ids(db, BriefingEntry.person_id, from_id)
    # Earlier merges into the removed person now point at the survivor, so they stay undoable
    snapshot["merge_ids"] = await _ids(db, PersonMerge.into_person_id, from_id)

    await _move(db, Identity, snapshot["identity_ids"], from_id, into_id)
    await _move(db, PersonPosition, moved_positions, from_id, into_id)
    await _move(db, PersonNote, snapshot["note_ids"], from_id, into_id)
    await _move(db, CatchUpItem, snapshot["catchup_item_ids"], from_id, into_id)
    await _move(db, BriefingEntry, snapshot["briefing_entry_ids"], from_id, into_id)
    if snapshot["merge_ids"]:
        await db.execute(
            update(PersonMerge).where(PersonMerge.id.in_(snapshot["merge_ids"])).values(into_person_id=into_id)
        )

    filled = {}
    for key in FILL_FIELDS:
        if not getattr(into, key) and getattr(src, key):
            filled[key] = getattr(src, key)
            setattr(into, key, getattr(src, key))
    snapshot["filled"] = filled
    snapshot["set_important"] = bool(src.is_important) and not into.is_important
    if snapshot["set_important"]:
        into.is_important = True
    snapshot["set_status"] = None
    if src.relationship_status_manual and not into.relationship_status_manual:
        snapshot["set_status"] = {"previous": into.relationship_status, "status": src.relationship_status}
        into.relationship_status = src.relationship_status
        into.relationship_status_manual = True

    record = PersonMerge(
        into_person_id=into_id, from_person_id=from_id, snapshot_json=json.dumps(snapshot), automatic=automatic,
    )
    db.add(record)
    await db.flush()
    await _repoint_suggestions(db, from_id, into_id)
    await db.delete(src)
    await db.flush()
    await _refresh_status(db, into)
    await db.flush()
    return record


async def _repoint_suggestions(db: AsyncSession, from_id: int, into_id: int) -> None:
    """Carry ``from_id``'s suggestion rows over to ``into_id`` before it is
    deleted, so a pair the user dismissed (or split apart by undo / detach)
    stays dismissed: on a collision dismissed wins, else the higher score.
    The pair between the two merged people disappears."""
    rows = list((await db.execute(
        select(PersonMergeSuggestion).where(or_(
            PersonMergeSuggestion.person_a_id == from_id, PersonMergeSuggestion.person_b_id == from_id,
        ))
    )).scalars().all())
    for row in rows:
        other = row.person_b_id if row.person_a_id == from_id else row.person_a_id
        reason, score, status = row.reason, row.score, row.status
        await db.delete(row)
        await db.flush()
        if other != into_id:
            await crud.add_merge_suggestion(db, into_id, other, reason, score, status=status)


async def undo(db: AsyncSession, merge_id: int) -> Person:
    """Undo a merge: recreate the removed person (with a new id) and move
    back the rows the merge moved that are still on the survivor. Rows
    changed since (an identity detached, a note deleted) stay as they are.
    Raises LookupError / LinkingError. Doesn't commit."""
    record = await crud.get_person_merge(db, merge_id)
    if record is None:
        raise LookupError("Merge not found")
    if record.undone_at is not None:
        raise LinkingError("This merge was already undone")
    into = await db.get(Person, record.into_person_id)
    if into is None:
        raise LookupError("Person not found")
    await db.refresh(into)
    snap = json.loads(record.snapshot_json)

    fields = {k: (_dt(v) if k in _DATETIME_COLUMNS else v) for k, v in snap["person"].items() if k in _PERSON_COLUMNS}
    person = Person(**fields)
    db.add(person)
    await db.flush()
    into_id = into.id

    await _move(db, Identity, snap["identity_ids"], into_id, person.id)
    await _move(db, PersonPosition, snap["position_ids"], into_id, person.id)
    await _move(db, PersonNote, snap["note_ids"], into_id, person.id)
    await _move(db, CatchUpItem, snap["catchup_item_ids"], into_id, person.id)
    await _move(db, BriefingEntry, snap["briefing_entry_ids"], into_id, person.id)
    if snap["merge_ids"]:
        await db.execute(
            update(PersonMerge)
            .where(PersonMerge.id.in_(snap["merge_ids"]), PersonMerge.into_person_id == into_id)
            .values(into_person_id=person.id)
        )
    await db.flush()
    for d in snap["dropped_positions"]:
        owner = person.id if d["side"] == "from" else into_id
        org_id = d["organization_id"]
        if org_id is not None and await db.get(Organization, org_id) is None:
            org_id = None
        if org_id is not None and (await db.execute(select(PersonPosition.id).where(
            PersonPosition.person_id == owner, PersonPosition.organization_id == org_id,
        ))).first():
            continue
        db.add(PersonPosition(
            person_id=owner, organization_id=org_id, organization=d["organization"], title=d["title"],
            is_active=d["is_active"], ended_at=_dt(d["ended_at"]), created_at=_dt(d["created_at"]) or datetime.utcnow(),
        ))

    for key, value in snap["filled"].items():
        if getattr(into, key) == value:
            setattr(into, key, None)
    if snap["set_important"]:
        into.is_important = False
    status = snap.get("set_status")
    if status and into.relationship_status_manual and into.relationship_status == status["status"]:
        into.relationship_status = status["previous"]
        into.relationship_status_manual = False

    record.undone_at = datetime.utcnow()
    await crud.add_merge_suggestion(db, into_id, person.id, UNMERGED, 0.0, status="dismissed")
    await db.flush()
    await _refresh_status(db, into)
    await _refresh_status(db, person)
    await db.flush()
    return person


async def detach(db: AsyncSession, identity_id: int, person_id: Optional[int] = None) -> Person:
    """Move one identity (with the catch-up items of conversations it is the
    peer of) to a new person. Raises LookupError / LinkingError. Doesn't commit."""
    ident = await crud.get_identity(db, identity_id)
    if ident is None or (person_id is not None and ident.person_id != person_id):
        raise LookupError("Identity not found")
    old = await db.get(Person, ident.person_id)
    await db.refresh(old)
    if len(await crud.identity_ids_for_person(db, old.id)) < 2:
        raise LinkingError("This is the person's only linked account")
    person = Person(is_self=bool(ident.is_self))
    db.add(person)
    await db.flush()
    await db.execute(update(Identity).where(Identity.id == ident.id).values(person_id=person.id))
    peer_convs = select(Conversation.id).where(Conversation.peer_identity_id == ident.id)
    await db.execute(
        update(CatchUpItem)
        .where(CatchUpItem.person_id == old.id, CatchUpItem.conversation_id.in_(peer_convs))
        .values(person_id=person.id)
    )
    # The old person's open suggestions may rest on the identity that left:
    # recompute them for both sides (dismissed pairs are kept).
    await db.execute(sa_delete(PersonMergeSuggestion).where(
        PersonMergeSuggestion.status == "pending",
        or_(PersonMergeSuggestion.person_a_id == old.id, PersonMergeSuggestion.person_b_id == old.id),
    ))
    await crud.add_merge_suggestion(db, old.id, person.id, DETACHED, 0.0, status="dismissed")
    await db.flush()
    await suggest_for_person(db, old.id)
    await suggest_for_person(db, person.id)
    await _refresh_status(db, old)
    await _refresh_status(db, person)
    await db.flush()
    return person
