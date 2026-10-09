"""Gmail connector (Phase 3). ``register`` adds it to the connector manager."""


def register(manager) -> None:
    """One Gmail connector per connected Google account."""
    from backend.connectors.gmail.connector import GmailConnector

    manager.register_factory(GmailConnector.channel, GmailConnector.display_name, GmailConnector)
