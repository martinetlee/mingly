# Mingly → multi-channel, person-centric communications app

## Context
Mingly ("Common Defense") is a Telegram-only client. Telegram's numeric ids *are* the app's identity model: `messages.chat_id` = `chats.telegram_id` (no FK), a "person" = a Telegram user id (`contacts`, `contact_positions`, `person_notes`, `scam_flags`), ~212 `telegram_id` refs in `backend/db/crud.py`, ~50 frontend components + routes + `chatUiStore` + WS events keyed by `telegram_id`/`tg_message_id`. Onboarding = Telegram login gates the whole app.

Goal: add Signal and Gmail first (Google Calendar next, SMS source TBD — abstraction must allow it), in a **unified, person-centric** UX where one person merges their Telegram/Signal/email/SMS identities, and **all features** (briefings, Shield, catch-up, LLM/org context, MCP, cloud sync) work on every channel.

Decisions made: internal surrogate integer ids · bring-your-own Google OAuth client (personal/small group) · signal-cli installed externally via brew.

## Architecture

### Data model (internal ids, external ids as TEXT)
- `accounts(id, channel, label, status, creds_ref→vault key)`
- `conversations(id, account_id FK, external_id TEXT, kind dm|group|channel|email_thread, title, unread, last_message_at, labels_json, tg_meta_json; UNIQUE(account_id, external_id))`
- `messages(id, conversation_id FK, external_id TEXT, sender_identity_id FK, subject, body_text, body_html, date, reply_to_message_id, extras_json (topic_id, grouped_id, fwd…), scam_* cols; UNIQUE(conversation_id, external_id))`
- `people(id, display_name, …)` · `identities(id, person_id FK, channel, handle TEXT, display_name, username, avatar, profile_json; UNIQUE(channel, handle))` · `contact_points(identity_id, kind phone|email, value_normalized)`
- `conversation_members`, `message_participants(message_id, identity_id, role from|to|cc|bcc)`, `message_attachments(message_id, external_ref, filename, mime, size, local_path)`, `person_merge_suggestions`, `person_merges` (undo log), `sync_cursors(account_id, conversation_id?, cursor_json)` (replaces Telegram-monotonic `chat_history_coverage`).
- **Name collision (verified):** existing AI-chat tables `conversations`/`conversation_messages` (`models.py:308,317`) and route `/api/conversations` (`api/router.py:132`) → rename to `ai_conversations*` and `/api/ai/conversations` first.

### Connector layer — `backend/connectors/` (modeled on `llm/base.py` + `llm/registry.py`)
- `Connector` ABC: `channel`, `capabilities` (SEND, EDIT, DELETE, REACT, REPLY, MEDIA_SEND, READ_RECEIPTS, FORUM_TOPICS, ADMIN_MODERATION, HISTORY_BACKFILL, SUBJECT, HTML_BODY, LABELS, TYPING, PRESENCE); `start/stop/status`; `begin_auth/continue_auth` returning a generic `AuthStep(kind: fields|qr|oauth_browser|done|error)` — covers Telegram phone→code→2FA, Google OAuth loopback, Signal QR; `sync(since, scope)`; `send(conv, draft)`; `fetch_media(msg, attachment_id)`; `conversation_capabilities(conv)`; `extra_router()` for channel-specific APIs.
- `ConnectorManager` (`connectors/manager.py`): one instance per `accounts` row, vault lock/unlock listeners, generic `connector_status` WS event (keep `telegram_status` alias one release). Replaces direct `TelegramRuntime` wiring in `main.py`.
- `IngestService` (`backend/ingest/service.py`) = the **only writer**: connectors emit `NormalizedConversation/Message/Identity`; service resolves ids, persists, then runs fan-out hooks (`ingest/fanout.py`) extracted from `telegram/sync.py:566-780`: catch-up reset, WS broadcast, scam enqueue + `note_sender_profile`, native notification (moved to `backend/notify.py`, realtime only, per-channel prefs), identity auto-link.
- Telegram-only features (forum topics, moderation/admin, archive, profiles) move under `/api/telegram/...`, capability-gated (409 otherwise).

### People & identity linking — `backend/people/linking.py`
- Auto-merge only on exact normalized phone (E.164, `phonenumbers`) or email, and only if exactly one existing person matches; otherwise a suggestion.
- Suggestions (never auto): same name + shared group, username≈email local-part.
- Manual merge / split / detach APIs; merges move identities, positions, notes, tags; undoable via `person_merges`.
- Scam flags attach to **identity** (per-channel), rolled up to person in UI.

