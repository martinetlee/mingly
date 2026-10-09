"""SMS / iMessage connector (Phase 5), read-only over macOS Messages' database.
``register`` adds it to the connector manager."""


def register(manager) -> None:
    """Register the one SMS connector (a Mac has one Messages database)."""
    from backend.connectors.sms.connector import SmsConnector

    manager.register(SmsConnector())
