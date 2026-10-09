# Phases 2–5 — contract for the remaining work

Builds on `SCHEMA_V2.md` (still authoritative for ids, payloads, routes).

## Shared infrastructure (done, use it)

- **Tables** (`backend/db/models.py`): `message_participants`, `person_merge_suggestions`,
  `person_merges`, `calendar_events`, `calendar_attendees`; columns
  `accounts.sync_state_json`, `conversations.labels_json` (migration 1).
- **Connector contract** (`backend/connectors/base.py`): `multi_account`,
  `account_id` / `account_key` (`"<channel>:<account_id>"`, Telegram keeps
  `"telegram:default"`), `send(conversation, OutgoingMessage) -> [message ids]`,
  `fetch_attachment(attachment, message, conversation) -> rel path`, `sync_now()`,
  `remove()`, `UnsupportedOperation`.
- **Manager** (`backend/connectors/manager.py`): `register_factory(channel, name, factory)`,
  `load_accounts()`, `for_account(channel, account_id)`, sign-in of a new account via
  `begin_auth("<channel>")` on a provisional connector that must create its
  `accounts` row (set `self.account_id`) before returning the `done` step; the
  manager then registers and starts it. `remove(key)` deletes the account row
  (cascade). Channel packages expose `register(manager)` and are wired in
  `_build_manager` (`backend/connectors/gmail/__init__.py`, `signal/__init__.py`).
- **Accounts API** (`backend/api/accounts.py`): `GET /api/accounts` →
  `{accounts: [describe()], channels: [{channel, display_name, multi_account}]}`;
  `POST /api/accounts/{key}/auth`, `POST /api/accounts/{key}/auth/{flow_id}`,
  `POST /api/accounts/{key}/logout`, `DELETE /api/accounts/{key}`.
- **Ingest** (`backend/ingest/normalized.py`, `backend/ingest/service.py`):
  connectors translate to `NormalizedConversation` / `NormalizedMessage` /
  `NormalizedIdentity` and call `ingest(account_id, channel, conversation, messages,
  source="realtime"|"backfill")`; `delete_messages(...)`; `message_participants(db, ids)`.
  Identities → people; `crud.upsert_identity` calls
  `backend.people.linking.on_contact_points_added` when a phone/email point appears.
- **Compose / media dispatch**: `/api/compose/send` and `/send-files` for a
  non-Telegram conversation call the connector's `send` (body adds `subject`, `to`,
  `cc`); `/api/media-download/{attachment_id}` calls `fetch_attachment`.
- **OAuth redirect router**: `backend/connectors/gmail/routes.py` mounted at
  `/api/connectors/gmail` WITHOUT the session token (authenticate by flow `state`).

## Phase 2 — people linking + Connections

- `backend/people/linking.py`:
  - `on_contact_points_added(db, identity)`: if exactly one *other* person owns an
    identity with the same normalized phone (E.164 via `phonenumbers`) or email,
    merge the identity's person into it automatically (`PersonMerge.automatic=True`);
    if several match, create suggestions instead. Never merge a person with
    `is_self` into another person or vice versa unless both are self.
  - Suggestions (never automatic): same display name + a shared group conversation;
    a username equal to an email local part. Stored in `person_merge_suggestions`
    (`person_a_id < person_b_id`).
  - `merge(db, into, from, automatic)` moves identities, positions, notes, catch-up
    and briefing person links, fills empty CRM fields of `into` from `from`, keeps
    `is_important` if either has it, records a `PersonMerge` snapshot, deletes `from`.
    `undo(db, merge_id)` recreates the person (new id) and moves its rows back.
    `detach(db, identity_id)` moves one identity to a new person.
- API (`backend/api/people.py` area): `GET /api/people/merge-suggestions`,
  `GET /api/people/{id}/merge-suggestions`, `POST /api/people/{id}/merge {other_person_id}`
  → surviving Person, `POST /api/people/merges/{merge_id}/undo`,
  `POST /api/people/{id}/identities/{identity_id}/detach` → new Person,
  `POST /api/people/merge-suggestions/{id}/dismiss`. Broadcast `person_changed`.
- Connections: Settings → Connections page lists accounts + connectable channels,
  runs any channel's sign-in through one generic step renderer (`fields`, `qr`, `progress`,
  `oauth_browser` + poll, `done`, `error`), re-auth, log out, remove. Boot: vault →
  LLM setup → app; no account at all → empty state pointing to Connections. The
  Telegram-only onboarding and `/api/auth` Telegram routes (`setup`, `send-code`,
  `verify-*`, `has-credentials`, `status`, `logout`) are replaced by the generic flow.

## Phase 3 — Gmail (`backend/connectors/gmail/`)