### Gmail — `backend/connectors/gmail/`
- Auth: user enters own Desktop OAuth client id/secret (vault); loopback redirect to `127.0.0.1:{port}/api/connectors/gmail/oauth/callback`, PKCE + state; scopes `gmail.modify`, `gmail.send` (+ `calendar.readonly` later via incremental auth). Refresh token in vault (`backend/secrets.py`).
- Thread = conversation, Gmail message = message; from/to/cc → `message_participants`; attachments lazy.
- Sync: backfill `threads.list newer_than:90d` (configurable) → store `historyId`; incremental `history.list` poll 60s focused / 5m background; 404 → bounded re-sync; 429 backoff.
- Labels → `labels_json`; unified inbox shows INBOX/Primary only by default (Promotions/Social/Updates/Spam hidden).
- Security: server-side `nh3` sanitize + sandboxed `iframe srcdoc` with CSP; remote images blocked by default.
- Send/reply via MIME with `In-Reply-To`/`References` + `threadId`; archive & mark-read.

### Signal — `backend/connectors/signal/`
- Detect `signal-cli` via `shutil.which` + `/opt/homebrew/bin`, `/usr/local/bin` (packaged app PATH lacks brew); version + Java check; Connections page shows `brew install signal-cli` if missing.
- Link: `signal-cli --config DATA_DIR/signal link -n Mingly` → `sgnl://linkdevice` URI → QR `AuthStep` (poll).
- Runtime: `jsonRpc` over stdio subprocess, supervised with backoff restart; receive data/sync/edit/delete/reaction/receipt envelopes; groups via `listGroups`; attachments copied into `data/media/signal/...`; send with quotes/attachments.
- Limits to surface in UI: no history before linking; signal-cli keys live in `DATA_DIR/signal` (0700) outside the Fernet vault.

### Features made channel-agnostic
- **Briefing** (`briefing/pipeline.py`): consumes conversations/messages by internal id; channel + subject in prompt; per-channel inclusion (email = Primary by default). `sources_json` → `{conversation_id, message_id}`.
- **Shield** (`scam/queue.py`, `pipeline.py`, `service.py`): targets `(conversation_id, message_ids)`, sender = identity; `channel` in pipeline input; add email phishing signals (sender-domain mismatch, link targets, attachment types); stage gating per channel.
- **Cloud sync** (`scam/cloud_sync.py` + `../mingly-cloud`): contract v2 (`X-Mingly-Contract: 2`, `{channel, sender_handle_hash, …}`), dual-send legacy tg fields one release; deploy cloud side first; email content upload opt-in, default off.
- **Catch-up**: keyed by conversation_id; email "needs reply" = last msg from other party and you're in to/cc.
- **MCP tools** (`api/mcp_tools.py`): take `conversation_id/person_id/message_id`, `channel` filter, `get_person` returns merged identities (breaking change; document).
- **Search** (`api/search.py`): across subject + body, all channels.

### Frontend
- `api/types.ts` / `client.ts`: `Conversation{id, account_id, channel, kind, capabilities[]}`, `Message{id, conversation_id, sender{identity_id, person_id, name}, subject?, body_html?, attachments[]}`, `Person{id, identities[]}`; routes `/api/conversations/{id}`, `/api/people/{id}`, `/api/accounts`, `/api/telegram/...`; `chatUiStore` + WS events keyed by internal ids.
- Boot (`App.tsx`): vault → LLM setup → app always; no accounts → empty state linking to Connections. `Onboarding.tsx` Telegram steps become a generic `AuthFlow` renderer (fields / QR / browser-OAuth+poll).
- New `pages/settings/Connections.tsx`: list/add/re-auth/remove accounts, Google client entry, signal-cli detection.
- Unified inbox: channel badge + filter chips, optional group-by-person; `EmailMessageView` (header, sandboxed body, attachments); compose gets subject/cc for email; `MessageBubble`/`MediaView` take `messageId`/`attachmentId`.
- Capability gating for `ForumTopicsPanel`, `ModerationPanel`, admin, reactions, edit — applied uniformly everywhere these appear.
- Person profile (`PersonCard`, `ContactInfoSection`): linked identities with channel icons, merge suggestions, merge/split dialogs (reuse `MergeOrgDialog.tsx` pattern).

## Data migration (existing user DBs)
**One schema cutover; code phased around it** (no dual-write — 212 crud refs would double the bug surface; single-user local SQLite, file backup is a clean rollback).
- Runner (`backend/db/migrations/runner.py`): allow Python-callable steps alongside SQL (each version stays in its own BEGIN/COMMIT, `runner.py:629`); before applying any version ≥120 on a non-fresh DB, WAL checkpoint + `sqlite3.backup()` to `DATA_DIR/backups/mingly-pre-v{N}-{ts}.db`, keep last 3, show path on failure.
- New area `migrations/areas/channels.py`, range **120–159**, registered in `areas/__init__.py`:
  - 120 rename AI chat tables · 121 create new tables · 122 accounts row + `chats`→`conversations` (temp `_tgmap`) · 123 `contacts`+senders → `people`/`identities`/`contact_points` (temp `_tgpeople`) · 124 rebuild `messages` (12-step SQLite rebuild; media paths kept as-is in `message_attachments.local_path`, no file moves) · 125–130 re-key `chat_tags`, `workspace_chat_overrides`, `agent_reviews`, `briefing_entries`, `catchup_*`, `contact_positions`, `person_notes`, `scam_*`, coverage→`sync_cursors` · 131 rewrite JSON blobs (`sources_json`, `citations_json`, `related_telegram_ids_json`; unmappable → `{legacy:…}`) · 132 drop temp maps, ANALYZE.
