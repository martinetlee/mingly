"""Gmail connector (Phase 3). ``register`` adds it to the connector manager."""
from backend.connectors.gmail.connector import GmailConnector


def register(manager) -> None:
    """One Gmail connector per connected Google account."""

    manager.register_factory(GmailConnector)
