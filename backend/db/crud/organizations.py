"""Organizations and the people's positions at them.

``person_positions.organization_id`` is the source of truth for membership;
``organization`` is the display name (mirrors the org while attached, frozen
once detached). Names match case/whitespace-insensitively (``name_key``).
"""

from typing import Optional

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.crud.text import contains_ci, name_key
from backend.db.models import Organization, PersonPosition
from backend.db.results import affected_rows

__all__ = [
    "find_organization_by_name",
    "attach_detached_positions",
    "get_or_create_organization",
    "merge_organizations",
    "rename_organization",
    "delete_organization",
    "list_organizations_with_counts",
    "detached_position_names",
]


async def find_organization_by_name(db: AsyncSession, name: str) -> Optional[Organization]:
    return (await db.execute(
        select(Organization).where(name_key(Organization.name) == name_key(name.strip()))
        .order_by(Organization.id).limit(1)
    )).scalar_one_or_none()


async def attach_detached_positions(db: AsyncSession, org: Organization) -> int:
    """Re-attach detached positions carrying ``org``'s name (at most one per
    person; the rest stay detached history). Flushes, doesn't commit."""
    candidates = list((await db.execute(
        select(PersonPosition).where(
            PersonPosition.organization_id.is_(None),
            name_key(PersonPosition.organization) == name_key(org.name),
        ).order_by(PersonPosition.is_active.desc(), PersonPosition.id)
    )).scalars().all())
    if not candidates:
        return 0
    taken = set((await db.execute(
        select(PersonPosition.person_id).where(PersonPosition.organization_id == org.id)
    )).scalars().all())
    attached = 0
    for p in candidates:
        if p.person_id in taken:
            continue
        taken.add(p.person_id)
        p.organization_id = org.id
        p.organization = org.name
        attached += 1
    await db.flush()
    return attached


async def get_or_create_organization(db: AsyncSession, name: str) -> tuple[Organization, bool]:
    """(org, created). Matches case/whitespace-insensitively; a new org adopts
    detached positions with its name. Flushes, doesn't commit. Raises
    ValueError on an empty name."""
    name = name.strip()
    if not name:
        raise ValueError("Organization name cannot be empty")
    org = await find_organization_by_name(db, name)
    if org is not None:
        return org, False
    org = Organization(name=name)
    db.add(org)
    await db.flush()
    await attach_detached_positions(db, org)
    return org, True


def _position_rank(p: PersonPosition) -> tuple:
    """Lower sorts first = the row to keep when two positions collide."""
    return (not p.is_active, not (p.title or "").strip(), p.id)


async def merge_organizations(db: AsyncSession, source: Organization, target: Organization) -> dict:
    """Fold ``source`` into ``target`` and delete ``source``. A person with
    positions at both keeps one (current over former, then titled, then
    oldest; a missing title is filled from the other). Flushes, doesn't commit."""
    if source.id == target.id:
        return {"moved_positions": 0, "merged_duplicate_positions": 0}
    target_by_person = {
        p.person_id: p for p in (await db.execute(
            select(PersonPosition).where(PersonPosition.organization_id == target.id)
        )).scalars().all()
    }
    source_positions = list((await db.execute(
        select(PersonPosition).where(PersonPosition.organization_id == source.id)
    )).scalars().all())
    moved = merged = 0
    for p in source_positions:
        other = target_by_person.get(p.person_id)
        if other is None:
            p.organization_id = target.id
            p.organization = target.name
            moved += 1
            continue
        keep, drop = (p, other) if _position_rank(p) < _position_rank(other) else (other, p)
        if not (keep.title or "").strip() and (drop.title or "").strip():
            keep.title = drop.title
        await db.delete(drop)
        await db.flush()  # free the (person_id, organization_id) slot first
        keep.organization_id = target.id
        keep.organization = target.name
        merged += 1
    if not (target.context or "").strip() and (source.context or "").strip():
        target.context = source.context
    await db.flush()
    await db.delete(source)
    await db.flush()
    return {"moved_positions": moved, "merged_duplicate_positions": merged}


async def rename_organization(db: AsyncSession, org: Organization, new_name: str) -> None:
    """Rename in place (caller has ruled out a clash with another org).
    Attached positions follow; detached history keeps its old name."""
    org.name = new_name
    await db.execute(
        update(PersonPosition).where(PersonPosition.organization_id == org.id).values(organization=new_name)
    )
    await db.flush()


async def delete_organization(db: AsyncSession, org: Organization, mode: str = "detach") -> dict:
    """mode='detach' keeps members' positions as history (organization_id NULL,
    name kept); mode='delete' removes them. Flushes, doesn't commit."""
    if mode not in ("detach", "delete"):
        raise ValueError("mode must be 'detach' or 'delete'")
    if mode == "detach":
        res = await db.execute(
            update(PersonPosition).where(PersonPosition.organization_id == org.id).values(organization_id=None)
        )
        counts = {"positions_detached": affected_rows(res), "positions_deleted": 0}
    else:
        res = await db.execute(delete(PersonPosition).where(PersonPosition.organization_id == org.id))
        counts = {"positions_detached": 0, "positions_deleted": affected_rows(res)}
    await db.delete(org)
    await db.flush()
    return counts


async def list_organizations_with_counts(db: AsyncSession, search: str = "") -> list[tuple]:
    """``(Organization, active_member_count, former_member_count)`` by name."""
    counts = (
        select(
            PersonPosition.organization_id.label("org_id"),
            func.sum(case((PersonPosition.is_active.is_(True), 1), else_=0)).label("active"),
            func.sum(case((PersonPosition.is_active.is_(True), 0), else_=1)).label("former"),
        )
        .where(PersonPosition.organization_id.isnot(None))
        .group_by(PersonPosition.organization_id)
        .subquery()
    )
    q = select(Organization, counts.c.active, counts.c.former).outerjoin(counts, counts.c.org_id == Organization.id)
    if search:
        q = q.where(contains_ci(Organization.name, search))
    return [
        (org, int(active or 0), int(former or 0))
        for org, active, former in (await db.execute(q.order_by(Organization.name))).all()
    ]


async def detached_position_names(db: AsyncSession) -> list[str]:
    """One display name per distinct (normalized) org name among detached,
    non-blank positions."""
    keyed = name_key(PersonPosition.organization)
    return [
        row[0] for row in (await db.execute(
            select(func.min(func.trim(PersonPosition.organization)))
            .where(PersonPosition.organization_id.is_(None), keyed != "")
            .group_by(keyed)
        )).all()
    ]
