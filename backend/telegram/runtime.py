"""The single owner of the live Telegram runtime.

Startup, vault unlock, onboarding (setup / verify-code / verify-2fa) all call
``runtime.ensure_started()``; logout, vault reset and shutdown call
``runtime.stop()``. Nothing else creates clients, registers handlers or starts
sync loops, so a second unlock can never stack a client or a handler set.

``ensure_started`` only does the fast part inline (connect, authorize,
register handlers); catch-up and the initial syncs run as supervised
background tasks that report progress over WebSocket (``telegram_status``).
"""

import asyncio
import logging
from typing import Optional

from backend.tasks import spawn
from backend.telegram import client as tg_client
from backend.telegram.client import TelegramUnavailable
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

PERIODIC_DIALOG_SYNC_SECONDS = 120
RECONNECT_BACKOFF_MAX = 300


class TelegramRuntime:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
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
        await ws_manager.broadcast({"type": "telegram_status", "state": state, "error": error})

    def _spawn(self, coro, name: str) -> None:
        self._tasks.append(spawn(coro, name=name))

    # -- client access (the only way to obtain a usable client) --------------

    async def client(self, require: bool = True):
        """Return the connected client, reconnecting a dropped link first.

        require=True raises TelegramUnavailable (HTTP 503) when there is no
        client or the reconnect fails; require=False returns None instead.
        Never creates a client — that happens only under the runtime lock.
        """
        c = tg_client.get_client()
        if c is None:
            if require:
                raise TelegramUnavailable()
            return None
        if not c.is_connected():
            async with self._connect_lock:
                if not c.is_connected():
                    try:
                        await c.connect()
                    except Exception as e:
                        logger.warning("Telegram reconnect failed: %s", e)
                        if require:
                            raise TelegramUnavailable(f"Telegram reconnect failed: {e}") from e
                        return None
        return c

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
        return await self.client(require=True)

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
            bulk_sync, repair_garbled_text, resolve_numeric_names, sync_dialogs, sync_global_mentions,
        )

        # Handlers are registered, so catch-up updates are processed by them.
        await self._step("catch_up", client.catch_up())
        await self._step("dialogs", sync_dialogs(limit=200))
        await self._step("bulk_sync", bulk_sync(n_chats=50, messages_per_chat=30))
        await self._set_state("ready")
        await self._step("all_dialogs", sync_dialogs(include_archived=True))
        await self._step("mentions", sync_global_mentions(days=30))
        await self._step("numeric_names", resolve_numeric_names())
        await self._step("unscanned_scan", self._scan_unscanned())
        await self._step("text_repair", repair_garbled_text())
        # Full DM history archive: its own single-flight job, so it never
        # blocks unlock or this sequence (its first step reuses the dialog
        # enumeration done above).
        from backend.telegram import archive
        archive.auto_start()

    async def _scan_unscanned(self) -> None:
        try:
            from backend.scam.queue import scan_unscanned_messages
        except ImportError:
            logger.info("backend.scam.queue not available; skipping startup scan")
            return
        await scan_unscanned_messages(max_messages=200, max_age_hours=48)

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
                    async with self._connect_lock:
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


def _on_vault_lock() -> None:
    """Vault locked or reset from any path: the Telegram session is a vault
    secret too, so drop the client and every task that uses it."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    spawn(runtime.stop(), name="runtime_stop_on_vault_lock")


def _register_vault_listener() -> None:
    from backend import secrets as vault
    vault.add_lock_listener(_on_vault_lock)


_register_vault_listener()
