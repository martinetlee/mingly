"""The user's own organizations ("I work at ..."), stored in prefs.json.

Organizations are referenced by id so renames and merges can't break
colleague context:

- ``user_organization_ids`` (list[int]) is the source of truth.
- ``user_organizations`` (list[str]) is kept in sync as display names for
  legacy readers, covering every entry in the user's order (it is what
  carries the order). It also holds name-only entries that don't match any
  org yet (the name fallback).

Legacy prefs have only names. ``resolve_user_orgs`` migrates them on read: any
name that now matches an org (case/whitespace-insensitively) gains its id.
The org mutation hooks (rename / merge / delete) rewrite both keys in one
``update_prefs`` cycle; call them right after the DB commit.
"""

from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.prefs import load_prefs, update_prefs
from backend.db.crud import name_key
from backend.db.models import Organization

IDS_KEY = "user_organization_ids"
NAMES_KEY = "user_organizations"


def _norm(name: str) -> str:
    return name.strip().lower()


def _clean_ids(raw) -> list[int]:
    out: list[int] = []
    for v in raw if isinstance(raw, list) else []:
        if isinstance(v, int) and not isinstance(v, bool) and v not in out:
            out.append(v)
    return out


def _clean_names(raw) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in raw if isinstance(raw, list) else []:
        if isinstance(v, str) and v.strip() and _norm(v) not in seen:
            seen.add(_norm(v))
            out.append(v.strip())
    return out


def read_user_org_prefs() -> tuple[list[int], list[str]]:
    """(ids, names) exactly as stored, sanitized. No DB access."""
    prefs = load_prefs()
    return _clean_ids(prefs.get(IDS_KEY)), _clean_names(prefs.get(NAMES_KEY))


async def _org_by_name(db: AsyncSession, name: str):

    return (await db.execute(
        select(Organization).where(name_key(Organization.name) == name_key(name)).limit(1)
    )).scalar_one_or_none()


async def _orgs_by_id(db: AsyncSession, ids: list[int]) -> dict[int, str]:

    if not ids:
        return {}
    return {o.id: o.name for o in (await db.execute(
        select(Organization).where(Organization.id.in_(ids))
    )).scalars().all()}


def _entries_to_lists(entries: list[dict]) -> tuple[list[int], list[str]]:
    ids = [e["id"] for e in entries if e["id"] is not None]
    return ids, [e["name"] for e in entries]


async def resolve_user_orgs(db: AsyncSession) -> list[dict]:
    """The user's orgs as [{id: int|None, name}] in the user's order (the
    order of the names mirror). Names resolve to the stored id whose org has
    that name, else to any org with that name (legacy migration, in place),
    else stay name-only (id None). Stored ids with no name entry (legacy or
    corrupt data) are appended. Dead ids are dropped. Persists the result."""
    ids, names = read_user_org_prefs()
    by_id = await _orgs_by_id(db, ids)
    stored_by_name = {_norm(name): oid for oid, name in by_id.items() if oid in ids}
    entries: list[dict] = []
    used: set[int] = set()
    for name in names:
        oid = stored_by_name.get(_norm(name))
        if oid is None:
            org = await _org_by_name(db, name)
            if org is not None:
                oid, by_id[org.id] = org.id, org.name
        if oid is not None:
            if oid in used:
                continue
            used.add(oid)
            entries.append({"id": oid, "name": by_id[oid]})
        else:
            entries.append({"id": None, "name": name})
    for oid in ids:
        if oid in by_id and oid not in used:
            used.add(oid)
            entries.append({"id": oid, "name": by_id[oid]})
    _store(*_entries_to_lists(entries))
    return entries


async def set_user_orgs(db: AsyncSession, entries: Iterable) -> list[dict]:
    """Replace the user's orgs, keeping the given order. Each entry is an org
    id (int; unknown ids are dropped) or a name (str; resolved to the matching
    org's id in place, else kept name-only). Duplicates keep their first slot."""
    entries = list(entries)
    given_ids = [e for e in entries if isinstance(e, int) and not isinstance(e, bool)]
    by_id = await _orgs_by_id(db, given_ids)
    out: list[dict] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for e in entries:
        if isinstance(e, bool):
            continue
        if isinstance(e, int):
            oid, name = (e, by_id.get(e)) if e in by_id else (None, None)
        elif isinstance(e, str) and e.strip():
            org = await _org_by_name(db, e)
            oid, name = (org.id, org.name) if org is not None else (None, e.strip())
        else:
            continue
        if name is None:
            continue
        if oid is not None:
            if oid in seen_ids:
                continue
            seen_ids.add(oid)
        elif _norm(name) in seen_names:
            continue
        seen_names.add(_norm(name))
        out.append({"id": oid, "name": name})
    _store(*_entries_to_lists(out))
    return out


def _store(ids: list[int], names: list[str]) -> None:
    def mutate(prefs: dict) -> None:
        prefs[IDS_KEY] = list(ids)
        prefs[NAMES_KEY] = list(names)
    current_ids, current_names = read_user_org_prefs()
    if current_ids != ids or current_names != names:
        update_prefs(mutate)


def _rewrite(mutate_lists) -> None:
    def mutate(prefs: dict) -> None:
        ids = _clean_ids(prefs.get(IDS_KEY))
        names = _clean_names(prefs.get(NAMES_KEY))
        ids, names = mutate_lists(ids, names)
        prefs[IDS_KEY] = _clean_ids(ids)
        prefs[NAMES_KEY] = _clean_names(names)
    update_prefs(mutate)


def _replace_name(names: list[str], old: Optional[str], new: Optional[str]) -> list[str]:
    out = []
    for n in names:
        if old is not None and _norm(n) == _norm(old):
            if new is not None:
                out.append(new)
        else:
            out.append(n)
    return out


def _mentions(org_id: int, name: str) -> bool:
    ids, names = read_user_org_prefs()
    return org_id in ids or _norm(name) in {_norm(n) for n in names}


def on_org_renamed(org_id: int, old_name: str, new_name: str) -> None:
    """The org id is unchanged; its display name follows (or a legacy name
    entry is upgraded to the id)."""
    if not _mentions(org_id, old_name):
        return

    def mutate(ids, names):
        if org_id not in ids:
            ids = ids + [org_id]
        names = _replace_name(names, old_name, new_name)
        if _norm(new_name) not in {_norm(n) for n in names}:
            names.append(new_name)
        return ids, names
    _rewrite(mutate)


def on_org_merged(source_id: int, source_name: str, target_id: int, target_name: str) -> None:
    """The source org is gone: membership moves to the target."""
    if not _mentions(source_id, source_name):
        return

    def mutate(ids, names):
        ids = [target_id if i == source_id else i for i in ids]
        if target_id not in ids:
            ids.append(target_id)
        names = _replace_name(names, source_name, target_name)
        if _norm(target_name) not in {_norm(n) for n in names}:
            names.append(target_name)
        return ids, names
    _rewrite(mutate)


def on_org_deleted(org_id: int, name: str) -> None:
    """The org no longer exists (positions detached or deleted): drop it."""
    if not _mentions(org_id, name):
        return
    _rewrite(lambda ids, names: ([i for i in ids if i != org_id], _replace_name(names, name, None)))
