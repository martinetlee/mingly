"""The single owner of the live Telegram runtime.

Startup, vault unlock, onboarding (setup / verify-code / verify-2fa) all reach
``runtime.ensure_started()`` through the Telegram connector
(``backend.telegram.connector``); logout, vault lock/reset and shutdown reach
``runtime.stop()`` the same way. Nothing else creates clients, registers handlers or starts
sync loops, so a second unlock can never stack a client or a handler set.

``ensure_started`` only does the fast part inline (connect, authorize,
register handlers); catch-up and the initial syncs run as supervised
background tasks that report progress over WebSocket (``connector_status``).
"""

import asyncio
import logging
from typing import Optional

from backend.aio import LoopLock
from backend.ingest import fanout
from backend.tasks import spawn
from backend.telegram import client as tg_client
from backend.telegram.client import TelegramUnavailable
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

PERIODIC_DIALOG_SYNC_SECONDS = 120
RECONNECT_BACKOFF_MAX = 300


class TelegramRuntime:
    def __init__(self):
        # Built on the server loop at first use: this singleton is created
        # at import, before (and on another thread than) that loop.
        self._lock = LoopLock()
        self._client = None
        self._handlers: list = []
        self._tasks: list[asyncio.Task] = []
        self.state = "stopped"
        self.last_error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._client is not None and bool(self._handlers)

    def status(self) -> dict:
        return {"state": self.state, "error": self.last_error}

    async def _set_state(self, state: str, error: Optional[str] = None) -> None:
        self.state = state
        self.last_error = error
        await ws_manager.broadcast({
            "type": "connector_status", "channel": "telegram", "account_key": "telegram:default",
            "state": state, "error": error,
        })

    def _spawn(self, coro, name: str) -> None:
        self._tasks.append(spawn(coro, name=name))

    async def login_client(self):
        """Client for onboarding (send-code / sign-in): reuse the current one
        or create it under the runtime lock."""
        async with self._lock:
            c = tg_client.get_client()
            if c is None:
                if not tg_client.has_credentials():
                    raise TelegramUnavailable("Telegram API credentials are not set")
                await tg_client._create_client()
                c = tg_client.get_client()
        return await tg_client.connected_client(require=True)

    async def replace_client(self) -> bool:
        """New API credentials: tear down and connect a fresh client. Returns
        whether it is already authorized (then call ensure_started)."""
        async with self._lock:
            self._teardown_locked()
            return await tg_client._create_client()

    async def send_code(self, phone: str) -> str:
        c = await self.login_client()
        return (await c.send_code_request(phone)).phone_code_hash

    async def sign_in(self, phone: str, code: str, phone_code_hash: str):
        c = await self.login_client()
        return await c.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)

    async def sign_in_2fa(self, password: str):
        c = await self.login_client()
        return await c.sign_in(password=password)

    async def ensure_started(self) -> bool:
        """Idempotently bring the runtime up. Returns True when Telegram is
        authorized and live handlers are registered, False when the account
        still needs onboarding. Raises if handler registration fails."""
        from backend.telegram.sync import register_event_handlers

        async with self._lock:
            client = tg_client.get_client()
            if self.running and client is self._client:
                return True
            # Detach the previous client's handlers/tasks before anything can
            # disconnect it, so its watchdog never fights the new client.
            self._teardown_locked()
            if client is None or not client.is_connected():
                if not tg_client.has_credentials():
                    return False
                await self._set_state("starting")
                authorized = await tg_client._create_client()
                client = tg_client.get_client()
            else:
                authorized = await client.is_user_authorized()
            if not authorized:
                await self._set_state("unauthorized")
                return False

            try:
                self._handlers = register_event_handlers(client)
            except Exception as e:
                logger.exception("Registering Telegram event handlers failed")
                self._handlers = []
                await self._set_state("error", f"Live updates unavailable: {e}")
                raise
            self._client = client
            await self._set_state("syncing")
            self._spawn(self._startup_sequence(client), "tg_startup_sequence")
            self._spawn(self._periodic_dialog_sync(), "tg_periodic_dialog_sync")
            self._spawn(self._watchdog(client), "tg_reconnect_watchdog")
            return True

    def _teardown_locked(self) -> None:
        for t in self._tasks:
            if not t.done() and t is not asyncio.current_task():
                t.cancel()
        self._tasks = []
        if self._client is not None:
            for callback, _builder in self._handlers:
                try:
                    self._client.remove_event_handler(callback)
                except Exception:
                    logger.warning("Removing event handler %s failed", callback, exc_info=True)
        self._handlers = []
        self._client = None

    async def stop(self, *, disconnect: bool = True) -> None:
        """Cancel runtime tasks, remove handlers and (optionally) disconnect."""
        from backend.telegram import archive
        from backend.telegram.media import stop_media_workers
        from backend.telegram.sync import cancel_tag_sync

        async with self._lock:
            self._teardown_locked()
            stop_media_workers()
            cancel_tag_sync()
            archive.stop()
            if disconnect:
                await tg_client.disconnect()
            await self._set_state("stopped")

    # -- background work -----------------------------------------------------

    async def _step(self, label: str, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Telegram startup step '%s' failed", label, exc_info=True)

    async def _startup_sequence(self, client) -> None:
        from backend.telegram.sync import (
            bulk_sync, resolve_numeric_names, sync_dialogs, sync_global_mentions,
        )

        await self._step("account", self._record_account(client))
        # Handlers are registered, so catch-up updates are processed by them.
        await self._step("catch_up", client.catch_up())
        await self._step("dialogs", sync_dialogs(limit=200))
        await self._step("bulk_sync", bulk_sync(n_conversations=50, messages_per_conversation=30))
        await self._set_state("ready")
        await self._step("all_dialogs", sync_dialogs(include_archived=True))
        await self._step("mentions", sync_global_mentions(days=30))
        await self._step("numeric_names", resolve_numeric_names())
        await self._step("unscanned_scan", self._scan_unscanned())
        # Full DM history archive: its own single-flight job, so it never
        # blocks unlock or this sequence (its first step reuses the dialog
        # enumeration done above).
        from backend.telegram import archive
        archive.auto_start()

    async def _record_account(self, client) -> None:
        """Record the signed-in user as the Telegram account's owner (and as
        the person who is "me")."""
        from backend.db import crud
        from backend.db.database import AsyncSessionLocal
        from backend.telegram import ids
        from backend.telegram.entities import user_full_name

        me = await client.get_me()
        if me is None:
            return
        async with AsyncSessionLocal() as db:
            account = await ids.account(db)
            identity = await ids.ensure_user_identity(db, me, is_self=True, commit=True)
            label = f"@{me.username}" if me.username else (user_full_name(me) or me.phone)
            await crud.set_account_owner(
                db, account.id, external_id=str(me.id), label=label, self_identity_id=identity.id,
            )

    async def _scan_unscanned(self) -> None:
        await fanout.inspect_backlog()

    async def _periodic_dialog_sync(self) -> None:
        from backend.telegram.sync import sync_dialogs
        while True:
            await asyncio.sleep(PERIODIC_DIALOG_SYNC_SECONDS)
            await self._step("periodic_dialogs", sync_dialogs(limit=100, include_archived=True))

    async def _watchdog(self, client) -> None:
        """Telethon retries a dropped connection a few times and then gives up
        silently. Reconnect with backoff after that, then catch up."""
        from backend.telegram.sync import sync_dialogs

        while True:
            try:
                await client.disconnected
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # disconnected with an error — same handling
            if client is not self._client:
                return
            await asyncio.sleep(1)
            if client.is_connected():
                continue  # reconnected by a request path in the meantime
            await self._set_state("reconnecting")
            backoff = 2
            while not client.is_connected():
                try:
                    async with tg_client.connect_lock:
                        if not client.is_connected():
                            await client.connect()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.info("Telegram reconnect failed (%s); retrying in %ss", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
            if not await client.is_user_authorized():
                await self._set_state("unauthorized", "Telegram session is no longer authorized")
                return
            await self._set_state("ready")
            await self._step("catch_up", client.catch_up())
            await self._step("dialogs", sync_dialogs(limit=100, include_archived=True))


runtime = TelegramRuntime()

