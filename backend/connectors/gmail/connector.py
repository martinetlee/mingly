"""Gmail connector: one instance per connected Google account.

Sign-in: ``fields`` (the user's own Desktop OAuth client id / secret) →
``oauth_browser`` (consent in the system browser, polled until the loopback
callback exchanged the code) → ``done`` once the account row, its self
identity and the vault credentials exist.

Runtime: a background loop backfills recent threads, then polls
``history.list`` from the stored ``historyId`` (``accounts.sync_state_json``).
A thread that keeps failing is set aside (``failed_threads``) and retried on
later polls instead of holding back the cursor.
A second loop syncs the account's Google Calendar (``calendar_sync``).
A thread is one ``email_thread`` conversation; its messages, senders and
recipients go through ``backend.ingest.service``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select, update

from backend.aio import LoopLocal
from backend.connectors.base import (
    AuthField,
    AuthFlowError,
    AuthStep,
    Capability,
    Connector,
    ConnectorStatus,
    OutgoingMessage,
)
from backend.connectors.gmail import mime, oauth
from backend.connectors.gmail.api import GmailApi, GmailAuthError, GmailError, GmailNotFound
from backend.connectors.gmail.calendar_sync import CalendarSync
from backend.ingest.normalized import (
    NormalizedAttachment,
    NormalizedConversation,
    NormalizedIdentity,
    NormalizedMessage,
)
from backend.media import MediaNotFound, MediaUnavailable
from backend.tasks import spawn
from backend.ws.manager import ws_manager
from backend.people import identities as person_identities
from backend.connectors.sync_state import load_sync_state, merge_sync_state
from backend import config
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Account, Message, MessageAttachment
from backend.fsutil import atomic_write_bytes
from backend.ingest.service import delete_messages, ingest

logger = logging.getLogger(__name__)

POLL_INTERVAL = 60.0
BACKOFF_BASE = 5.0
BACKOFF_MAX = 15 * 60.0
DEFAULT_BACKFILL_DAYS = 90
MAX_BACKFILL_THREADS = 2000
FETCH_CONCURRENCY = 5
LABEL_REFRESH = timedelta(minutes=10)
# A thread that fails in a way a retry of the whole pass can't fix is set
# aside (``failed_threads`` in the sync state) and retried on later polls.
THREAD_RETRY_LIMIT = 10
MAX_FAILED_THREADS = 500

# Gmail's tabbed categories kept out of the inbox (still synced, archived).
HIDDEN_CATEGORIES = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS"}
SYSTEM_LABELS = {
    "INBOX": "Inbox", "SENT": "Sent", "IMPORTANT": "Important", "STARRED": "Starred", "SPAM": "Spam",
    "TRASH": "Trash", "CATEGORY_PERSONAL": "Personal", "CATEGORY_PROMOTIONS": "Promotions",
    "CATEGORY_SOCIAL": "Social", "CATEGORY_UPDATES": "Updates", "CATEGORY_FORUMS": "Forums",
}
SKIPPED_MESSAGE_LABELS = {"DRAFT", "CHAT"}

SETUP_HELP = (
    "Mingly connects to Gmail with your own Google OAuth client, so your mail never passes through anyone "
    "else's servers. In the Google Cloud Console: create a project, enable the Gmail API and the Google "
    "Calendar API, configure the OAuth consent screen (add yourself as a test user), then create "
    "credentials → OAuth client ID → application type \"Desktop app\" and paste its client ID and secret here."
)

_MSGID = re.compile(r"<[^<>\s]+>")


def backfill_days() -> int:
    from backend.prefs import get_pref
    try:
        days = int(get_pref("gmail_backfill_days", DEFAULT_BACKFILL_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_BACKFILL_DAYS
    return max(1, min(days, 3650))


class GmailConnector(Connector):
    channel = "gmail"
    display_name = "Gmail"
    capabilities = frozenset({
        Capability.SEND, Capability.REPLY, Capability.SUBJECT, Capability.HTML_BODY, Capability.LABELS,
        Capability.MEDIA_SEND, Capability.HISTORY_BACKFILL,
    })
    multi_account = True

    def __init__(self, account_id: Optional[int] = None) -> None:
        super().__init__(account_id)
        self._state = "stopped"
        self._error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._api: Optional[GmailApi] = None
        self._self_email: Optional[str] = None
        self._label_names: dict[str, str] = {}
        self._labels_at: Optional[datetime] = None
        self._flows: dict[str, str] = {}  # flow_id -> stage (fields | browser)
        self._sem: LoopLocal[asyncio.Semaphore] = LoopLocal(lambda: asyncio.Semaphore(FETCH_CONCURRENCY))
        self.calendar: Optional[CalendarSync] = None
        self._calendar_task: Optional[asyncio.Task] = None

    # -- status ----------------------------------------------------------------------

    def is_configured(self) -> bool:
        if self.account_id is None:
            return False
        creds = oauth.Credentials.load(self.account_id)
        return creds is not None and bool(creds.refresh_token)

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(self._state, self._error)

    def history_note(self) -> Optional[str]:
        return (
            f"Mingly holds mail from the {backfill_days()} days before this account was connected (at most "
            f"{MAX_BACKFILL_THREADS} threads) and everything since; older mail stays in Gmail."
        )

    def conversation_capabilities(self, conversation: Any) -> frozenset[Capability]:
        """A thread is always synced whole, so there is no older history to
        page in (HISTORY_BACKFILL is the account's mailbox backfill)."""
        return self.capabilities - {Capability.HISTORY_BACKFILL}

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d["label"] = self._self_email
        return d

    async def _set_state(self, state: str, error: Optional[str] = None) -> None:
        if (state, error) == (self._state, self._error):
            return
        self._state, self._error = state, error
        await ws_manager.broadcast({
            "type": "connector_status", "channel": self.channel, "account_key": self.account_key,
            "state": state, "error": error,
        })

    # -- lifecycle ---------------------------------------------------------------------

    async def start(self) -> bool:
        generation = self._stop_generation
        async with self._lifecycle():
            return await self._start_locked(generation)

    async def _start_locked(self, generation: int) -> bool:
        """Start under the lifecycle lock; spawns nothing when a stop was
        requested after the start (``generation``) was."""
        if self._stop_generation != generation:
            return False
        if self._task is not None and not self._task.done():
            return True
        creds = oauth.Credentials.load(self.account_id) if self.account_id is not None else None
        if creds is None or not creds.refresh_token:
            await self._set_state("unauthorized")
            return False

        async with AsyncSessionLocal() as db:
            acc = await crud.get_account(db, self.account_id)
        if self._stop_generation != generation:
            return False
        if acc is None:
            await self._set_state("error", "Account no longer exists")
            return False
        self._self_email = (acc.external_id or "").lower() or None
        self._api = GmailApi(creds)
        self._wake = asyncio.Event()
        await self._set_state("starting")
        self._task = spawn(self._run(), name=f"gmail_sync:{self.account_id}")
        self.calendar = CalendarSync(self.account_id, self._api)
        self._calendar_task = spawn(self.calendar.run(), name=f"gcal_sync:{self.account_id}")
        return True

    async def stop(self) -> None:
        self._stop_generation += 1
        async with self._lifecycle():
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        for task in (self._task, self._calendar_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = self._calendar_task = None
        self.calendar = None
        if self._api is not None:
            await self._api.close()
            self._api = None
        await self._set_state("stopped")

    async def restart(self) -> None:
        generation = self._stop_generation
        async with self._lifecycle():
            await self._stop_locked()
            await self._start_locked(generation)

    async def sync_now(self) -> None:
        if self._wake is not None:
            self._wake.set()
        if self.calendar is not None:
            self.calendar.wake()

    async def logout(self) -> None:
        await self._forget(keep_client=True)

    async def remove(self) -> None:
        await self._forget(keep_client=False)

    async def _forget(self, *, keep_client: bool) -> None:
        await self.stop()
        if self.account_id is None:
            return
        creds = oauth.Credentials.load(self.account_id)
        if creds is not None and creds.refresh_token:
            await oauth.revoke(creds.refresh_token)
        oauth.forget(self.account_id, keep_client=keep_client)
        await self._set_state("unauthorized")

    # -- sign-in -------------------------------------------------------------------------

    def _fields_step(self, flow_id: str, error: Optional[str] = None) -> AuthStep:
        has_client = self.account_id is not None and oauth.Credentials.load(self.account_id) is not None
        message = SETUP_HELP
        if has_client:
            message += " Leave both empty to keep the client this account already uses."
        if error:
            message = f"{error}\n\n{message}"
        return AuthStep(
            flow_id=flow_id,
            kind="fields",
            title="Connect Gmail",
            message=message,
            fields=[
                AuthField("client_id", "OAuth client ID", required=not has_client,
                          placeholder="1234-abc.apps.googleusercontent.com"),
                AuthField("client_secret", "OAuth client secret", kind="password", required=not has_client),
            ],
        )

    def _browser_step(self, pending: oauth.PendingAuth) -> AuthStep:
        return AuthStep(
            flow_id=pending.flow_id,
            kind="oauth_browser",
            title="Sign in with Google",
            message="Approve access in your browser. Google warns that the app is unverified because it is your "
                    "own OAuth client — choose Advanced → Continue. This page continues automatically.",
            auth_url=pending.auth_url(),
            poll=True,
        )

    async def begin_auth(self, params: dict[str, Any]) -> AuthStep:
        flow_id = secrets.token_urlsafe(16)
        self._flows[flow_id] = "fields"
        return self._fields_step(flow_id)

    async def continue_auth(self, flow_id: str, data: dict[str, Any]) -> AuthStep:
        stage = self._flows.get(flow_id)
        if stage is None:
            raise AuthFlowError("Unknown or expired sign-in flow")
        if stage == "fields":
            client_id = str(data.get("client_id") or "").strip()
            client_secret = str(data.get("client_secret") or "").strip()
            if not client_id and not client_secret and self.account_id is not None:
                existing = oauth.Credentials.load(self.account_id)
                if existing is not None:
                    client_id, client_secret = existing.client_id, existing.client_secret
            if not client_id or not client_secret:
                return self._fields_step(flow_id, "Enter both the client ID and the client secret.")
            self._flows[flow_id] = "browser"
            return self._browser_step(oauth.start(flow_id, client_id, client_secret))

        pending = oauth.by_flow(flow_id)
        if pending is None:
            self._flows.pop(flow_id, None)
            return AuthStep(flow_id=flow_id, kind="error", title="Sign-in expired",
                            message="The browser sign-in took too long. Start again.")
        if not pending.finished:
            return self._browser_step(pending)
        oauth.discard(pending)
        self._flows.pop(flow_id, None)
        if pending.error:
            return AuthStep(flow_id=flow_id, kind="error", title="Google sign-in failed", message=pending.error)
        try:
            email = await self._finish_sign_in(pending)
        except Exception as e:
            logger.warning("Gmail sign-in could not be completed: %r", e)
            return AuthStep(flow_id=flow_id, kind="error", title="Google sign-in failed", message=str(e))
        return AuthStep(flow_id=flow_id, kind="done", title="Gmail connected", message=f"Signed in as {email}")

    async def abandon_flow(self, flow_id: str, *, stop: bool = False) -> None:
        self._flows.pop(flow_id, None)
        pending = oauth.by_flow(flow_id)
        if pending is not None:
            oauth.discard(pending)  # a late browser callback then finds nothing to complete

    async def close_flows(self) -> None:
        for flow_id in list(self._flows):
            await self.abandon_flow(flow_id)

    async def _finish_sign_in(self, pending: oauth.PendingAuth) -> str:
        """Create (or confirm) the account row and its self identity, then
        store the credentials under the account id."""

        creds = oauth.Credentials(pending.client_id, pending.client_secret, pending.refresh_token)
        api = GmailApi(creds, access_token=pending.access_token, expires_in=pending.expires_in)
        try:
            profile = await api.profile()
        finally:
            await api.close()
        email = str(profile.get("emailAddress") or "").strip().lower()
        if not email:
            raise ValueError("Google did not report the account's email address")

        async with AsyncSessionLocal() as db:
            if self.account_id is not None:
                acc = await crud.get_account(db, self.account_id)
                if acc is None:
                    raise ValueError("This account was removed")
                if acc.external_id and acc.external_id.lower() != email:
                    raise ValueError(f"You signed in as {email}, but this connection is {acc.external_id}")
            else:
                acc = (await db.execute(
                    select(Account).where(Account.channel == self.channel, Account.external_id == email)
                )).scalar_one_or_none()
                if acc is None:
                    acc = Account(channel=self.channel, external_id=email, label=email)
                    db.add(acc)
                    await db.commit()
                    await db.refresh(acc)
            me = await person_identities.upsert_identity(db, channel=self.channel, external_id=email, is_self=True)
            await crud.set_account_owner(db, acc.id, external_id=email, label=email, self_identity_id=me.id)
            account_id = acc.id
        creds.save(account_id)
        self.account_id = account_id
        self._self_email = email
        return email

    async def on_reauthorized(self) -> None:
        """Re-auth of a running account (or the same mailbox added again):
        pick up the new grant. (A brand-new account is registered and started
        by the manager instead.)"""
        await self.restart()

    # -- sync loop ---------------------------------------------------------------------------

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                state = await self._load_sync_state()
                if not state.get("history_id"):
                    if not failures:  # while backing off, keep showing the error
                        await self._set_state("syncing")
                    await self._backfill(backfill_days())
                else:
                    await self._incremental(state)
                failures = 0
                await self._set_state("ready")
                delay = POLL_INTERVAL
            except asyncio.CancelledError:
                raise
            except GmailAuthError as e:
                await self._set_state("unauthorized", f"Sign in again: {e}")
                return
            except GmailError as e:
                failures += 1
                delay = min(e.retry_after or BACKOFF_BASE * 2 ** (failures - 1), BACKOFF_MAX)
                await self._set_state("reconnecting" if e.retryable else "error", str(e))
            except Exception as e:
                logger.exception("Gmail sync failed for account %s", self.account_id)
                failures += 1
                delay = min(BACKOFF_BASE * 2 ** (failures - 1), BACKOFF_MAX)
                await self._set_state("error", str(e) or type(e).__name__)
            await self._sleep(delay)

    async def _sleep(self, seconds: float) -> None:
        assert self._wake is not None
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _load_sync_state(self) -> dict:
        return await load_sync_state(self.account_id)

    async def _save_sync_state(self, **values: Any) -> None:
        await merge_sync_state(self.account_id, **values)

    def _require_api(self) -> GmailApi:
        if self._api is None:
            raise MediaUnavailable("Gmail is not connected")
        return self._api

    async def _refresh_labels(self, force: bool = False) -> None:
        now = datetime.utcnow()
        if not force and self._labels_at is not None and now - self._labels_at < LABEL_REFRESH:
            return
        labels = await self._require_api().labels()
        self._label_names = {l["id"]: l.get("name") or l["id"] for l in labels if l.get("type") == "user"}
        self._labels_at = now

    async def _backfill(self, days: int) -> None:
        """Pull every thread of the last ``days`` days; the historyId taken
        first makes the next poll catch anything that changed meanwhile."""
        api = self._require_api()
        profile = await api.profile()
        history_id = str(profile["historyId"])
        if not self._self_email:
            self._self_email = str(profile.get("emailAddress") or "").lower() or None
        await self._refresh_labels(force=True)
        page: Optional[str] = None
        seen = 0
        attempted: set = set()
        failed: set = set()
        while True:
            resp = await api.list_threads(f"newer_than:{days}d", page)
            ids = [t["id"] for t in resp.get("threads") or []][: MAX_BACKFILL_THREADS - seen]
            failed |= await self._sync_threads(ids)
            attempted.update(ids)
            seen += len(ids)
            page = resp.get("nextPageToken")
            if not page or seen >= MAX_BACKFILL_THREADS:
                break
        previous = _failed_threads(await self._load_sync_state())
        await self._save_sync_state(
            history_id=history_id, last_sync_at=datetime.utcnow().isoformat(),
            failed_threads=_next_failures(previous, attempted, failed),
        )

    async def _incremental(self, state: dict) -> None:
        api = self._require_api()
        start = str(state["history_id"])
        added: dict[str, set] = {}
        deleted: dict[str, set] = {}
        latest = start
        page: Optional[str] = None
        try:
            while True:
                resp = await api.history(start, page)
                for h in resp.get("history") or []:
                    for x in h.get("messagesAdded") or []:
                        m = x.get("message") or {}
                        added.setdefault(m.get("threadId"), set()).add(m.get("id"))
                    for key in ("labelsAdded", "labelsRemoved"):
                        for x in h.get(key) or []:
                            added.setdefault((x.get("message") or {}).get("threadId"), set())
                    for x in h.get("messagesDeleted") or []:
                        m = x.get("message") or {}
                        deleted.setdefault(m.get("threadId"), set()).add(m.get("id"))
                latest = str(resp.get("historyId") or latest)
                page = resp.get("nextPageToken")
                if not page:
                    break
        except GmailNotFound:
            await self._resync(state)
            return
        await self._refresh_labels()
        previous = _failed_threads(state)
        touched = [t for t in set(added) | set(deleted) if t]
        failed: set = set()
        for tid in touched + [t for t in previous if t not in added and t not in deleted]:
            if not await self._sync_thread_isolated(tid, realtime_ids=frozenset(added.get(tid) or ())):
                failed.add(tid)
            if deleted.get(tid):
                await delete_messages(self.account_id, tid, deleted[tid])
        await self._save_sync_state(
            history_id=latest, last_sync_at=datetime.utcnow().isoformat(),
            failed_threads=_next_failures(previous, set(touched) | set(previous), failed),
        )

    async def _resync(self, state: dict) -> None:
        """The stored historyId is too old (Gmail keeps about a week): re-pull
        threads since the last successful sync, bounded by the backfill window."""
        days = backfill_days()
        try:
            last = datetime.fromisoformat(state.get("last_sync_at") or "")
            days = max(1, min(days, (datetime.utcnow() - last).days + 2))
        except ValueError:
            pass
        logger.info("Gmail history expired for account %s; re-syncing %d days", self.account_id, days)
        await self._backfill(days)

    async def _sync_threads(self, thread_ids: list[str]) -> set:
        """Sync threads concurrently; returns the ids that failed for good.
        A retryable (or auth) error is raised only after every fetch ended,
        so no sync of this pass keeps running behind the next one."""
        results = await asyncio.gather(*(self._sync_thread_isolated(t) for t in thread_ids), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise next((e for e in errors if isinstance(e, GmailAuthError)), errors[0])
        return {t for t, ok in zip(thread_ids, results) if not ok}

    async def _sync_thread_isolated(self, thread_id: str, realtime_ids: frozenset = frozenset()) -> bool:
        """``_sync_thread`` for the sync loop: False (logged) when this one
        thread fails in a way that retrying the whole pass can't fix, so it
        never holds back the cursor; retryable and auth errors propagate."""
        try:
            await self._sync_thread(thread_id, realtime_ids=realtime_ids)
            return True
        except GmailAuthError:
            raise
        except GmailError as e:
            if e.retryable:
                raise
            logger.warning("Gmail thread %s of account %s skipped: %s", thread_id, self.account_id, e)
        except Exception:
            logger.exception("Gmail thread %s of account %s could not be stored", thread_id, self.account_id)
        return False

    # -- threads → normalized shapes --------------------------------------------------------

    async def _sync_thread(self, thread_id: str, realtime_ids: frozenset = frozenset()) -> Optional[int]:

        async with self._sem.get():
            try:
                thread = await self._require_api().get_thread(thread_id)
            except GmailNotFound:
                return None
        conv, msgs = self.normalize_thread(thread)
        if conv is None:
            return None
        live = [m for m in msgs if m.external_id in realtime_ids]
        rest = [m for m in msgs if m.external_id not in realtime_ids]
        res = None
        if rest or not live:
            res = await ingest(self.account_id, self.channel, conv, rest, source="backfill")
        if live:
            res = await ingest(self.account_id, self.channel, conv, live, source="realtime")
        return res.conversation_id

    def _is_self(self, email: str) -> bool:
        return bool(self._self_email) and email == self._self_email

    def _identity(self, name: str, email: str) -> NormalizedIdentity:
        return NormalizedIdentity(external_id=email, display_name=name or None,
                                  is_self=True if self._is_self(email) else None)

    def normalize_thread(self, thread: dict) -> "tuple[Optional[NormalizedConversation], list[NormalizedMessage]]":
        raw = [m for m in thread.get("messages") or [] if not SKIPPED_MESSAGE_LABELS & set(m.get("labelIds") or [])]
        if not raw:
            return None, []
        by_rfc_id = {}
        for m in raw:
            mid = mime.headers_of(m.get("payload") or {}).get("message-id", "")
            found = _MSGID.findall(mid)
            if found:
                by_rfc_id[found[0]] = m["id"]

        msgs = [self.normalize_message(m, by_rfc_id) for m in raw]
        label_ids: set = set()
        for m in raw:
            label_ids.update(m.get("labelIds") or [])
        counterparts: dict[str, str] = {}
        for nm in msgs:
            for ident in ([nm.sender] if nm.sender else []) + [p for p, _ in nm.participants]:
                if not self._is_self(ident.external_id):
                    if not counterparts.get(ident.external_id):
                        counterparts[ident.external_id] = ident.display_name or ""
        peer = None
        if len(counterparts) == 1:
            email, name = next(iter(counterparts.items()))
            peer = NormalizedIdentity(external_id=email, display_name=name or None)
        subject = next((m.subject for m in msgs if m.subject), None) or "(no subject)"
        labels = [SYSTEM_LABELS[l] for l in label_ids if l in SYSTEM_LABELS]
        labels += [self._label_names[l] for l in label_ids if l in self._label_names]
        conv = NormalizedConversation(
            external_id=str(thread["id"]),
            kind="email_thread",
            title=subject,
            peer=peer,
            clear_peer=peer is None,  # a thread that gained a second counterpart is no longer one-to-one
            members_count=len(counterparts) + 1,
            unread_count=sum(1 for m in raw if "UNREAD" in (m.get("labelIds") or [])),
            is_archived="INBOX" not in label_ids or bool(label_ids & HIDDEN_CATEGORIES),
            labels=labels,
            last_message_at=max(m.date for m in msgs),
        )
        return conv, msgs

    def normalize_message(self, m: dict, by_rfc_id: Optional[dict] = None) -> NormalizedMessage:
        payload = m.get("payload") or {}
        h = mime.headers_of(payload)
        body = mime.parse_payload(payload)
        frm = mime.parse_address(h.get("from"))
        sender = self._identity(*frm) if frm else None
        participants = [
            (self._identity(name, email), role)
            for role in ("to", "cc", "bcc")
            for name, email in mime.parse_addresses(h.get(role))
        ]
        label_ids = list(m.get("labelIds") or [])
        reply_to_external_id = None
        parents = _MSGID.findall(h.get("in-reply-to", ""))
        if parents and by_rfc_id:
            reply_to_external_id = by_rfc_id.get(parents[0])
        if m.get("internalDate"):
            millis = int(m["internalDate"])
        else:
            millis = 0
        meta = {k: v for k, v in {
            "rfc822_message_id": (_MSGID.findall(h.get("message-id", "")) or [None])[0],
            "references": h.get("references") or None,
            "in_reply_to": parents[0] if parents else None,
            "reply_to": h.get("reply-to") or None,
            "label_ids": label_ids,
        }.items() if v}
        return NormalizedMessage(
            external_id=str(m["id"]),
            date=datetime.utcfromtimestamp(millis / 1000),
            seq=millis,
            sender=sender,
            sender_name=(frm[0] or frm[1]) if frm else None,
            subject=h.get("subject") or None,
            text=body.text if body.text is not None else (m.get("snippet") or None),
            text_html=body.html,
            is_outgoing="SENT" in label_ids or (frm is not None and self._is_self(frm[1])),
            reply_to_external_id=reply_to_external_id,
            participants=participants,
            attachments=[
                NormalizedAttachment(kind=mime.attachment_kind(a.mime_type), filename=a.filename,
                                     mime_type=a.mime_type, size=a.size, external_ref=a.external_ref)
                for a in body.attachments
            ],
            meta=meta,
        )

    # -- send / attachments ---------------------------------------------------------------------------

    async def modify_thread(self, conversation: Any, *, add: list[str] = (), remove: list[str] = ()) -> None:
        """Change a thread's labels at Gmail (archive = remove INBOX, read =
        remove UNREAD) and re-sync it so the conversation reflects them."""
        await self._require_api().modify_thread(conversation.external_id, list(add), list(remove))
        await self._sync_thread(conversation.external_id)

    async def send_new(self, draft: OutgoingMessage) -> "tuple[int, list[int]]":
        """Start a new thread; returns (conversation id, stored message ids)."""
        to = _dedupe([a.strip().lower() for a in draft.to])
        cc = _dedupe([a.strip().lower() for a in draft.cc], also=set(to))
        if not to:
            raise ValueError("No recipient: add an address in To")
        raw = await asyncio.to_thread(  # reads and encodes the attachments
            mime.build_raw,
            sender=self._self_email or "", to=to, cc=cc, subject=(draft.subject or "").strip(), text=draft.text,
            attachments=[mime.OutgoingAttachment(f.path, f.filename, f.mime_type) for f in draft.files],
        )
        sent = await self._require_api().send(raw, None)
        return await self._store_sent(sent, str(sent.get("threadId") or ""))

    async def _store_sent(self, sent: dict, thread_id: str) -> "tuple[int, list[int]]":

        conv_id = await self._sync_thread(thread_id)
        if conv_id is None:
            raise ValueError("The message was sent but could not be loaded back")
        async with AsyncSessionLocal() as db:
            ids = await crud.message_ids_by_external(db, conv_id, [str(sent["id"])])
        return conv_id, list(ids.values())

    async def send(self, conversation: Any, draft: OutgoingMessage) -> list[int]:
        """Reply in a thread. ``draft.to`` non-empty is the complete To list
        (the Mail composer shows the recipients); empty = reply to the
        sender (Reply-To), or to your own recipients when answering yourself."""

        api = self._require_api()
        async with AsyncSessionLocal() as db:
            if draft.reply_to_message_id is not None:
                target = await crud.get_message(db, draft.reply_to_message_id)
            else:
                target = (await db.execute(
                    select(Message).where(Message.conversation_id == conversation.id)
                    .order_by(Message.seq.desc()).limit(1)
                )).scalar_one_or_none()
            parts = (await crud.message_participants(db, [target.id])).get(target.id, []) if target else []

        to: list[str] = []
        meta: dict = {}
        if target is not None:
            meta = json.loads(target.meta_json) if target.meta_json else {}
        if draft.to:
            pass
        elif target is not None:
            if target.is_outgoing:
                to = [p["external_id"] for p in parts if p["role"] == "to"]
            else:
                reply_to = mime.parse_address(meta.get("reply_to"))
                sender = next((p["external_id"] for p in parts if p["role"] == "from"), None)
                to = [reply_to[1]] if reply_to else ([sender] if sender else [])
        to = _dedupe(to + [a.strip().lower() for a in draft.to], exclude=self._self_email)
        cc = _dedupe([a.strip().lower() for a in draft.cc], exclude=self._self_email, also=set(to))
        if not to:
            raise ValueError("No recipient: add an address in To")

        subject = draft.subject
        if not subject:
            base = (target.subject if target is not None else None) or conversation.title or ""
            subject = base if re.match(r"^\s*re:", base, re.I) else f"Re: {base}".strip()
        rfc_id = meta.get("rfc822_message_id")
        raw = await asyncio.to_thread(  # reads and encodes the attachments
            mime.build_raw,
            sender=self._self_email or "",
            to=to,
            cc=cc,
            subject=subject,
            text=draft.text,
            in_reply_to=rfc_id,
            references=meta.get("references") if rfc_id else None,
            attachments=[mime.OutgoingAttachment(f.path, f.filename, f.mime_type) for f in draft.files],
        )
        sent = await api.send(raw, conversation.external_id if target is not None else None)
        _, ids = await self._store_sent(sent, str(sent.get("threadId") or conversation.external_id))
        return ids

    async def fetch_attachment(self, attachment: Any, message: Any, conversation: Any) -> str:

        api = self._require_api()
        part_id, _, att_id = (attachment.external_ref or "").partition(":")
        data: Optional[bytes] = None
        if att_id:
            try:
                data = mime.b64url_decode((await api.get_attachment(message.external_id, att_id))["data"])
            except GmailError as e:
                if e.retryable:
                    raise
        if data is None:
            # Gmail re-issues attachment ids; find the part again by its id.
            try:
                full = await api.get_message(message.external_id)
            except GmailNotFound:
                raise MediaNotFound("The email no longer exists")
            part = mime.find_part(full.get("payload") or {}, part_id)
            body = (part or {}).get("body") or {}
            if body.get("data"):
                data = mime.b64url_decode(body["data"])
            elif body.get("attachmentId"):
                data = mime.b64url_decode((await api.get_attachment(message.external_id, body["attachmentId"]))["data"])
            else:
                raise MediaNotFound("The attachment no longer exists")

        rel = f"media/{conversation.id}/{attachment.id}{_extension(attachment.filename, attachment.mime_type)}"
        await asyncio.to_thread(_write_file, Path(config.DATA_DIR) / rel, data)
        async with AsyncSessionLocal() as db:
            await db.execute(update(MessageAttachment).where(MessageAttachment.id == attachment.id)
                             .values(size=len(data)))
            await crud.set_attachment_local_path(db, attachment.id, rel)
        return rel


def _failed_threads(state: dict) -> dict:
    failed = state.get("failed_threads")
    return dict(failed) if isinstance(failed, dict) else {}


def _next_failures(previous: dict, attempted: set, failed: set) -> dict:
    """The ``failed_threads`` sync state after a pass: attempts per thread
    that still fails; threads that synced are dropped, and so is one that
    failed ``THREAD_RETRY_LIMIT`` times."""
    out = {t: n for t, n in previous.items() if t not in attempted}
    for t in failed:
        n = int(previous.get(t) or 0) + 1
        if n >= THREAD_RETRY_LIMIT:
            logger.warning("Gmail thread %s failed %d times; giving up on it", t, n)
            continue
        out[t] = n
    return dict(list(out.items())[-MAX_FAILED_THREADS:])


def _write_file(path: Path, data: bytes) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, data, 0o600)


def _dedupe(addrs: list[str], *, exclude: Optional[str] = None, also: frozenset = frozenset()) -> list[str]:
    out: list[str] = []
    for a in addrs:
        if a and "@" in a and a != exclude and a not in out and a not in also:
            out.append(a)
    return out


def _extension(filename: Optional[str], mime_type: Optional[str]) -> str:
    ext = Path(filename or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        ext = (mimetypes.guess_extension(mime_type or "") or "").lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else ""
