# Schema v2 — multi-channel, person-centric

Contract for the Phase 1 cutover (see the plan in `docs/multichannel/PLAN.md`).
Every backend module, API route, WebSocket event and frontend type follows this
document. Where this doc and old code disagree, this doc wins.

## Principles

1. **Internal ids everywhere.** Every row has a dense integer `id`. APIs, WS
   events, JSON blobs, URLs and the frontend only ever carry internal ids:
   `conversation_id`, `message_id`, `person_id`, `identity_id`, `account_id`.
2. **Channel ids are data, never keys.** A channel's own id (Telegram peer id,
   Gmail thread id, Signal UUID) lives in an `external_id TEXT` column and is
   only read by that channel's connector code (`backend/telegram/…`).
3. **Real foreign keys** with explicit `ondelete`. `PRAGMA foreign_keys=ON`.
4. **Fresh schema.** No legacy migrations. A pre-v2 database is backed up to
   `DATA_DIR/backups/` and replaced on first start (see "Database lifecycle").
   Telegram history re-syncs from Telegram.
5. **Person-centric.** A *person* is the CRM entity; an *identity* is one
   handle on one channel. Every identity belongs to exactly one person.

## Database lifecycle

- `schema_meta(key TEXT PK, value TEXT)` holds `schema_generation = '2'`.
- `init_db()`: if the DB file exists and is not generation 2 (no
  `schema_meta`, or it has the pre-v2 `chats` table), copy it to
  `DATA_DIR/backups/mingly-pre-v2-<ts>.db` (sqlite online backup), delete the
  original (+ `-wal`/`-shm`), and create a fresh DB. Log the backup path.
- Fresh DB: `create_all`, then stamp generation 2, then run v2 migrations
  (`backend/db/migrations/runner.py`, list starts empty at version 1) and seed
  default tags.
- Media under `DATA_DIR/media/` from v1 is left on disk untouched (orphaned;
  re-downloaded on demand).

## Tables

Names in **bold** are new; `→` shows the v1 origin.

### Channels & accounts

**accounts**
| col | type | notes |
|---|---|---|
| id | int PK | |
| channel | str NOT NULL | `telegram` \| `gmail` \| `signal` \| `sms` |
| external_id | str NULL | own id on the channel (Telegram user id, email address, phone) once signed in |
| label | str NULL | display: `@ann`, `ann@gmail.com` |
| self_identity_id | FK identities NULL SET NULL | "me" on this account |
| created_at | datetime | |
UNIQUE(channel, external_id)

### People

**people** (→ contacts, the CRM record)
| col | notes |
|---|---|
| id PK | |
| display_name | user-set override, NULL = derive from identities |
| twitter_handle, linkedin_url, notes, is_important | from contacts |
| relationship_status, relationship_status_manual | from contacts |
| last_interaction_at, last_outgoing_at, last_incoming_at | from contacts |
| is_self | bool, the user themself |
| created_at, updated_at | |

Derived (column_property / query): `name` = display_name or best identity
name; `is_contact` = any identity `is_contact`.

