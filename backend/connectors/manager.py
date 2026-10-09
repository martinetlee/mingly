"""Owns every connector's lifecycle.

Startup, vault unlock and onboarding call ``start_all``; vault lock/reset
and shutdown call ``stop_all``. Credentials live in the vault, so a locked
vault stops every connector (the lock listener below).

Single-account channels (Telegram) have one connector registered up front.
Multi-account channels (Gmail, …) register a factory: one connector per
``accounts`` row is created by ``load_accounts``, and a sign-in started for
the bare channel name runs on a provisional connector that is registered once
its flow created the account.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from backend.connectors.base import AuthFlowError, AuthStep, Connector
from backend.tasks import spawn

logger = logging.getLogger(__name__)

ConnectorFactory = Callable[[Optional[int]], Connector]


class ConnectorManager:
    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}
        self._factories: dict[str, ConnectorFactory] = {}
        self._names: dict[str, str] = {}
        self._flows: dict[str, Connector] = {}  # flow_id -> connector running it

    # -- registry --------------------------------------------------------------

    def register(self, connector: Connector) -> None:
        if connector.account_key in self._connectors:
            raise ValueError(f"Connector {connector.account_key} already registered")
        self._connectors[connector.account_key] = connector

    def register_factory(self, channel: str, display_name: str, factory: ConnectorFactory) -> None:
        self._factories[channel] = factory
        self._names[channel] = display_name

    def channels(self) -> list[dict[str, Any]]:
        """Channels a user can connect (for the Connections page)."""
        single = {c.channel: c for c in self._connectors.values() if not c.multi_account}
        out = [{"channel": ch, "display_name": c.display_name, "multi_account": False} for ch, c in single.items()]
        out += [
            {"channel": ch, "display_name": self._names[ch], "multi_account": True}
            for ch in self._factories if ch not in single
        ]
        return sorted(out, key=lambda d: d["display_name"])

    def all(self) -> list[Connector]:
        return list(self._connectors.values())

    def get(self, key: str) -> Optional[Connector]:
        """Look up by account key, or by channel when it has exactly one."""
        if key in self._connectors:
            return self._connectors[key]
        matches = [c for c in self._connectors.values() if c.channel == key]
        return matches[0] if len(matches) == 1 else None

    def for_account(self, channel: Optional[str], account_id: Optional[int]) -> Optional[Connector]:
        """The connector serving a conversation's account."""
        for c in self._connectors.values():
            if c.channel == channel and (c.account_id == account_id or not c.multi_account):
                return c
        return None

    async def load_accounts(self) -> None:
        """Create connectors for stored accounts of multi-account channels,
        and tell single-account connectors their stored account (the oldest
        signed-in row of their channel; None when there is none)."""
        from backend.db import crud
        from backend.db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            accounts = await crud.get_accounts(db)
        signed_in = {}
        for acc in accounts:
            if acc.external_id is not None:
                signed_in.setdefault(acc.channel, acc.id)
        for c in self._connectors.values():
            if not c.multi_account:
                c.account_id = signed_in.get(c.channel)
        for acc in accounts:
            factory = self._factories.get(acc.channel)
            if factory is None or f"{acc.channel}:{acc.id}" in self._connectors:
                continue
            self.register(factory(acc.id))

    # -- sign-in ---------------------------------------------------------------

    async def begin_auth(self, key: str, params: dict[str, Any]) -> Optional[AuthStep]:
        """Start signing in: re-auth of a registered account (by key or
        single-account channel), or a new account of a multi-account channel.
        None when ``key`` names nothing."""
        connector = self._connectors.get(key)
        if connector is None and key in self._factories:
            connector = self._factories[key](None)  # provisional until the flow creates the account
        if connector is None:
            connector = self.get(key)
        if connector is None:
            return None
        step = await connector.begin_auth(params)
        self._track(step, connector)
        return step

    async def continue_auth(self, flow_id: str, data: dict[str, Any]) -> AuthStep:
        connector = self._flows.get(flow_id)
        if connector is None:
            raise AuthFlowError("Unknown or expired sign-in flow")
        step = await connector.continue_auth(flow_id, data)
        self._track(step, connector)
        if step.kind == "done" and connector.account_key not in self._connectors and connector.account_id is not None:
            self.register(connector)
            spawn(self._start_one(connector), name=f"connector_start:{connector.account_key}")
        return step

    def _track(self, step: AuthStep, connector: Connector) -> None:
        if step.kind in ("done", "error"):
            self._flows.pop(step.flow_id, None)
        else:
            self._flows[step.flow_id] = connector
            while len(self._flows) > 32:
                self._flows.pop(next(iter(self._flows)))

    async def remove(self, key: str) -> bool:
        """Disconnect an account for good (multi-account channels): log out,
        unregister and delete its account row with everything synced from it."""
        connector = self._connectors.get(key)
        if connector is None or not connector.multi_account:
            return False
        await connector.stop()
        await connector.remove()
        self._connectors.pop(key, None)
        if connector.account_id is not None:
            from backend.db.database import AsyncSessionLocal
            from backend.db.models import Account

            async with AsyncSessionLocal() as db:
                acc = await db.get(Account, connector.account_id)
                if acc is not None:
                    await db.delete(acc)
                    await db.commit()
        return True

    # -- lifecycle -------------------------------------------------------------

    async def _start_one(self, connector: Connector) -> bool:
        try:
            return bool(await connector.start())
        except Exception as e:  # one failing channel never keeps the others down
            logger.warning("Connector %s failed to start: %r", connector.account_key, e)
            return False

    async def start_all(self) -> dict[str, bool]:
        """Start every configured connector concurrently; errors end up in
        each connector's status."""
        try:
            await self.load_accounts()
        except Exception:
            logger.warning("Loading connector accounts failed", exc_info=True)
        configured = [c for c in self.all() if c.is_configured()]
        results = await asyncio.gather(*(self._start_one(c) for c in configured))
        return {c.account_key: ok for c, ok in zip(configured, results)}

    async def stop_all(self) -> None:
        results = await asyncio.gather(*(c.stop() for c in self.all()), return_exceptions=True)
        for c, res in zip(self.all(), results):
            if isinstance(res, BaseException):
                logger.warning("Connector %s failed to stop: %r", c.account_key, res)


def _build_manager() -> ConnectorManager:
    from backend.telegram.connector import connector as telegram

    from backend.connectors import gmail, signal, sms

    m = ConnectorManager()
    m.register(telegram)
    gmail.register(m)
    signal.register(m)
    sms.register(m)
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
