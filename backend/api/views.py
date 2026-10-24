"""API views of domain objects that combine stored rows with runtime state."""

from backend.connectors.manager import manager
from backend.db.models import Conversation
from backend.serializers import conversation_out


def conversation_json(conv: Conversation, *, with_last_message: bool = True) -> dict:
    """``serializers.conversation_out`` with the live connector's capabilities and history note."""
    return conversation_out(conv, capabilities=manager.capabilities_for(conv), history_note=manager.history_note_for(conv),
                            with_last_message=with_last_message)
