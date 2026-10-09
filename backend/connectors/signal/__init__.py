"""Signal connector (Phase 4). ``register`` adds it to the connector manager."""


def register(manager) -> None:
    """Register the Signal connector factory: one connector per linked number."""
    from backend.connectors.signal.connector import SignalConnector

    manager.register_factory(SignalConnector.channel, SignalConnector.display_name, SignalConnector)