- REST over `httpx` (no Google client libraries). Bring-your-own Desktop OAuth
  client: sign-in steps `fields` (client id / secret) → `oauth_browser` (auth URL with
  PKCE + state, redirect `http://127.0.0.1:<API_PORT>/api/connectors/gmail/oauth/callback`)
  polling until the callback exchanged the code → `done`. Scopes
  `gmail.modify`, `gmail.send`, `calendar.readonly`, `userinfo.email`. Client
  credentials + refresh token in the vault (`backend.secrets`), per account.
- Sync: backfill threads `newer_than:90d` (pref), store `historyId` in
  `accounts.sync_state_json`; `history.list` polling (60 s while running); 404 →
  bounded re-sync; 429/5xx backoff. Thread = conversation (`email_thread`, peer =
  the single counterpart when there is exactly one), message = message with
  `subject`, sanitized `text_html` (nh3 allowlist; remote images rewritten to a
  blocked placeholder unless allowed), `text` (html2text or text/plain),
  participants from/to/cc, attachments (lazy, `external_ref` = Gmail attachment id).
  Labels → `labels_json`; the inbox shows INBOX threads only (others hidden by
  `is_archived`), Promotions/Social/Updates/Forums excluded by default.
- Send / reply: MIME with In-Reply-To / References + threadId; attachments.
- Capabilities: SEND, REPLY, SUBJECT, HTML_BODY, LABELS, MEDIA_SEND, HISTORY_BACKFILL.
- Notifications: per-channel preference, email default off.
- Frontend: email message view (header from/to/cc/subject, sandboxed iframe for
  HTML, attachment list, "load remote images" per sender), compose subject/cc for
  email conversations, label filter.

## Phase 4 — Signal (`backend/connectors/signal/`)

- signal-cli: the app-managed install (`signal/install.py`: pinned signal-cli +
  Temurin JRE, SHA-256 and size verified while streaming, safe extraction, run
  check, atomic placement under `DATA_DIR/signal-runtime/`) is preferred; else
  Homebrew's (`shutil.which` + `/opt/homebrew/bin`, `/usr/local/bin`). Missing or
  broken → a `fields` step "Set up / Repair Signal support" (`submit_label`), then a
  `progress` step (`progress` 0–1, polled) while it installs, then the QR step in
  the same flow.
- Link: `signal-cli --config <DATA_DIR>/signal link -n Mingly` → `qr` step with the
  `sgnl://linkdevice` URI, poll until linked → account row (`external_id` = number).
- Runtime: `signal-cli -a <number> jsonRpc` subprocess over stdio, supervised
  (backoff restart); receive envelopes (data, sync-sent, edits, remote deletes,
  reactions, receipts) → `ingest(..., source="realtime")`; groups via `listGroups`;
  attachments copied from signal-cli's attachment dir; send / reply with quotes.
  Identity external_id = ACI uuid, phone → contact point (links to Telegram people).
  No history before linking. Key dir `DATA_DIR/signal` mode 0700.

## Phase 5 — Calendar, cloud contract, SMS

- Google Calendar on the Gmail account's OAuth (same scopes): sync events into
  `calendar_events` / `calendar_attendees`; person page shows upcoming/recent
  meetings; briefings include today's meetings with the people involved.
- Shield cloud contract v2 (client side; the server is not in this workspace):
  documented payload with `channel` and hashed handles, sent only when the server
  advertises v2; v1 Telegram payload otherwise.
- SMS: connector skeleton for macOS Messages (`~/Library/Messages/chat.db`, needs
  Full Disk Access) behind detection; read-only ingest.

## UI layout decision (hybrid) — 2026-10-10

Media-specific spaces for reading/writing, unified views where combining adds value:

- **Chats** (`/chats`): real-time messengers only (Telegram, Signal, later SMS) —
  `kind != email_thread`. Channel filter chips (All + one per connected messenger).
- **Mail** (`/mail`): its own email client — account switcher (several Gmail
  accounts), folders (Inbox, Sent, Archive/All, labels), thread list (subject,
  participants, snippet, date, unread), reading pane rendering the thread with
  `EmailMessageView`, reply / reply-all / compose with subject, to, cc, attachments,
  archive and mark read/unread. Backend: `GET /api/conversations?kind=email_thread
  &account_id=&label=` (+ `exclude_kind` for Chats).
- **Person page**: cross-channel timeline — messages of the person's one-to-one
  conversations, emails they're a participant in, and (Phase 5) meetings, newest
  first, each linking to its Chats / Mail location. `GET /api/people/{id}/timeline`.
- **Home / Catch-up / Briefings / global search**: stay cross-channel; items link
  to the right space (an email thread opens in Mail, a chat in Chats).
