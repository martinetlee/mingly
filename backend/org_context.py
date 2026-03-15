"""Shared helper to resolve organizational context for LLM prompts.

Given a list of sender names from a chat, identifies which ones are
colleagues of the user (belong to the same organizations) and returns
a concise context string for injection into LLM prompts.
"""

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.settings import _load_prefs
from backend.db.models import Contact, ContactPosition

logger = logging.getLogger(__name__)


def get_user_organizations() -> list[str]:
    """Return the list of org names the user belongs to (from prefs)."""
    prefs = _load_prefs()
    return prefs.get("user_organizations", [])


async def get_org_context_for_senders(
    db: AsyncSession,
    sender_names: list[str],
) -> Optional[str]:
    """Look up which senders are colleagues (share an org with the user).

    Returns a string like:
        "Your colleagues in this chat: Alice (Engineer at Zircuit), Bob (PM at Zircuit)."
    or None if no matches / user has no orgs configured.
    """
    user_orgs = get_user_organizations()
    if not user_orgs or not sender_names:
        return None

    # Normalize for case-insensitive matching
    user_orgs_lower = {o.lower() for o in user_orgs}

    # Find contacts whose first_name (or first_name + last_name) matches sender names
    # and who have an active position in one of the user's orgs
    q = (
        select(
            Contact.first_name,
            Contact.last_name,
            ContactPosition.title,
            ContactPosition.organization,
        )
        .join(ContactPosition, ContactPosition.telegram_id == Contact.telegram_id)
        .where(
            ContactPosition.is_active == True,  # noqa: E712
        )
    )
    rows = (await db.execute(q)).all()

    # Build lookup: sender_name -> list of (title, org)
    matches: dict[str, list[tuple[str, str]]] = {}
    sender_names_lower = {s.lower(): s for s in sender_names}

    for first_name, last_name, title, org in rows:
        if org.lower() not in user_orgs_lower:
            continue
        # Try matching against sender names
        full_name = f"{first_name} {last_name}".strip() if last_name else (first_name or "")
        for candidate_lower, candidate_original in sender_names_lower.items():
            if (
                (first_name and first_name.lower() == candidate_lower)
                or (full_name and full_name.lower() == candidate_lower)
            ):
                matches.setdefault(candidate_original, []).append((title or "", org))

    if not matches:
        return None

    parts = []
    for name, positions in matches.items():
        for title, org in positions:
            if title:
                parts.append(f"{name} ({title} at {org})")
            else:
                parts.append(f"{name} (at {org})")

    return "Your colleagues in this chat: " + ", ".join(parts) + "."


async def get_org_context_for_contact(
    db: AsyncSession,
    contact_name: str,
) -> Optional[str]:
    """Get org context for a single contact (used in 1-on-1 briefings).

    Returns a string like:
        "This person is your colleague: Alice (Engineer at Zircuit)."
    or None if no match.
    """
    user_orgs = get_user_organizations()
    if not user_orgs:
        return None

    user_orgs_lower = {o.lower() for o in user_orgs}

    q = (
        select(
            Contact.first_name,
            Contact.last_name,
            ContactPosition.title,
            ContactPosition.organization,
        )
        .join(ContactPosition, ContactPosition.telegram_id == Contact.telegram_id)
        .where(ContactPosition.is_active == True)  # noqa: E712
    )
    rows = (await db.execute(q)).all()

    contact_lower = contact_name.lower()
    for first_name, last_name, title, org in rows:
        if org.lower() not in user_orgs_lower:
            continue
        full_name = f"{first_name} {last_name}".strip() if last_name else (first_name or "")
        if (
            (first_name and first_name.lower() == contact_lower)
            or (full_name and full_name.lower() == contact_lower)
        ):
            if title:
                return f"This person is your colleague: {contact_name} ({title} at {org})."
            else:
                return f"This person is your colleague at {org}."

    return None


def get_user_orgs_system_context() -> Optional[str]:
    """Return a system-level context string about the user's organizations.

    Used for intelligence chat where we don't have specific senders yet.
    """
    user_orgs = get_user_organizations()
    if not user_orgs:
        return None
    org_list = ", ".join(user_orgs)
    return (
        f"The user belongs to these organizations: {org_list}. "
        "When you encounter people from these organizations in chats or messages, "
        "note their affiliation — they are the user's colleagues."
    )
