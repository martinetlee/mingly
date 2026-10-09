"""Owns every connector's lifecycle.

Startup, vault unlock and onboarding call ``start_all``; vault lock/reset
and shutdown call ``stop_all``. Credentials live in the vault, so a locked
vault stops every connector (the lock listener below).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from backend.connectors.base import Connector
from backend.tasks import spawn

logger = logging.getLogger(__name__)


class ConnectorManager:
    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        if connector.account_key in self._connectors:
            raise ValueError(f"Connector {connector.account_key} already registered")
        self._connectors[connector.account_key] = connector

    def all(self) -> list[Connector]:
        return list(self._connectors.values())

    def get(self, key: str) -> Optional[Connector]:
        """Look up by account key, or by channel for single-account channels."""
        if key in self._connectors:
            return self._connectors[key]
        matches = [c for c in self._connectors.values() if c.channel == key]
        return matches[0] if len(matches) == 1 else None

    async def start_all(self) -> dict[str, bool]:
        """Start every configured connector concurrently. One failing channel
        never keeps the others down; its error is in its status."""
        configured = [c for c in self.all() if c.is_configured()]
        results = await asyncio.gather(*(c.start() for c in configured), return_exceptions=True)
        started = {}
        for c, res in zip(configured, results):
            if isinstance(res, BaseException):
                logger.warning("Connector %s failed to start: %r", c.account_key, res)
                started[c.account_key] = False
            else:
                started[c.account_key] = bool(res)
        return started

    async def stop_all(self) -> None:
        results = await asyncio.gather(*(c.stop() for c in self.all()), return_exceptions=True)
        for c, res in zip(self.all(), results):
            if isinstance(res, BaseException):
                logger.warning("Connector %s failed to stop: %r", c.account_key, res)


def _build_manager() -> ConnectorManager:
    from backend.telegram.connector import connector as telegram

    m = ConnectorManager()
    m.register(telegram)
    return m


manager = _build_manager()


def _on_vault_lock() -> None:
    """Vault locked or reset from any path: sessions are vault secrets too,
    so drop every connector and the tasks that use it."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    spawn(manager.stop_all(), name="connectors_stop_on_vault_lock")


def _register_vault_listener() -> None:
    from backend import secrets as vault
    vault.add_lock_listener(_on_vault_lock)


_register_vault_listener()
