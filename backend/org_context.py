"""Shared helper to resolve organizational context for LLM prompts.

Given sender names from a conversation, identifies which ones are colleagues of the
user (active position in one of the user's organizations) and returns a
concise context string for injection into LLM prompts.

``OrgDirectory`` loads the colleague list once (filtered in SQL to the user's
orgs) so a briefing run with many conversations does one query, not one per conversation.
"""

import logging
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import prefs
from backend.db import crud
from backend.db.models import Organization, Person, PersonPosition

logger = logging.getLogger(__name__)


def get_user_organizations() -> list[str]:
    """Return the list of org names the user belongs to (from prefs)."""
    orgs = prefs.load_prefs().get("user_organizations", [])
    return [o for o in orgs if isinstance(o, str) and o.strip()]


def get_user_organization_ids() -> Optional[list[int]]:
    """Org ids the user belongs to (prefs ``user_organization_ids``, kept by
    backend.user_orgs), or None when only legacy names are stored."""
    ids = prefs.load_prefs().get("user_organization_ids")
    if not isinstance(ids, list):
        return None
    return [i for i in ids if isinstance(i, int) and not isinstance(i, bool)]


async def resolve_user_org_ids(
    db: AsyncSession,
    names: Optional[list[str]] = None,
    ids: Optional[list[int]] = None,
) -> list[int]:
    """The user's organizations as existing organization ids: stored ids that
    still exist, plus names resolved through the normalized name key
    lower(trim(name)) (legacy prefs / name-only entries). Renames can't break
    the id part; unknown ids and unresolvable names are dropped."""
    found: list[int] = []
    if ids:
        found += (await db.execute(
            select(Organization.id).where(Organization.id.in_(ids))
        )).scalars().all()
    names = [n.strip() for n in (names or []) if n and n.strip()]
    if names:
        found += (await db.execute(
            select(Organization.id).where(
                func.lower(func.trim(Organization.name)).in_([n.lower() for n in names])
            )
        )).scalars().all()
    return list(dict.fromkeys(found))


class OrgDirectory:
    """Colleagues of the user, keyed by every lowercased name they go by
    (person name, each identity's display name, first name and full name).

    A colleague is a person with an ACTIVE position ATTACHED to one of the
    user's organizations (organization_id set); detached positions — org
    deleted with mode=detach — never count."""

    def __init__(self, rows: list[tuple]):
        """``rows``: (names, title, org) per colleague position."""
        # name_lower -> list of (title, org)
        self._by_name: dict[str, list[tuple[str, str]]] = {}
        for names, title, org in rows:
            entry = (title or "", org)
            for key in {(n or "").strip().lower() for n in names}:
                if key:
                    self._by_name.setdefault(key, []).append(entry)

    @classmethod
    async def load(
        cls,
        db: AsyncSession,
        user_orgs: Optional[list[str]] = None,
        user_org_ids: Optional[list[int]] = None,
    ) -> "OrgDirectory":
        """With neither argument, the user's orgs are read from prefs."""
        if user_orgs is None and user_org_ids is None:
            # Prefs path: backend.user_orgs owns the representation (and
            # migrates legacy names to ids as it reads).
            from backend.user_orgs import resolve_user_orgs
            org_ids = [e["id"] for e in await resolve_user_orgs(db) if e.get("id") is not None]
        else:
            org_ids = await resolve_user_org_ids(db, user_orgs, user_org_ids)
        if not org_ids:
            return cls([])
        positions = (await db.execute(
            select(PersonPosition.person_id, Person.name, PersonPosition.title, Organization.name)
            .join(Person, Person.id == PersonPosition.person_id)
            .join(Organization, Organization.id == PersonPosition.organization_id)
            .where(
                PersonPosition.is_active == True,  # noqa: E712
                PersonPosition.organization_id.in_(org_ids),
            )
        )).all()
        identities = await crud.identities_for_people(db, {row[0] for row in positions})
        rows = []
        for person_id, name, title, org in positions:
            names = [name]
            for ident in identities.get(person_id, []):
                first = (ident.first_name or "").strip()
                names += [ident.display_name, first, f"{first} {(ident.last_name or '').strip()}"]
            rows.append((names, title, org))
        return cls(rows)

    def for_senders(self, sender_names: list[str]) -> Optional[str]:
        """'Your colleagues in this conversation: Alice (Engineer at Zircuit), Bob (at Zircuit).' or None."""
        parts: list[str] = []
        for name in dict.fromkeys(n for n in sender_names if n):
            for title, org in self._by_name.get(name.lower(), []):
                parts.append(f"{name} ({title} at {org})" if title else f"{name} (at {org})")
        if not parts:
            return None
        return "Your colleagues in this conversation: " + ", ".join(parts) + "."

    def for_person(self, person_name: str) -> Optional[str]:
        matches = self._by_name.get((person_name or "").lower())
        if not matches:
            return None
        title, org = matches[0]
        if title:
            return f"This person is your colleague: {person_name} ({title} at {org})."
        return f"This person is your colleague at {org}."


async def get_org_context_for_senders(
    db: AsyncSession,
    sender_names: list[str],
) -> Optional[str]:
    """One-off lookup; prefer ``OrgDirectory.load`` once per batch."""
    if not sender_names:
        return None
    return (await OrgDirectory.load(db)).for_senders(sender_names)


async def get_org_context_for_person(
    db: AsyncSession,
    person_name: str,
) -> Optional[str]:
    """One-off lookup for a direct conversation's person; prefer ``OrgDirectory.load`` once per batch."""
    return (await OrgDirectory.load(db)).for_person(person_name)


def get_user_orgs_system_context(user_orgs: Optional[list[str]] = None) -> Optional[str]:
    """Return a system-level context string about the user's organizations.

    Used for the intelligence assistant where we don't have specific senders yet.
    """
    orgs = user_orgs if user_orgs is not None else get_user_organizations()
    if not orgs:
        return None
    org_list = ", ".join(orgs)
    return (
        f"The user belongs to these organizations: {org_list}. "
        "When you encounter people from these organizations in conversations or messages, "
        "note their affiliation — they are the user's colleagues."
    )
