"""Shared helpers for classifying and naming Telethon entities.

Every place that turns a Telethon User/Chat/Channel into a chat row or a
display name goes through these, so the rules (supergroup vs channel,
name fallbacks, deactivated groups) are defined once.
"""

from typing import Optional

from telethon.tl.types import Channel, User


def chat_type(entity) -> str:
    """'private' for users, 'channel' for broadcast channels, else 'group'.

    Supergroups are Channel objects with ``megagroup=True``; checking
    ``hasattr(entity, 'broadcast')`` is wrong because every Channel has the
    attribute.
    """
    if isinstance(entity, User):
        return "private"
    if isinstance(entity, Channel):
        return "group" if getattr(entity, "megagroup", False) else "channel"
    return "group"


def user_full_name(entity) -> str:
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    return f"{first} {last}".strip()


def display_name(entity, fallback_id: Optional[int] = None, dialog_name: Optional[str] = None) -> str:
    """Best human-readable name: dialog name, title, full name, username, id."""
    fid = str(fallback_id) if fallback_id is not None else ""
    if dialog_name and dialog_name != fid:
        return dialog_name
    if entity is not None:
        name = getattr(entity, "title", None) or user_full_name(entity)
        if name:
            return name
        username = getattr(entity, "username", None)
        if username:
            return username
        if fallback_id is None:
            return str(getattr(entity, "id", ""))
    return fid


def sender_name(sender) -> Optional[str]:
    if sender is None:
        return None
    if isinstance(sender, User):
        return user_full_name(sender) or getattr(sender, "username", None) or None
    return getattr(sender, "title", None) or str(sender.id)


def is_deactivated(entity) -> bool:
    """True for basic groups Telegram has deactivated (migrated to a supergroup)."""
    return bool(getattr(entity, "deactivated", False)) or getattr(entity, "migrated_to", None) is not None


def is_forum(entity) -> bool:
    return bool(getattr(entity, "forum", False))
