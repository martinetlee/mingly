"""Optional connector roles: what a channel can do beyond the base contract.

``Connector`` covers what every channel has (lifecycle, sign-in, send, react,
read receipts, attachments). The groups of operations only some channels
support are separate interfaces a connector also inherits, so a route asks
for exactly the role it needs (``api.lookups.connector_with``) and answers
409 ``unsupported_channel`` when the conversation's connector doesn't have it.

All arguments are stored rows (internal ids); a connector translates them to
its channel's own ids. Errors: ``backend.errors.DomainError`` subclasses
(unavailable / rate limited / not found) pass through to the client; any
other exception is the channel refusing the operation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional

from backend.connectors.base import SyncResult


class MessageActions(ABC):
    """Changing messages that were already sent."""

    @abstractmethod
    async def edit_message(self, conversation: Any, message: Any, text: str) -> int:
        """Edit an outgoing message; returns the stored message id."""

    @abstractmethod
    async def delete_messages(self, conversation: Any, messages: list, *, revoke: bool) -> None:
        """Delete on the channel (for everyone when ``revoke``). The caller
        removes the stored rows."""

    @abstractmethod
    async def forward_messages(self, source: Any, messages: list, target: Any) -> None:
        """Forward ``messages`` of ``source`` into ``target`` (same account)."""

    @abstractmethod
    async def pin_message(self, conversation: Any, message: Any, *, pinned: bool, notify: bool) -> None:
        """Pin / unpin a message for everyone in the conversation."""


class DialogActions(ABC):
    """Mirroring the user's own conversation-list state to the channel.
    Best-effort: the local state is the source of truth, so these return
    quietly when the channel is unreachable."""

    @abstractmethod
    async def set_dialog_pinned(self, conversation: Any, pinned: bool) -> None: ...

    @abstractmethod
    async def set_dialog_archived(self, conversation: Any, archived: bool) -> None: ...

    @abstractmethod
    async def set_dialog_muted(self, conversation: Any, muted: bool, until: Optional[datetime]) -> None:
        """``until`` is naive UTC; None while muted = indefinitely."""


class GroupMembers(ABC):
    """Who is in a conversation and what they're doing."""

    @abstractmethod
    async def list_members(self, conversation: Any, limit: int) -> list[dict]:
        """Members (each recorded as an identity), creator / admins first:
        ``{identity_id, person_id, name, username, is_bot, role}``."""

    @abstractmethod
    async def presence(self, conversation: Any) -> dict:
        """Online / last-seen of the other party of a direct conversation:
        ``{"status", …}`` (``{"status": "unknown"}`` when not known)."""

    @abstractmethod
    async def send_typing(self, conversation: Any) -> None: ...


class ForumTopics(ABC):
    """Topics of a forum-style group."""

    @abstractmethod
    async def list_topics(self, conversation: Any) -> list[dict]:
        """``{id, title, icon_color, icon_emoji_id, date, is_closed, is_pinned}``."""

    @abstractmethod
    async def create_topic(self, conversation: Any, title: str) -> Optional[int]:
        """Returns the new topic's id when the channel reports it."""

    @abstractmethod
    async def set_topic_closed(self, conversation: Any, topic_id: int, closed: bool) -> None: ...


class GroupAdmin(ABC):
    """Administering a group or broadcast channel the user manages."""

    @abstractmethod
    async def promote(self, conversation: Any, identity_id: int, permissions: dict) -> None: ...

    @abstractmethod
    async def demote(self, conversation: Any, identity_id: int) -> None: ...

    @abstractmethod
    async def ban(self, conversation: Any, identity_id: int) -> None: ...

    @abstractmethod
    async def restrict(self, conversation: Any, identity_id: int, allowed: dict,
                       until: Optional[datetime]) -> None:
        """``allowed``: permission name → still allowed (False = restricted)."""

    @abstractmethod
    async def approve(self, conversation: Any, identity_id: int) -> None:
        """Lift every restriction (unban / unrestrict)."""

    @abstractmethod
    async def list_invite_links(self, conversation: Any) -> list[dict]: ...

    @abstractmethod
    async def create_invite_link(self, conversation: Any, *, expire: Optional[datetime],
                                 usage_limit: Optional[int], title: Optional[str],
                                 request_needed: bool) -> dict: ...

    @abstractmethod
    async def revoke_invite_link(self, conversation: Any, link: str) -> None: ...

    @abstractmethod
    async def get_group_settings(self, conversation: Any) -> dict:
        """``{"title", "about"}``."""

    @abstractmethod
    async def update_group_settings(self, conversation: Any, *, title: Optional[str],
                                    about: Optional[str]) -> list[str]:
        """Returns the names of the fields that were changed."""

    @abstractmethod
    async def set_slow_mode(self, conversation: Any, seconds: int) -> None: ...

    @abstractmethod
    async def post_stats(self, conversation: Any, limit: int) -> list[dict]:
        """Recent broadcast posts: ``{external_id, text, date, views,
        forwards, replies_count}``."""


class HistorySync(ABC):
    """Pulling a conversation's history from the channel on demand."""

    @abstractmethod
    async def sync_history(self, conversation: Any, limit: int, *, offset_seq: int = 0,
                           topic_id: Optional[int] = None) -> SyncResult:
        """Newest messages (``offset_seq`` 0) or the page older than it."""

    @abstractmethod
    async def fetch_around(self, conversation: Any, *, anchor_external_id: Optional[str],
                           date: Optional[datetime], limit: int) -> SyncResult: ...

    @abstractmethod
    def can_fetch_around(self, conversation: Any, external_id: Optional[str]) -> bool:
        """Whether an unstored message (by the channel's id) / date window
        can be fetched."""

    @abstractmethod
    def is_syncing(self, conversation: Any) -> bool: ...

    @abstractmethod
    async def request_full_history(self, conversation: Any) -> None:
        """Queue a full-history walk of the conversation ahead of others."""

    @abstractmethod
    async def sync_recent(self, n_conversations: int, per_conversation: int) -> int:
        """Refresh the newest messages of the most recent conversations;
        returns how many conversations were synced."""

    @abstractmethod
    async def reachable(self) -> bool:
        """Whether the channel's network session is up (reconnecting first)."""

    @abstractmethod
    async def ensure_reachable(self) -> None:
        """Raise ``ServiceUnavailable`` when the network session is down."""


class GlobalSearch(ABC):
    """Server-side search across all of the account's conversations."""

    @abstractmethod
    async def search_messages(self, query: str) -> list[dict]:
        """Hits grouped per conversation (stored locally so they can be
        opened): ``{conversation_id, conversation_title, conversation_kind,
        messages: [{external_id, text, sender_name, date}]}``."""


class AddressBook(ABC):
    """The account's own contact list."""

    @abstractmethod
    async def sync_address_book(self) -> Optional[dict]:
        """Import the contact list (entries become identities marked
        ``is_contact``; others are unmarked). Returns counts, or None when the
        account isn't signed in."""