- Orphans (rows pointing at deleted chats) are logged and preserved, never abort the migration.

## Git setup (before any code)
- **All work happens on a NEW branch `feature/multi-channel`, created from `origin/main`.** Nothing is committed to `app-review-fixes`.
- Pre-check done: there are no uncommitted changes to commit (working tree clean), and everything on the currently checked-out `app-review-fixes` is already merged into `origin/main` (PR #118), so branching from `origin/main` loses nothing.
- `git fetch && git switch -c feature/multi-channel origin/main`. Commit at the end of each phase (and sub-steps within Phase 1) on this branch.

## Phases (each ends buildable + tested)

Status (2026-10-10): all phases implemented on `feature/multi-channel` — 0 `8264ead`, 1 `57c5dac`, 2–4 `a1e5ed8`, 5 (Calendar, SMS, Shield cloud v2 client) in the following commit. UI follows the hybrid layout in `PHASES_2_5.md`.
0. **Refactor, old schema, no behavior change** — `Connector` ABC, `ConnectorManager`, `TelegramConnector` wrapping `TelegramRuntime`; extract `IngestService` + fan-out from `telegram/sync.py`; generic `/api/accounts/{id}/auth` with `/api/auth` as wrapper; runner callable steps + backups; golden tests pinning current ingest behavior (rows, WS events, enqueues).
1. **Schema cutover (Telegram-only still)** — migrations 120–132, models/crud rewrite, AI-conversation rename, routers re-keyed, WS events, MCP tools, frontend ids, cloud contract v2 (cloud first). Freeze other feature work during this phase.
2. **People + Connections** — linking service, merge/split API+UI, Connections page, boot change, capability gating, `/api/telegram/*` mounts.
3. **Gmail** — OAuth, backfill + history polling, sanitize, email view/compose, attachments, label filters, email Shield stage, notification prefs.
4. **Signal** — detection, QR link, jsonRpc supervisor, send/receive, groups, attachments, phone linking.
5. **Calendar + hardening** — `calendar_events` table (attendees→identities) feeding person timeline + briefings; drop legacy cloud fields; `SmsConnector` once source is chosen; update `CLAUDE.md`/README.

New deps: `google-auth-oauthlib`, `google-api-python-client`, `nh3`, `phonenumbers`, `html2text` (backend); `qrcode.react` (frontend). Add to `requirements.txt` + `mingly.spec` hidden imports.

## Key risks
1. Phase-1 migration on real DBs (negative marked ids, senders without contacts, orphans) → backup, legacy fallbacks, fixture test, dry run on a copy of the real DB.
2. Wide Phase-1 release (crud, ~20 routers, MCP, frontend) → feature freeze during it.
3. Email volume flooding inbox/briefings/LLM cost/scam queue → Primary-only defaults, per-channel opt-in.
4. Signal fragility (Java, protocol breaks, no history, keys outside vault).
5. Wrong auto-merges mixing people/scam flags → verified-only auto-merge, undo log.
6. Cross-repo cloud contract coordination with `mingly-cloud`.

## Verification
- Every phase: `python3 -m pytest -p no:deepeval -q tests backend`; pyflakes/mypy gates from `build_app.sh`; `cd frontend && npm run typecheck && npx vitest run && npm run build`.
- Phase 0: golden ingest tests pass unchanged before/after extraction; app runs, live Telegram message appears + notifies.
- Phase 1: migration test from a v119 fixture DB (row counts, map completeness, JSON round-trip, no orphan FKs); run migration on a **copy** of the real `data/mingly.db` and click through chats, profile, briefing, catch-up, Shield, MCP; confirm backup file created.
- Phase 2: merge/split/undo round-trip tests; Connections page adds/removes Telegram account.
- Phase 3: Gmail with own OAuth client — backfill, new mail arrives via history poll, reply threads correctly in Gmail web, malicious HTML fixture is sanitized, remote images blocked.
- Phase 4: link a Signal device via QR, send/receive DM + group message, attachment, phone-match auto-links to the Telegram person.
- Final: `./build_app.sh` packaged app launches and each connector works from the `.app` (PATH/brew detection).