**identities** (→ contacts' Telegram fields + message senders)
| col | notes |
|---|---|
| id PK | |
| person_id | FK people NOT NULL, CASCADE |
| channel | str NOT NULL |
| external_id | str NOT NULL — Telegram user id as decimal string; lowercased email; Signal ACI |
| display_name, first_name, last_name, username, phone, bio | channel profile |
| avatar_path | relative to DATA_DIR (`media/avatars/identities/<id>.jpg`) |
| is_contact | in the channel's address book |
| is_bot, is_deleted, is_self | |
| synced_at, created_at, updated_at | |
UNIQUE(channel, external_id). Index(person_id).

**contact_points**: id, identity_id FK CASCADE, kind (`phone`\|`email`),
value (E.164 / lowercased email). UNIQUE(identity_id, kind, value);
Index(kind, value). Used for cross-channel linking (Phase 2).

### Conversations & messages

**conversations** (→ chats)
| col | notes |
|---|---|
| id PK | |
| account_id | FK accounts NOT NULL CASCADE |
| external_id | str NOT NULL — Telegram: marked peer id as decimal string |
| kind | `direct` \| `group` \| `channel` \| `email_thread` (→ type; `private`→`direct`) |
| title | (→ name) |
| username | public handle if any |
| avatar_path | relative to DATA_DIR (`media/avatars/conversations/<id>.jpg`); direct conversations fall back to the peer identity's avatar |
| peer_identity_id | FK identities NULL SET NULL — the other party of a `direct` conversation |
| is_monitored, is_hidden, is_briefing, is_tracked, is_pinned, is_archived | flags as v1 |
| is_muted, muted_until, is_marked_unread, unread_count, members_count | as v1 |
| is_forum, slow_mode_seconds, is_deactivated, is_self, is_bot, is_deleted_account | as v1 (generic-enough flags) |
| pinned_message_id | FK messages NULL SET NULL (`use_alter`) |
| last_message_at, last_message_text, last_message_sender | (→ last_message_date …) |
| last_activity_at, last_synced_at, created_at | |
UNIQUE(account_id, external_id). Derived: `history_complete` (column_property
over history_coverage).
A **one-to-one** conversation (`crud.one_to_one_filter`) is a `direct` chat or an
`email_thread` with a single counterpart (`peer_identity_id` set); those are a
person's conversations for relationship status, needs-reply and the person page.

**messages**
| col | notes |
|---|---|
| id PK | |
| conversation_id | FK conversations NOT NULL CASCADE |
| external_id | str NOT NULL — Telegram msg id as decimal string |
| seq | bigint NOT NULL — channel-native ordering within the conversation (Telegram msg id; others: epoch ms) |
| sender_identity_id | FK identities NULL SET NULL (NULL for channel posts / service / anonymous) |
| sender_name | denormalized display |
| subject | NULL (email) |
| text, text_html | |
| date, is_outgoing, is_edited, is_pinned, kind, meta_json, fwd_from_name, delivery_state | as v1 |
| reply_to_external_id | str NULL — channel id of the replied-to message (may not be stored) |
| topic_id, grouped_id | as v1 |
| reactions_json, my_reaction | as v1 |
| importance_score, importance_reason, user_flagged | as v1 |
| scam_* | all v1 scam columns unchanged |
| created_at | |
UNIQUE(conversation_id, external_id). Index(conversation_id, seq),
Index(conversation_id, date, id), Index(date, id), Index(sender_identity_id),
Index(scam_status).

Media moves off `messages`:

**message_attachments**: id, message_id FK CASCADE, position int, kind
(`photo`\|`video`\|`video_note`\|`voice`\|`audio`\|`gif`\|`document`\|`sticker`),
filename, mime_type, size, local_path (relative to DATA_DIR, NULL until
downloaded), external_ref (channel handle to fetch it; NULL for Telegram,
which refetches by message), created_at. UNIQUE(message_id, position).

**history_coverage** (→ chat_history_coverage): conversation_id PK FK CASCADE,
history_complete, oldest_seq, newest_contiguous_seq, walk_cursor_seq,
last_walk_at, last_topup_at, error, attempts.

### Organisation & tagging (re-keyed)

- tags, workspaces, workspace_tags, workspace_boards, workspace_columns,
  organizations: unchanged (+ real FKs where they reference each other).
- **conversation_tags** (→ chat_tags): conversation_id FK CASCADE, tag_id FK CASCADE.
- **workspace_conversation_overrides** (→ workspace_chat_overrides): workspace_id FK, conversation_id FK CASCADE, needs_triage_since, triaged_at.
- **person_positions** (→ contact_positions): person_id FK CASCADE, organization_id FK NULL, organization, title, is_active, ended_at. UNIQUE(person_id, organization_id).
- person_notes: person_id FK CASCADE; `citations_json` = `[{conversation_id, message_id}]`.

### Intelligence

- **ai_conversations / ai_conversation_messages** (→ conversations / conversation_messages): the LLM chat history.
- catchup_items: conversation_id FK CASCADE UNIQUE, person_id FK NULL SET NULL (→ contact_id).
- catchup_summaries: conversation_id FK CASCADE UNIQUE; message JSON blobs carry `message_id`.
- briefing_runs unchanged; briefing_entries: conversation_id FK NULL SET NULL, person_id FK NULL SET NULL (set for `important_contact` entries, whose conversation_id is NULL), `sources_json` = `[{conversation_id, message_id}]`.
- agent_reviews: conversation_id PK FK CASCADE, reviewed_through_seq, reviewed_message_count, reviewed_at, agent_label.
- **admin_action_log** (model now): conversation_id FK CASCADE, action, target_identity_id FK NULL, details, created_at.

### Shield (scam)

- scam_flags: identity_id FK CASCADE UNIQUE (→ telegram_id), flagged_conversation_id FK NULL, flagged_message_id FK NULL (SET NULL), rest unchanged.
- scam_reports: reported_identity_id FK, reported_in_conversation_id FK, rest unchanged.
- scam_check_requests: related_conversation_id, related_message_ids_json (internal ids), related_identity_ids_json.
- scam_feedback: related_conversation_id, related_message_id, related_flag_identity_id.
- scam_analytics: `scans_private` → `scans_direct`; others unchanged.
- Cloud payloads (`scam/cloud_sync.py`): keep sending the Telegram ids the
  current cloud contract expects, read via the identity/conversation
  external_id **when channel == telegram**; skip non-Telegram rows until cloud
  contract v2 (Phase 3).

Dropped: `sync_text_repair` (one-shot v1 repair).

## Telegram mapping (only `backend/telegram/*` may know this)

| Telegram | v2 |
|---|---|
| marked peer id (dialog id) | `conversations.external_id = str(peer_id)` |
| message id | `messages.external_id = str(id)`, `messages.seq = id` |
| user id | `identities(channel='telegram', external_id=str(user_id))` |
| chat as sender (channel post, anonymous admin) | `sender_identity_id = NULL`, `sender_name` set |
| reply_to_msg_id | `reply_to_external_id = str(id)` |

Helpers in `backend/telegram/ids.py`: `peer_key(peer_id) -> str`,
`conversation_id_for(db, peer_id)`, `peer_id_of(conversation) -> int`,
`ensure_identity_for_user(db, user)`; an in-process cache maps peer id ↔
conversation id for the account.

## API conventions

- Routes key on internal ids: `/api/conversations/{conversation_id}/…` (→
  `/api/chats/{telegram_id}/…`), `/api/people/{person_id}/…` (→
  `/api/contacts/by-telegram/…`, `/api/people/{telegram_id}`),
  `/api/messages/{message_id}`.
- `/api/ai/conversations` (→ `/api/conversations`) for LLM chat history.
- Telegram-only operations stay where they are but take internal ids and
  return 409 with `"code": "unsupported_channel"` for non-Telegram conversations.

### Payload shapes

```jsonc
// Conversation
{ "id", "account_id", "channel", "kind", "title", "username", "avatar_url",
  "peer": { "person_id", "identity_id" } | null,
  "is_monitored", "is_hidden", "is_briefing", "is_tracked", "is_forum",
  "slow_mode_seconds", "is_pinned", "is_archived", "is_bot",
  "is_deleted_account", "history_complete", "is_muted", "muted_until",
  "is_marked_unread", "unread_count", "last_synced_at", "last_activity_at",
  "last_message": { "text", "sender_name", "date" } | absent,
  "capabilities": ["send", "react", …] }

// Message
{ "id", "conversation_id", "external_id", "seq",
  "sender": { "identity_id", "person_id", "name", "avatar_url" } | null,  // name = sender_name
  "subject", "text", "text_html", "date", "is_outgoing", "kind", "meta",
  "attachments": [ { "id", "kind", "filename", "mime_type", "size", "url" } ],
  "reactions", "my_reaction", "importance_score", "user_flagged",
  "reply_to_message_id",            // internal id if stored, else null
  "reply_to_external_id",           // channel id of the replied-to message (open it with around_external_id)
  "reply_to_preview": { "sender_name", "text", "media_type" } | null,
  "topic_id", "is_pinned", "grouped_id", "delivery_state", "is_edited",
  "fwd_from_name", ...scam_fields }

// Person
{ "id", "name", "display_name", "is_contact", "is_important", "is_self",
  "identities": [ { "id", "channel", "external_id", "display_name",
                    "username", "phone", "avatar_url", "is_contact" } ],
  "twitter_handle", "linkedin_url", "notes", "relationship_status", … }
```

`media_url`/`media_type`/`media_filename`/`media_size` on Message are
replaced by `attachments[]`.

### WebSocket events

All `chat_id` → `conversation_id`, `tg_message_id` → `message_id` (internal),
`telegram_ids` → `identity_ids` / `person_ids`, `user_id` → `identity_id`.
`telegram_status` is removed; `connector_status` is the only status event.
`navigate_to_chat` → `navigate_to_conversation {conversation_id}`.

## Route map (v1 → v2)

Path params named `{conversation_id}`, `{message_id}`, `{person_id}`,
`{identity_id}`, `{attachment_id}` are internal ids. Request/response bodies
follow the same rule (`chat_id`→`conversation_id`, `telegram_id`→
`person_id`/`identity_id`, `msg_tg_id`/`tg_message_id`→`message_id`).
Telegram-only operations on a non-Telegram conversation answer
`409 {"detail": "<message>", "code": "unsupported_channel", "channel": "<channel>"}`.

| v1 | v2 |
|---|---|
| `/api/chats` (list, `/by-ids`, `/search`, `/search/messages`, `/hidden`, `/tracked`, `/briefing`, `/archived`, `/bulk-sync`) | `/api/conversations/…` same sub-paths |
| `/api/chats/{telegram_id}/…` (hide, briefing, track, mark-read, mark-unread, pin, archive, archive-history, mute, messages/window, backfill, messages/search, sync, shared-media, members, presence, typing, topics, topics/{topic_id}/state, slow-mode, pinned, post-stats) | `/api/conversations/{conversation_id}/…` same sub-paths |
| `/api/chats/{telegram_id}/messages/{msg_tg_id}/react` · `/pin` | `/api/messages/{message_id}/react` · `/pin` |
| `/api/chats/{telegram_id}/ensure` | `POST /api/telegram/conversations/ensure` `{peer_id}` → Conversation |
| `/api/chats/search/telegram`, `/api/search/telegram` | `GET /api/telegram/search?q=` (directory search; results carry `peer_id` and `conversation_id` when stored) |
| `/api/chats/folders` | `/api/telegram/folders` |
| `/api/chats/create/{private,group,channel}`, `/api/chats/join` | `/api/telegram/create/{private,group,channel}`, `/api/telegram/join` |
| messages/window query `before`/`after`/`anchor` = `(date, tg id)` | query `before`/`after`/`anchor` = **message id**; `around_external_id` fetches an unstored message (e.g. a reply target) from the channel; `around_date` unchanged |
| `/api/compose/{send,edit,delete,forward,send-files}` | same paths; bodies: `conversation_id`, `reply_to_message_id`, `message_id(s)`, forward `{from_conversation_id, message_ids, to_conversation_id}` |
| `/api/admin/{telegram_id}/…` | `/api/admin/{conversation_id}/…`; target users as `identity_id` |
| `/api/media-download/{chat}/{msg}` | `POST /api/media-download/{attachment_id}` → `{url, kind}` |
| `/api/contacts` (list), `/{contact_id}`, `/by-telegram/{tid}`, `/{tid}` | `GET /api/people` (`?contacts_only=`), `GET/PUT /api/people/{person_id}` |
| `/api/contacts/by-telegram/{tid}/important` | `POST /api/people/{person_id}/important` |
| (new) | `GET /api/people/{person_id}/conversations` → the person's direct Conversations on every channel, most recent first |
| `/api/contacts/{tid}/positions…` | `/api/people/{person_id}/positions…` |
| `/api/contacts/{id}/relationship-status[/auto]`, `/refresh-statuses` | `/api/people/{person_id}/relationship-status[/auto]`, `/api/people/refresh-statuses` |
| `/api/contacts/sync` | `POST /api/people/sync` (address-book sync of every connector that has one) |
| `/api/contacts/resolve/{tid}`, `/resolve-batch` | removed (payloads carry person ids); `GET /api/telegram/users/{user_id}/person` → `{person_id}` for `tg://user?id=` links |
| `/api/people/{tid}/notes…` | `/api/people/{person_id}/notes…` |
| `/api/profiles/{user_id or @username}` | `GET /api/telegram/identities/{identity_id}/profile` (live Telegram profile); `GET /api/telegram/resolve?username=` → `{person_id?, conversation_id?}` |
| `/api/conversations` (AI chat history) | `/api/ai/conversations` |
| `/api/catchup/{chat_telegram_id}/…` | `/api/catchup/{conversation_id}/…` |
| `/api/chats/{tid}/tags…` | `/api/conversations/{conversation_id}/tags…` |
| `/api/scam/senders/{tid}/verdict`, `/flags/{tid}` | `/api/scam/senders/{identity_id}/verdict`, `/api/scam/flags/{identity_id}` |
| `/api/scam/scanned-messages/{chat_id}/{tg_message_id}` | `/api/scam/scanned-messages/{message_id}` |
| `/api/scam/status/{chat_id}` | `/api/scam/status/{conversation_id}` |
| `/api/scam/check-users` `{telegram_ids}` / `check-messages` `{chat_id, message_ids}` | `{identity_ids}` / `{conversation_id, message_ids}` |
| `/api/workspaces/{id}/move` `{chat_id}` | `{conversation_id}` |
| (new) | `GET /api/people/{person_id}/timeline` items: `{kind: "message", message, conversation}` and, for meetings the person attends that have started, `{kind: "meeting", event}` (pages are cut by messages) |
| (new, Phase 5) | `GET /api/calendar/events?start=&end=&from_date=&to_date=&account_id=&person_id=` → `{events: [CalendarEvent]}`; `GET /api/calendar/people/{person_id}/upcoming` → `{events}`; `GET /api/calendar/accounts`; `GET/PUT /api/calendar/accounts/{account_id}/calendars` (synced calendars, pref `calendar_ids`); `POST /api/calendar/sync`. CalendarEvent: `start_at`/`end_at` UTC ISO, or `YYYY-MM-DD` (end exclusive) when `all_day`; `attendees[]` = `{identity_id, person_id, name, email, response_status, is_organizer, is_self}`, organizer first |

Telegram-only routes live in `backend/api/telegram.py` (prefix `/api/telegram`).

## WebSocket events (v2)

```jsonc
{ "type": "new_message", "conversation_id", "message_id" /* null for a new-conversation ping */,
  "conversation_title", "sender_name", "text", "is_tracked", "is_outgoing", "message"? }
{ "type": "message_edited", "conversation_id", "message_id", "message" }
{ "type": "messages_deleted", "conversation_id", "message_ids" }
{ "type": "messages_pinned", "conversation_id", "message_ids", "pinned" }
{ "type": "media_ready", "conversation_id", "message_id", "attachment_id", "kind", "url" }
{ "type": "conversations_synced", "conversation_ids" }          // was chats_synced
{ "type": "conversation_synced" | "conversation_sync_empty", "conversation_id" }
{ "type": "conversation_sync_failed", "conversation_id", "status", "error" }
{ "type": "message_sent", "conversation_id", "message_id" }
{ "type": "message_send_failed", "conversation_id", "error" }
{ "type": "user_typing", "identity_id", "conversation_id", "name" }
{ "type": "user_status", "identity_id", "status", "last_seen"? }
{ "type": "navigate_to_conversation", "conversation_id" }
{ "type": "connector_status", "channel", "account_key", "state", "error" }
{ "type": "summary_ready", "conversation_id" }
{ "type": "person_notes_changed" | "person_changed", "person_id" }
{ "type": "scam_users_flagged", "identity_ids" }
{ "type": "calendar_synced", "account_id" }                     // Google Calendar events of an account changed
// scam_detected / scam_check_safe / scam_check_error: chat_id→conversation_id,
//   sender_ids→identity_ids; messages[] entries carry message_id, conversation_id, identity_id
// archive_progress: current_chat → {conversation_id, title}
// unchanged: bulk_sync_complete, scan_complete, importance_update (message_id = internal id),
//   research_complete (entity: 'person' | 'organization'), catchup_summaries_updated, resync
```
`telegram_status` is gone; the frontend reads `connector_status`.

## Field renaming rule (every request/response body not listed above)

Endpoints not spelled out above keep their v1 shape **except** these renames,
applied mechanically by backend and frontend alike:

| v1 field | v2 field |
|---|---|
| `chat_id`, `chat_telegram_id`, a chat's `telegram_id` | `conversation_id` |
| `chat_name` | `conversation_title` |
| `chat_type` (`private`/`group`/`channel`) | `conversation_kind` (`direct`/`group`/`channel`/`email_thread`) |
| a person's/contact's `telegram_id`, `contact_id` | `person_id` |
| a sender's `sender_id` | `sender_identity_id` (+ `sender_person_id` when a person link is useful) |
| `telegram_ids` (people) | `person_ids`; (Shield senders) `identity_ids` |
| `tg_message_id`, `msg_tg_id`, `message_id` meaning a Telegram id | `message_id` (internal) |
| `reply_to_tg_message_id` | `reply_to_message_id` |
| message `media_url`/`media_type`/`media_filename`/`media_size`/`media_path` | `attachments[]` |
| `sources: [{chat_id, telegram_id}]`, `citations: [{chat_id, tg_message_id}]` | `[{conversation_id, message_id}]` |
| `user_id` (Telegram user in typing/status/admin) | `identity_id` |

Embedded Conversation / Message / Person objects use the shapes in
"Payload shapes" (`backend/serializers.py`).
