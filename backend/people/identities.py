"""Recording the people a channel tells us about.

``upsert_identity`` is how every connector stores a handle it sees: the
identity row (``crud.store_identity``) plus the cross-channel linking rules
(``linking``) that may merge it into an existing person.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.models import Identity
from backend.people import linking


async def upsert_identity(
    db: AsyncSession, *, channel: str, external_id: str, commit: bool = True, **profile
) -> Identity:
    """Create or update the identity ``(channel, external_id)``.

    Profile fields (see ``crud.PROFILE_FIELDS``) that are not None take the
    new value. A new identity gets a new person (marked ``is_self`` when the
    identity is). When a phone / email contact point is added, the person may
    be merged into an existing one with the same verified handle; a new
    username may yield a merge suggestion. Returns the stored identity.
    """
    identity, change = await crud.store_identity(db, channel=channel, external_id=external_id, **profile)
    if change.any:
        if (change.created or "phone" in change.changed) and await crud.sync_contact_points(db, identity):
            await db.flush()
            await linking.on_contact_points_added(db, identity)
        elif change.new_username:
            await linking.on_username_changed(db, identity)
    if commit:
        await db.commit()
    return identity
