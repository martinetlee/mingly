"""Telegram as a ``Connector``: a thin adapter over ``TelegramRuntime``.

The runtime stays the single owner of the Telethon client; this class maps
its lifecycle and onboarding (API credentials → phone → code → optional 2FA
password) onto the channel-neutral connector contract; sign-in runs through
``/api/accounts/telegram/auth``. Telegram implements every optional role
(``connectors.roles``); the work lives in ``outgoing``, ``dialogs``, ``admin``
and the sync ``coordinator`` / ``archive``.
"""

from __future__ import annotations

import secrets
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError

from backend import secrets as vault
from backend.connectors.base import (
    AuthField, AuthFlowError, AuthStep, Capability, Connector, ConnectorStatus, OutgoingMessage, SentFiles,
    SyncResult,
)
from backend.connectors.roles import (
    AddressBook, DialogActions, ForumTopics, GlobalSearch, GroupAdmin, GroupMembers, HistorySync, MessageActions,
)
from backend.telegram import admin, archive, dialogs, outgoing
from backend.telegram import client as tg_client
from backend.telegram.coordinator import coordinator
from backend.telegram.runtime import runtime
from backend.telegram.sync import bulk_sync

MAX_OPEN_FLOWS = 8


class InvalidCredentials(ValueError):
    pass


@dataclass
class _Flow:
    stage: str  # credentials | phone | code | password
    phone: Optional[str] = None
    phone_code_hash: Optional[str] = None


_CREDENTIAL_FIELDS = [
    AuthField("api_id", "API ID", placeholder="12345678"),
    AuthField("api_hash", "API Hash", kind="password"),
]
_PHONE_FIELDS = [AuthField("phone", "Phone number", kind="phone", placeholder="+1 555 123 4567")]
_CODE_FIELDS = [AuthField("code", "Login code", kind="code")]
_PASSWORD_FIELDS = [AuthField("password", "Two-step verification password", kind="password")]


