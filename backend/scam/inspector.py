"""Shield as a ``MessageInspector``: incoming messages from every channel are
queued for scam checks (the queue itself is a no-op while Shield is off)."""

from typing import Optional

from backend.scam import queue, requests


class ShieldInspector:
    async def inspect(self, conversation_id: int, message_ids: list[int], source: str) -> None:
        await queue.enqueue_targets(conversation_id, message_ids, source=source)

    def note_sender_profile(self, identity_id: int, *, bio: Optional[str] = None,
                            is_contact: Optional[bool] = None) -> None:
        requests.note_sender_profile(identity_id, bio=bio, is_contact=is_contact)

    async def inspect_backlog(self) -> None:
        await queue.scan_unscanned_messages(max_messages=200, max_age_hours=48)


shield_inspector = ShieldInspector()