class TelegramConnector(Connector, MessageActions, DialogActions, GroupMembers, ForumTopics, GroupAdmin,
                        HistorySync, GlobalSearch, AddressBook):
    channel = "telegram"
    display_name = "Telegram"
    capabilities = frozenset({
        Capability.SEND, Capability.EDIT, Capability.DELETE, Capability.REACT, Capability.REPLY,
        Capability.PIN, Capability.FORWARD,
        Capability.MEDIA_SEND, Capability.READ_RECEIPTS, Capability.FORUM_TOPICS,
        Capability.ADMIN_MODERATION, Capability.HISTORY_BACKFILL, Capability.TYPING,
        Capability.PRESENCE, Capability.MEMBERS,
    })

    def __init__(self) -> None:
        super().__init__(account_id=None)
        self._flows: OrderedDict[str, _Flow] = OrderedDict()

    @property
    def account_key(self) -> str:
        # One Telegram account, owned by the process-wide runtime.
        return "telegram:default"

    # -- lifecycle -----------------------------------------------------------

    def is_configured(self) -> bool:
        return tg_client.has_credentials()

    async def start(self) -> bool:
        if not self.is_configured():
            return False
        return await runtime.ensure_started()

    async def stop(self) -> None:
        await runtime.stop()

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(runtime.state, runtime.last_error)

    async def fetch_attachment(self, attachment, message, conversation) -> str:
        from backend.telegram.media import download_attachment

        return await download_attachment(attachment.id)

    def account_actions(self) -> list[dict]:
        return [{"id": "change_credentials", "label": "Change API credentials",
                 "params": {"reset_credentials": True}}]

    def conversation_capabilities(self, conversation) -> frozenset[Capability]:
        caps = set(self.capabilities)
        if not conversation.is_forum:
            caps.discard(Capability.FORUM_TOPICS)
        if conversation.kind == "direct":
            caps.discard(Capability.ADMIN_MODERATION)
            caps.discard(Capability.MEMBERS)
        return frozenset(caps)

    # -- messages (base contract + MessageActions) ----------------------------

    async def send(self, conversation, draft: OutgoingMessage) -> list[int]:
        return await outgoing.send(conversation, draft)

    async def send_files(self, conversation, draft: OutgoingMessage) -> SentFiles:
        return await outgoing.send_files(conversation, draft)

    async def react(self, conversation, message, emoji: Optional[str]) -> None:
        await outgoing.react(conversation, message, emoji)

    async def mark_read(self, conversation) -> None:
        await outgoing.acknowledge_read(conversation)

    async def edit_message(self, conversation, message, text: str) -> int:
        return await outgoing.edit_message(conversation, message, text)

    async def delete_messages(self, conversation, messages: list, *, revoke: bool) -> None:
        await outgoing.delete_messages(conversation, messages, revoke=revoke)

    async def forward_messages(self, source, messages: list, target) -> None:
        await outgoing.forward_messages(source, messages, target)

    async def pin_message(self, conversation, message, *, pinned: bool, notify: bool) -> None:
        await outgoing.pin_message(conversation, message, pinned=pinned, notify=notify)

    # -- DialogActions / GroupMembers / ForumTopics / GlobalSearch / AddressBook

    async def set_dialog_pinned(self, conversation, pinned: bool) -> None:
        await dialogs.set_dialog_pinned(conversation, pinned)

    async def set_dialog_archived(self, conversation, archived: bool) -> None:
        await dialogs.set_dialog_archived(conversation, archived)

    async def set_dialog_muted(self, conversation, muted: bool, until: Optional[datetime]) -> None:
        await dialogs.set_dialog_muted(conversation, muted, until)

    async def list_members(self, conversation, limit: int) -> list[dict]:
        return await dialogs.list_members(conversation, limit)

    async def presence(self, conversation) -> dict:
        return await dialogs.presence(conversation)

    async def send_typing(self, conversation) -> None:
        await dialogs.send_typing(conversation)

    async def list_topics(self, conversation) -> list[dict]:
        return await dialogs.list_topics(conversation)

    async def create_topic(self, conversation, title: str) -> Optional[int]:
        return await dialogs.create_topic(conversation, title)

    async def set_topic_closed(self, conversation, topic_id: int, closed: bool) -> None:
        await dialogs.set_topic_closed(conversation, topic_id, closed)

    async def search_messages(self, query: str) -> list[dict]:
        return await dialogs.search_messages(query)

    async def sync_address_book(self) -> Optional[dict]:
        return await dialogs.sync_address_book(configured=self.is_configured())

    # -- GroupAdmin -----------------------------------------------------------

    async def promote(self, conversation, identity_id: int, permissions: dict) -> None:
        await admin.promote(conversation, identity_id, permissions)

    async def demote(self, conversation, identity_id: int) -> None:
        await admin.demote(conversation, identity_id)

    async def ban(self, conversation, identity_id: int) -> None:
        await admin.ban(conversation, identity_id)

    async def restrict(self, conversation, identity_id: int, allowed: dict, until: Optional[datetime]) -> None:
        await admin.restrict(conversation, identity_id, allowed, until)

    async def approve(self, conversation, identity_id: int) -> None:
        await admin.approve(conversation, identity_id)

    async def list_invite_links(self, conversation) -> list[dict]:
        return await admin.list_invite_links(conversation)

    async def create_invite_link(self, conversation, *, expire, usage_limit, title, request_needed) -> dict:
        return await admin.create_invite_link(
            conversation, expire=expire, usage_limit=usage_limit, title=title, request_needed=request_needed,
        )

    async def revoke_invite_link(self, conversation, link: str) -> None:
        await admin.revoke_invite_link(conversation, link)

    async def get_group_settings(self, conversation) -> dict:
        return await admin.get_group_settings(conversation)

    async def update_group_settings(self, conversation, *, title, about) -> list[str]:
        return await admin.update_group_settings(conversation, title=title, about=about)

    async def set_slow_mode(self, conversation, seconds: int) -> None:
        await admin.set_slow_mode(conversation, seconds)

    async def post_stats(self, conversation, limit: int) -> list[dict]:
        return await admin.post_stats(conversation, limit)

    # -- HistorySync ----------------------------------------------------------

    async def sync_history(self, conversation, limit: int, *, offset_seq: int = 0,
                           topic_id: Optional[int] = None) -> SyncResult:
        return await coordinator.sync_conversation(conversation.id, limit, offset_seq=offset_seq, topic_id=topic_id)

    async def fetch_around(self, conversation, *, anchor_external_id: Optional[str],
                           date: Optional[datetime], limit: int) -> SyncResult:
        return await coordinator.fetch_around(
            conversation.id, anchor_external_id=anchor_external_id, date=date, limit=limit,
        )

    def can_fetch_around(self, conversation, external_id: Optional[str]) -> bool:
        # Telegram message ids are integers
        return external_id is None or (external_id.isascii() and external_id.isdigit())

    def is_syncing(self, conversation) -> bool:
        return coordinator.is_syncing(conversation.id)

    async def request_full_history(self, conversation) -> None:
        await archive.request_conversation(conversation.id, priority=True)

    async def sync_recent(self, n_conversations: int, per_conversation: int) -> int:
        return await bulk_sync(n_conversations=n_conversations, messages_per_conversation=per_conversation)

    async def reachable(self) -> bool:
        return await tg_client.connected_client(require=False) is not None

    async def ensure_reachable(self) -> None:
        await tg_client.connected_client()

    # -- sign-in steps --------------------------------------------------------

    async def save_credentials(self, api_id: str, api_hash: str) -> bool:
        """Store API credentials and connect a fresh client. Returns whether
        the stored session is already authorized (the runtime is then up)."""
        api_id, api_hash = api_id.strip(), api_hash.strip()
        if not api_id.isdigit():
            raise InvalidCredentials("API ID must be a number")
        if not api_hash:
            raise InvalidCredentials("API Hash is required")
        vault.save_secret("tg_api_id", api_id)
        vault.save_secret("tg_api_hash", api_hash)
        authorized = await runtime.replace_client()
        if authorized:
            await runtime.ensure_started()
        return authorized

    async def send_code(self, phone: str) -> str:
        return await runtime.send_code(phone)

    async def sign_in(self, phone: str, code: str, phone_code_hash: str) -> Optional[dict]:
        """Returns the signed-in account, or None when a 2FA password is
        required. Raises PhoneCodeInvalidError for a wrong code."""
        try:
            await runtime.sign_in(phone, code, phone_code_hash)
        except SessionPasswordNeededError:
            return None
        return await self._after_sign_in()

    async def sign_in_2fa(self, password: str) -> dict:
        await runtime.sign_in_2fa(password)
        return await self._after_sign_in()

    async def _after_sign_in(self) -> dict:
        await tg_client.persist_session()
        await runtime.ensure_started()
        me = await tg_client.get_me()
        return {"username": me.username, "first_name": me.first_name}

    async def logout(self) -> None:
        """Terminate the server-side session, stop all sync work and forget
        the local session string."""
        await runtime.stop(disconnect=False)
        await tg_client.log_out()
        vault.delete_secret("telegram_session")

    # -- generic auth flow ---------------------------------------------------

    def _open(self, flow: _Flow) -> str:
        flow_id = secrets.token_urlsafe(12)
        self._flows[flow_id] = flow
        while len(self._flows) > MAX_OPEN_FLOWS:
            self._flows.popitem(last=False)
        return flow_id

    def _step(self, flow_id: str, flow: _Flow, message: Optional[str] = None) -> AuthStep:
        fields = {
            "credentials": _CREDENTIAL_FIELDS,
            "phone": _PHONE_FIELDS,
            "code": _CODE_FIELDS,
            "password": _PASSWORD_FIELDS,
        }[flow.stage]
        titles = {
            "credentials": "Connect to Telegram",
            "phone": "Your phone number",
            "code": "Enter the code sent via Telegram",
            "password": "Two-step verification",
        }
        return AuthStep(flow_id, "fields", title=titles[flow.stage], message=message, fields=list(fields))

    def _done(self, flow_id: str, account: Optional[dict] = None) -> AuthStep:
        self._flows.pop(flow_id, None)
        name = (account or {}).get("username") or (account or {}).get("first_name")
        return AuthStep(flow_id, "done", message=f"Signed in as {name}" if name else "Signed in")

    async def begin_auth(self, params: dict[str, Any]) -> AuthStep:
        """Starts at the phone step when API credentials exist, unless
        ``params["reset_credentials"]`` asks to enter new ones."""
        start_at_phone = self.is_configured() and not params.get("reset_credentials")
        flow = _Flow("phone" if start_at_phone else "credentials")
        return self._step(self._open(flow), flow)

    async def continue_auth(self, flow_id: str, data: dict[str, Any]) -> AuthStep:
        flow = self._flows.get(flow_id)
        if flow is None:
            raise AuthFlowError("Unknown or expired sign-in flow")
        try:
            return await self._advance(flow_id, flow, data)
        except InvalidCredentials as e:
            return self._step(flow_id, flow, str(e))
        except PhoneCodeInvalidError:
            return self._step(flow_id, flow, "Invalid code. Please try again.")
        except Exception as e:
            return self._step(flow_id, flow, str(e) or type(e).__name__)

    async def _advance(self, flow_id: str, flow: _Flow, data: dict[str, Any]) -> AuthStep:
        if flow.stage == "credentials":
            if await self.save_credentials(str(data.get("api_id", "")), str(data.get("api_hash", ""))):
                return self._done(flow_id)
            flow.stage = "phone"
        elif flow.stage == "phone":
            phone = str(data.get("phone", "")).strip()
            flow.phone_code_hash = await self.send_code(phone)
            flow.phone = phone
            flow.stage = "code"
        elif flow.stage == "code":
            account = await self.sign_in(flow.phone or "", str(data.get("code", "")).strip(), flow.phone_code_hash or "")
            if account is not None:
                return self._done(flow_id, account)
            flow.stage = "password"
        elif flow.stage == "password":
            return self._done(flow_id, await self.sign_in_2fa(str(data.get("password", ""))))
        return self._step(flow_id, flow)


connector = TelegramConnector()
