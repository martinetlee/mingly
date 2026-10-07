# Mingly — Efficiency, Reliability & Reuse Review

Reviewed 2026-10-06 against `main` @ `617a12d` (+ uncommitted Telethon 1.45 bump). Read-only review: no application code was changed.

**Scope:** whole app (~20k lines backend, ~22k lines frontend). Every user-facing flow was traced UI → API → DB/Telegram: chats, compose, media, forum topics, search, contacts/orgs/positions, workspaces/kanban, intelligence/briefing/catch-up, Shield, auth/vault/onboarding, settings, MCP, infra.

**Method:** six area reviews ran in parallel. Each finding had to be backed by a code trace with a concrete input or interleaving. The headline findings were then re-verified independently:

- reading the exact lines;
- running `pyflakes` over `backend/`;
- inspecting compiled code objects;
- comparing Telethon 1.42/1.45 sources;
- read-only queries against the real DB (`~/Library/Application Support/Mingly/mingly.db`);
- reproduction scripts against in-memory SQLite (search, positions/boards, briefing) and vitest harnesses (WebSocket hook).

Findings marked **[V]** were re-verified first-hand while writing this doc; the rest rest on the area reviewers' end-to-end traces.

Severity: **P0** = feature is broken or data is corrupted today · **P1** = race/reliability bug a normal user will hit · **P2** = correctness/efficiency issue in edge cases or at scale · **P3** = reuse/cleanup.

---

## 0. Executive summary

1. **Several features are dead right now from incomplete refactors, and nothing catches it.** Global search, Shield rescan, the chat "Check for scam", new-chat auto-create on incoming messages, briefings (manual and scheduled), forum topics and vault reset all fail on every call. In most cases the failure is swallowed, so the UI looks like it simply did nothing. The common root cause is that there is no static check for undefined names/attributes and no endpoint smoke tests. `pyflakes` alone finds three of these.
2. **No component owns the Telegram runtime.** Client creation, handler registration, catch-up and the periodic sync are copy-pasted across startup, unlock and login. The only complete copy (startup) can never run, because the vault is always locked at launch. Every unlock stacks another client and another set of event handlers.
3. **Async results are not tied to the identity they were requested for.** This is the dominant race class in the frontend: chat switching, workspace switching, search typeahead, org/contact selection, Tracked, Intelligence conversations and ModerationPanel. Some have guards, some don't, and the guards differ. Results from chat A can render under chat B, and in the worst case delete/edit/forward/unpin act on the wrong chat.
4. **The sync engine stores wrong and stale content.** Formatted messages are stored with markdown-unparsed text plus entity offsets for the raw text, so the HTML is garbled. Edits, reaction removals and unpins never update stored rows. A "skipped" sync is reported as "this chat has no history".
5. **State models overload one field with several meanings.** Examples: `scam_score` (verdict + confidence + "failed"); stage tags resolved by global display name; `ContactPosition.organization` as a free-text join key; offset pagination counted from "newest". Each produces a family of bugs that point fixes won't close.
6. **There is no single-flight / job abstraction.** Briefings, catch-up summaries, cloud sync, scam scans and media downloads can all run concurrently against the same rows and the same local Ollama, producing duplicates, wasted LLM time and corrupt partial files.
7. **Frontend infrastructure is hand-rolled per page.** It is missing:
   - a shared data-fetching hook;
   - a single WebSocket connection (each mount leaks a reconnecting socket);
   - a single HTTP/SSE client (four copies of authenticated fetch);
   - a Modal/Confirm/Toast system (native `confirm`/`alert` mixed with three dialog styles);
   - a UTC datetime convention.

   Most of the P1 races below fall out of these gaps.

---

## 1. P0 — Broken today (verify-and-fix first)

| # | Feature | Evidence | What the user sees | Notes |
|---|---|---|---|---|
| P0-1 **[V]** | Global search (⌘K) | `backend/api/search.py:31` selects `Message.tg_message_id` but `:47` reads `r.telegram_id` → `AttributeError` → 500. Column rename in commit 3e4d6ec missed this file. Frontend `GlobalSearch.tsx:80,84` also still uses `m.telegram_id`. | Any query matching any message text returns 500. `GlobalSearch.tsx:153` sets `results=null`, so local chats/contacts/orgs vanish and only Telegram hits remain. | Rename on both ends; add a search test that hits a message. Show per-section errors instead of blanking. |
| P0-2 **[V]** | Shield "Rescan", chat "Check for scam" | `backend/api/scam.py:46,150` call `spawn(...)`, which is never imported (pyflakes: `undefined name 'spawn'`). Introduced in 617a12d when `asyncio.create_task` was replaced. | `POST /api/scam/check-messages` and `/check-users` always 500. Shield's spinner silently clears; chat shows "Failed to start scam check". | `from backend.tasks import spawn`. |
| P0-3 **[V]** | Incoming message from a chat not yet in the DB | `backend/telegram/sync.py:1234` assigns `_chat_type = chat.type` inside `on_new_message`. That makes `_chat_type` a local (cell) variable of the whole function, so the call `_chat_type(entity)` at `:1117` raises `UnboundLocalError`. It is caught at `:1130` → `return`. Verified: `'_chat_type' in on_new_message.__code__.co_cellvars == True`. | The first live message of every brand-new conversation (new DM, newly joined group) is silently dropped. The chat only appears after a later dialog sync, and that live message is never saved. Because the periodic sync never runs (P0-6), this can persist for the whole session. | Rename the local (`_chat_kind`). The real fix is §2-A plus the lint gate. |
| P0-4 **[V]** | Briefing — manual and scheduled | Manual: `intelligence.py:1298` wraps `get_tracked_chats` rows, which are `Row(Chat, last_text, …)` since commit b3fea1d, then reads `chat.name` → `AttributeError`. Scheduler: `briefing_scheduler.py:79` does `row[0]` on `get_briefing_chats`, which returns plain `Chat` → `TypeError`. Then `:168` uses `msg.telegram_id`, which doesn't exist. Reproduced against in-memory SQLite. | The manual stream dies right after `start` ("Briefing failed: network error"). Scheduled runs fail every period; `briefing_last_run` is written *before* running (`:243`), so there is no retry. Real DB: 0 briefing runs ever. | Three hand-copied pipelines (§2-F) are why these drifted. |
| P0-5 **[V]** | Forum topics (list / create / close / reopen) | `chats.py:1083,1152,1188` import `Get/Create/EditForumTopicRequest` from `telethon.tl.functions.channels`. In Telethon **both 1.42 and 1.45** they exist only in `functions.messages` (with `peer=`), so this is not caused by the 1.45 upgrade. | Listing always falls back to the DB ("Topic N" titles, no General, N+1 queries). Create/close/reopen always return 400; `ForumTopicsPanel.tsx:45,58` swallows the error. | See also P1-C7 (topic scoping). |
| P0-6 **[V]** | Startup catch-up, 2-min periodic dialog sync, startup scam scan | `main.py:75-109` runs only if `has_credentials()` and `init_client()` is authorized at process start. The vault is always locked at launch (`secrets.get_secret` returns `None` when `_cache is None`), so the block never runs. The unlock path (`auth.py:80-89`) spawns only full sync, bulk sync and mention sync. `_periodic_dialog_sync`, `catch_up()` and `scan_unscanned_messages` have no other callers. | Messages missed while the app was closed aren't caught up. Nothing re-syncs dialogs every 2 minutes; `sync.py:481` and `chats.py:589` explicitly defer work to that sync. | Fix via §2-A. |
| P0-7 **[V]** | "Forgot password? → Reset" on Unlock | `auth.py:95` puts `require_session_token` on `/vault/reset`, but the Unlock screen has no token by definition. `Unlock.tsx:35` swallows the 401. | A user who forgot their password has no recovery path; the button just un-spins. | Needs a different guard (Host/Origin + typed confirmation) and a real teardown (§2-A, §2-I). |
| P0-8 | Org "Generate context", conversation auto-title with Anthropic/OpenAI | `organizations.py:245-295`, `conversations.py:67-74,285-295` POST raw to `{base_url}/chat/completions` with no auth header, bypassing the provider registry. | 502 "LLM error" with any keyed provider; works only with keyless Ollama. | §2-G. |
| P0-9 | Ollama model pull error reported as success | `api/llm.py:493-500`: an inline `{"error": …}` (HTTP 200) is merged and then marked `done`. `LlmSetup.tsx` only checks `status==='error'`. | Setup says "AI is ready" with a model that isn't installed. Every LLM feature then yields "Summary unavailable." | Treat any `error` key as failure on both ends. |

**Systemic root cause of P0-1…P0-5:** renames and refactors with no safety net. `backend/tests` has one file and `tests/` has none for search, scam, briefing or forum. The build (`build_app.sh`) runs no Python checks.

**Design fix:**

- Add `pyflakes`/`ruff --select F821,F841,F811` and an import-every-router smoke test to `build_app.sh`. They would have caught P0-2 and P0-3.
- Add one API smoke test per router that calls each GET/POST with a seeded in-memory DB. That would have caught P0-1 and P0-4.
- Add an attribute-level check (mypy on `backend/` with SQLAlchemy 2.0 typed models) to catch `msg.telegram_id`-style drift.

---

## 2. Systemic design issues (root causes behind the P1/P2 findings)

### 2-A. The Telegram runtime has no owner

**Findings:**

- **P1 [V] Re-unlock stacks clients.**
  - `/vault/unlock` always runs `init_client()` → `sync_dialogs` → `register_event_handlers()` (`auth.py:80-89`).
  - `init_client` overwrites the global `_client` without disconnecting the old one (`client.py:40`).
  - `register_event_handlers` has no idempotency guard (`sync.py:1102`).
  - `Unlock.tsx:67` Enter bypasses the button's `disabled`.
  - Double-Enter interleaving: both requests create clients. The second client gets **two** handler sets, so every message is saved twice, scam-scanned twice, broadcast twice and notified twice. The first client leaks while still connected on the same auth key, which risks `AUTH_KEY_DUPLICATED` / session revocation.
  - Token skew: the frontend may keep T1 while the server holds T2, causing a 401 storm and a bounce back to Unlock in a loop.
  - The same overwrite pattern exists in `/api/auth/setup` and in `main.py` startup.
- **P0-6** (startup block dead) and **P0-7** (reset).
- **P2 No lock / teardown.**
  - `secrets.lock()` is never called, apart from reset.
  - Reset leaves the Telegram client, handlers, schedulers and WebSockets running.
  - The session token never expires.
  - WebSockets are authenticated only at connect time (`router.py:91-93`), so a rotated token doesn't evict them.

**Root cause:** post-auth bootstrap is copy-pasted across `main.py`, unlock, setup, verify-code and verify-2fa, and each copy does a different subset.

- **P1 No reconnect supervision.** Reconnection is lazy and request-driven (`ensure_connected` from ~26 call sites). If Telethon gives up after its default 5 retries (e.g. waking from sleep on slow Wi-Fi), live updates stay dead until some request happens to reconnect. Unlock wraps everything in `except Exception: pass`, so a FloodWait or IntegrityError during `sync_dialogs` silently skips `register_event_handlers()`: no live updates for the whole session.
- **P2 Logout leaves the server session active.** It never calls `client.log_out()`, so the session stays in Telegram's Active Sessions. Background bulk/tag syncs keep running after logout and report every chat as "done".
- **P3 Notification click broadcasts from the wrong thread.** It runs `ws_manager.broadcast` on a fresh event loop on the Cocoa main thread (`sync.py:53-66`), writing to sockets owned by the server loop. It works by accident. Use `run_coroutine_threadsafe(…, server_loop)`.

**Design:** add one `TelegramRuntime` (module singleton guarded by an `asyncio.Lock`).

- `ensure_started()`: if already running, no-op. Otherwise disconnect any old client, connect, register handlers exactly once, `catch_up()`, start the periodic sync and the scan queue (§2-E).
- `stop()`: cancel the runtime's tasks, remove handlers, disconnect.

Every successful auth/unlock path calls `ensure_started()`. Lock/reset/logout call `stop()`, rotate the token and close all WebSockets. `/vault/unlock` on an already-unlocked vault only issues a token. The frontend gates every Enter handler on its in-flight flag (Unlock, and Onboarding `:271,349,388,425`).

### 2-A2. The sync engine: message conversion, upsert semantics, coordination

- **P1 [V] Stored text and HTML are garbled for formatted messages.** `sync.py:984,1150` store `msg.text`, which Telethon builds by *markdown-unparsing* the message (default `parse_mode`). The app then applies `msg.entities`, whose offsets refer to the raw `msg.message`. Reproduced with the app's own `_entities_to_html`:

  | Input | Rendered by the app | Correct (raw text + entities) |
  |---|---|---|
  | `hello __world__` | `hello <em>__wor</em>ld__` | `hello <em>world</em>` |
  | `use `x` now` | ``use <code>`</code>x` now`` | `use <code>x</code> now` |

  Stored `text` also carries markdown markers into search, LLM prompts and previews. The `has_raw_markdown` / `_markdown_pass` heuristic (`sync.py:310`), `_strip_formatting` and the `MessageBubble.tsx:650` workaround all paper over this. Other write paths (`sync_global_mentions`, the Raw handler) store raw text, and the first writer wins (next item), so the same message can differ depending on which path saw it first.
- **P1 [V] Edits, reaction removals and unpins never reach stored rows.**
  - The update branch of `crud.save_message` never assigns `text`.
  - It updates `text_html` and `reactions_json` only when the new value is truthy, so an edit to plain text or a removed reaction leaves the old value.
  - It never updates `is_pinned`.
  - There is no `events.MessageEdited` handler.

  A contact edits "meet at 5" to "meet at 6"; the bubble, search and briefings keep "5" forever. Only in-app edits work, via `update_message_text`.
- **P2 Outgoing messages sent from other devices spin forever.** `delivery_state = "sending" if msg.out` (`sync.py:1162`). Telethon never dispatches this client's own sends, so any `out` message reaching the handler came from another device and is already delivered. A replayed event can also regress "sent" back to "sending".
- **P2 `sync_messages` returns 0 for skipped, no-client and no-change alike.** Opening a never-synced chat while offline (or while another sync of it is in flight) broadcasts `chat_sync_empty`, which shows "No message history available — this group may have been migrated" and disables the fallback poll. There are two independent dedup sets (`_ACTIVE_CHAT_SYNCS`, `_BG_SYNCING`) and no global rate limiter across bulk, tag, mention, open and manual syncs.
- **P2 Briefing chats are excluded from every automatic sync** (`sync.py:654`, `chats.py:749`), and the briefing reads only local rows. A 09:00 briefing after a night with the app closed says "No messages in this period."
- **P2 Refetch storm.** Bulk sync broadcasts `chat_synced` for each of 50 chats whether or not anything changed (`sync.py:657, 723`). Chats, Home and Intelligence answer each with full refetches, unthrottled; only Board and WorkspaceContext throttle. This is multiplied by the leaked sockets (§2-H.1).
- **P2 One commit per row.**
  - `save_message` does SELECT + INSERT + chat UPDATE + COMMIT + REFRESH per message, and `upsert_chat` does the same per dialog. A 50×30 bulk sync is about 1.5k commits.
  - `/vault/unlock` awaits `sync_dialogs(limit=200)` inline, including serial mention-chat history syncs and up to 200 profile-photo downloads, so the unlock screen hangs on first run.
  - `_download_photo` runs inline before the `new_message` broadcast, so a slow download delays the message appearing.
- **P2 Pinned-message probe.** Every sync calls `GetFullChannelRequest` for supergroups and channels (flood-prone, 50× per bulk sync). For DMs it makes a guaranteed-failing `GetFullChatRequest`. Everything is under `except: pass`, which swallows `FloodWaitError` so the bulk loop's FloodWait-stop never fires. DM pins are never fetched.
- **P3 Concurrent NewMessage tasks for a brand-new chat.** Telethon dispatches one task per update, and `upsert_chat` is SELECT-then-INSERT, so the loser's IntegrityError makes the handler `return` before `save_message`. The second message is dropped. The same race in `sync_dialogs` aborts the whole dialog sync.
- **P3 Duplication.**
  - Chat-type classification exists 5 times. The `chats.py:488` copy (`hasattr(c,'broadcast')`) is wrong: it labels every supergroup "channel".
  - The name-fallback logic is duplicated 5 times.
  - Two reconnect helpers have different semantics.
  - Bare `asyncio.create_task` is used in four places despite `spawn()`.
  - The `events.Raw` UpdateShortMessage handler is redundant with NewMessage, which already handles it.

**Design:**

- One `message_to_row(msg)` function (raw text + entities → `text`, `text_html`, reactions, pin, topic, delivery state = "sent") used by sync, the live handler, mention sync and compose.
- One real upsert, `INSERT … ON CONFLICT(tg_message_id, chat_id) DO UPDATE`. Telegram-owned columns always take the new value; local-only columns (scam, `my_reaction`, `media_path`) are kept; delivery state is monotonic. Batch it per page with one commit.
- A `MessageEdited` handler through the same path.
- A per-chat `SyncCoordinator` (`chat_id → Future`): concurrent callers await the in-flight sync and get a typed result (`changed | skipped | unavailable | error`). It owns a global token bucket and FloodWait backoff. Only a completed empty fetch may emit `chat_sync_empty`. Broadcast one batched `chats_synced{ids}` only when something changed.
- Briefing runs sync their tracked and briefing chats through the coordinator first.
- `upsert_chat` becomes `ON CONFLICT DO UPDATE`.
- Move entity helpers to `backend/telegram/entities.py`.
- Re-sync to backfill the corrupted `text`/`text_html`.

### 2-B. Async results aren't bound to the identity they were requested for (frontend stale-response races)

Every guard in the app is "last caller wins" (`seq`), not "result belongs to the current X". Some places have neither. Concrete user-visible failures:

| Where | Interleaving → result |
|---|---|
| `Chats.tsx:1199-1203, 1262-1264, 1184-1185` — send / send files / sync then `loadMessages(selectedChat)` from a stale closure | Send in A, click B, send resolves → A's messages render under B's header and ComposeBar. Replying to a visible message then calls `sendMessage(B, text, A_msg_id)`. Upload progress and `syncing` are page-level, so B's compose box shows A's upload. |
| `Chats.tsx:655-692` — `loadMoreMessages` has no guard | A's older page is prepended into B. B's offset becomes 100, which suppresses B's live WS appends (`:899`). |
| `Chats.tsx:738-776, 493-508` — URL/mention/palette/notification/new-chat entry points reset fewer fields than `handleSelectChat` | Editing message 123 in A, then opening B from a mention: B's ComposeBar starts in edit mode → `editMessage(B, 123)` edits B's message 123 (a channel shares the ID space). Reply-to leaks the same way. B isn't marked read and shows A's topic title. |
| `Chats.tsx:1315-1330, 2165` + `useKeyboardShortcuts.ts:33-48` — dialogs resolve the chat at *confirm* time, and j/k/arrows still fire under modals | Open Delete for msg 500 in A, press ↓ (B selected behind the modal), confirm → `deleteMessages(B, [500])`. Forward and Report do the same. |
| `ChatInfoPanel.tsx:1041` renders `<ModerationPanel>` without `key` | Switch group A → B with the panel open: A's pinned list remains, and Unpin calls `pinMessage(B, A_msg_id, false)`, which may unpin an unrelated message in B. |
| `Chats.tsx:531-575` loadChats/loadMoreChats; `Board.tsx:604`; `WorkspaceContext.refreshWorkspaces` | Switch workspace or tag filter while a fetch is in flight → the previous workspace's chats render, and load-more appends pages from the old filter. Board keeps the old board under the new header; a drag posts B's workspace with A's board (400). |
| `Board.tsx:205-240` — the optimistic move doesn't bump `boardSeqRef` | A WS-triggered `GET /board` taken before the move lands after it → the card snaps back and stays wrong. |
| `Tracked.tsx:41-69`, `Contacts.tsx:85-96`, `ComboInput.tsx:29-50`, `Chats.tsx:998-1014, 1400-1423`, `NewChatDialog.tsx:137-160`, `Intelligence.tsx selectConv`, Home `handleWsEvent` | Out-of-order typeahead/selection responses overwrite newer ones. Tracked can show A's messages while Compose sends to B. ComboInput reopens after selection. |
| `ContactInfoSection.tsx:136-166`, Profile `PositionsSection` — `{...contact, positions}` from the closure at request start | Delete positions A then B quickly → A is resurrected in the UI, and clicking it again gives a 404. An add-position completion reverts a concurrent phone/notes edit. |
| `Organizations.tsx` generate/save context; `conversations.py suggest_title`; `Intelligence.tsx handleRegenerateEntry` | Generate on org A, switch to B → the view jumps back to A after up to 60s. An auto-title overwrites a manual rename. Two regenerates leave the second running with no spinner or Stop. |
| `WorkspaceDialog.tsx:44-56` resets the form on every new `workspace` object | Editing a workspace from Board: an incoming message refreshes counts → the draft is reset. (Opening from the Sidebar doesn't do this, which is inconsistent.) |

**Design:**

1. Add a `useLatestQuery(key, fetcher)` hook (AbortController + key check: a result is applied only if `key` still equals the current key) and use it for every scoped list, typeahead and selection. Alternatively adopt TanStack Query, which provides keyed caches, cancellation, dedupe and invalidation-after-mutation, and removes most of the hand-rolled state. Chats alone has 62 `useState` and 17 effects.
2. Chats: add a single `selectChat(chat, {anchorMsgId?, topicId?})` reducer action that every entry point calls, plus a `useChatMessages({chatId, topicId})` store keyed by view scope. Per-chat transient UI (reply, edit, upload progress, syncing, draft) lives in that keyed slot, so it physically cannot cross chats.
3. Dialogs capture `{chatId, tgMessageId}` when they open. A shared `<Modal>` registers a shortcut scope that suspends page shortcuts.
4. Mutations update via functional `setState(prev => …)`, or invalidate the owning query. Server-generated content (context, title, regenerate) is written compare-and-set (`WHERE title IS NULL` / `updated_at = :read_at`).
5. Forms initialise on the *open transition* (or `key={entity.id}`), not on every prop identity change.

### 2-C. The message window model (offset-from-newest pagination)

- **P1 duplicates and skips:** `crud.get_messages` uses `ORDER BY date DESC LIMIT/OFFSET` (`crud.py:309-311`). New WS messages shift every offset: scrolling up re-fetches the last 5, giving duplicate React keys. Deletions skip rows. Albums share a timestamp and there is no tiebreaker.
- **P1 jump windows:** after jumping to a search result or the pinned banner, `messageOffset` stays 0 (`Chats.tsx:1035-1048`, `:2008`). Scrolling up loads *newer* messages above older ones. New WS messages append under the old window as if contiguous, and there is no way back to the present.
- **P1 "Load older from Telegram" is unreachable:** `Chats.tsx:668-679` calls `syncChat(id,100)`, but `sync.py:951` `iter_messages(chat, limit=limit)` has no `offset_id`, so it re-downloads the newest 100 **[V]**. History older than the local store can never be loaded.
- **P1 highlight pins the scroll:** `MessageList.tsx:101-118` re-centres on `highlightTelegramId` on every messages change and skips prepend anchoring. Mention, URL and pinned-banner entry points never clear it.
- **P2 unread separator:** placed by `messages.length - unreadCount` but compared against the grouped index (`MessageList.tsx:91-93, 234`). An open chat is never re-acknowledged as read, so the badge returns after the next list reload.
- The same offset problem exists elsewhere: Shield scanned-list pagination (`api/scam.py:323`, no tiebreaker, live inserts shift pages); Briefing/`get_messages_since` returning the **oldest** N in a window (§2-F).

**Design:**

- Keyset cursors `(date, tg_message_id)` on both directions: `before=` / `after=`, returning `{messages, has_older, has_newer}`.
- The anchor/date-jump endpoint returns a window with both flags.
- The client tracks `{oldestCursor, newestCursor, atLatest}`. It appends WS messages only when `atLatest`, shows a "Jump to latest" control, and dedupes merges by id.
- Telegram backfill takes `offset_id = oldest local tg_message_id` and returns `exhausted`.
- Highlight is a one-shot scroll intent, separate from the visual highlight.
- Unread is anchored to the first unread `tg_message_id`.
- Read acknowledgement is debounced while the chat is open and focused.

### 2-D. Overloaded fields and "same intent, different operation"

**Scam verdicts (Shield).**

- **P1 One verdict is fanned out to the whole batch, and the target is often not analysed.**
  - Real-time (`sync.py:1236-1266`) and manual (`api/scam.py:80-107`) checks prepend up to 5 context messages.
  - `scam/client.py:154` builds the prompt from `messages[:5]`, so with 5 context messages the new message (index 5) is dropped.
  - `service.py:316-365` writes the single score to **all** message ids and flags **every** sender. Mass scan does the same per chat (up to 200 messages).
  - Real DB: 1,411 requests with 6 ids; target text absent from the trace in 6 of 8 sampled; single messages re-scored by up to 27 requests.
  - Consequences: a dismissed sender is re-flagged by a scan that only looked at the dismissed messages; innocent group members are flagged; good verdicts are overwritten by later batches.
- **P1 Failures are stored as "safe".**
  - On `result.error`, `_apply_message_result` still runs (`service.py:116-120`), so `scam_score = 0.5` **[V]** and `scam_check_safe` is broadcast *before* the error. The chat UI says "No scam detected".
  - Failed rows are never retried (`scam_score IS NULL` filter) and export them as not-scam.
  - Real DB: 2,037 failed vs 465 completed requests; 2,231 messages carry `Node 'classify' failed: Value looks like object, but can't find closing '}'` (Ollama tool-call JSON). That failure rate needs its own root-cause investigation in the Ollama tool-calling path.
- **P1 Three verdict paths disagree.**
  - `DELETE /flags/{id}` nulls the score and sets `not_scam` on **every message the sender ever sent, in all chats**, overwriting the user's own manual `scam` verdicts. Dismissed messages also vanish from Shield (filters require `scam_score IS NOT NULL`).
  - `PUT /scanned-messages/verdict` sets only the verdict: no flag change, no dismissal queue, no sync reset.
  - "Mark not scam" and "Dismiss" therefore give different end states.
- **P2 No single "effective verdict".** Only `MessageBubble.tsx:481` applies manual > pipeline precedence. Filters, status, export, cloud upload and Shield pills use raw `score > 0.5`. WS sets `scam_score: event.confidence` (raw) while the DB stores `0.5 + conf/2`.
- **Design:**
  - Add a `scam_status` column (`pending|completed|failed|short_circuited`). Failures never touch the score.
  - Request model with explicit `targets` vs `context`. Only targets are scored and flagged; targets are always included in the prompt, never truncated.
  - One service-level `set_verdict(scope=message|sender, verdict)` used by every entry point. Sender dismissal is a persisted per-sender record checked by the flagging code, not a bulk rewrite. Manual verdicts are never overwritten.
  - `effective_is_scam` defined once as a SQL expression / hybrid property, serialized by the API; the UI never re-derives it.

**Kanban stage tags (P1, data loss).**

- `crud.py:1241` resolves a new column's tag by **global case-insensitive name**, with no `category='stage'` filter and no exclusion of the workspace's membership tags.
- `move_board_chat` strips all stage tags; `delete_board_column` deletes the tag from all members.
- Reproduced: column "investors" in a workspace whose include-tag is "Investors". Dragging one card out removes that chat from the workspace; deleting the column wipes the user's tag from every chat.
- Stage tags are also shared across workspaces, and orphans survive `delete_workspace`, so a new "Done" column instantly fills with old cards.
- **Design:** each column owns a freshly minted `category='stage'` tag. It is deleted with its last column, including in `delete_workspace`. Mutations refuse to strip a tag with `WorkspaceTag` membership.

**Organizations as strings (P2).**

- `ContactPosition.organization` is free text. Renaming onto an existing org gives `IntegrityError` → 500 (unhandled in `Organizations.tsx:145`). There is no merge.
- Case variants create split orgs. `Tag` is unique only case-sensitively, but `_resolve_stream_tag` uses `lower(name)` + `scalar_one_or_none()` → `MultipleResultsFound` 500.
- Deleting an org (one click, **no confirm**, `Organizations.tsx:316`) deletes every member's job history at that org.
- **Design:**
  - Positions keyed by `organization_id` FK.
  - A unique index on `lower(trim(name))` for orgs and tags, via a migration that merges duplicates.
  - Rename-onto-existing becomes an explicit merge.
  - Delete goes through the shared confirm dialog stating the impact, or detaches positions instead of deleting them.

### 2-E. No single-flight / job abstraction for background work

| Work | Problem |
|---|---|
| Briefing (manual + scheduler) | No lock. A manual run at 08:58 and the 09:00 scheduler run both stream against one Ollama and persist two runs. Leaving and returning to the Briefing tab re-enables Generate while the orphaned stream still persists. |
| Catch-up summaries (`POST /catchup/generate-summaries` + 30-min scheduler) | Both run `generate_all_summaries`. The select-then-insert upsert loses the race to `UNIQUE` and the work is discarded. One HTTP request does N sequential LLM calls (120s timeout each). |
| Scam scans | A raw `create_task` per message (`sync.py:1277`) and per chat (`:877`); no semaphore anywhere in `backend/`. No in-flight marker, so mass scan, startup scan and real-time context batches re-scan the same rows. Real DB: 170 messages scored ≥2× within 5s; 97 ReadTimeouts. These calls also bypass `spawn()`'s strong-ref and logging hardening. |
| Shield cloud sync | Startup sync (+5s) and both "Sync Now" buttons can run concurrently: double POSTs, and two `extractall`s into the live model dir while `_get_ensemble` loads it. |
| Media download | `media.py:93` / `sync.py:908`: `if not exists: download_media(file=final_path)`. An interrupted download leaves a truncated file that is served forever; a concurrent request returns a half-written file. |
| Lazy photo in `MessageBubble.tsx:377-385` | On failure, `finally → setPhotoLoading(false)` re-triggers the effect. With Telegram disconnected (503 in ms) every photo bubble hammers `/api/media-download` in a tight loop. With Telegram connected, each retry calls `get_messages`, inviting FloodWait. |
| `VideoPlayer.tsx:150` | Auto-downloads every video on mount (10 videos → 10 concurrent full downloads), unlike Audio (click) and photos (lazy). |

**Design:** a small `JobManager` (one `asyncio.Task` per job key: `briefing`, `catchup`, `cloud_sync`, `scan:{chat}:{msg}`, `media:{chat}:{msg}`). A second request attaches to the running job's progress or gets 409.

- Scans go through one bounded `asyncio.Queue` with N workers sized to the provider, deduped by `(chat_id, tg_message_id)`.
- Downloads and model bundles write to a temp file, then `os.replace` (extract to a temp dir, then rename).
- Upserts use `INSERT … ON CONFLICT DO UPDATE`.
- Client side, one `useMediaDownload(key)` hook with a terminal error state, bounded backoff, abort on unmount and a global concurrency limit, used by photo, video, audio, album, document and GIF.

### 2-F. LLM pipelines are copy-pasted

- **P1 Briefings summarize the oldest messages in the window.** `crud.get_messages_since` (`crud.py:389-414`) does `ORDER BY date ASC LIMIT 200` **[V]**, then callers take `lines[-150:]`. A weekly briefing of a busy group sees roughly the first day. `get_messages_from_sender_since` has the same pattern.
- **P1 Catch-up can show "All caught up!" with chats still waiting.** `dashboard.py:107` limits needs-reply to 10 *before* the client filters out handled/snoozed items. After 10 triage actions without replying, the queue is empty forever.
- **P2 The catch-up scheduler only summarizes chats the user has already triaged.** `CatchUpItem` rows are created only on user action.
- **P2 Infrastructure failures are persisted as content.** `_summarize_*` turn Ollama-down / model-missing / timeout into `"Summary unavailable."` and the run is saved and shown as today's briefing. `_run_agent` catches only Connect/Timeout errors, so other errors truncate the SSE with no `error` event.
- **P2 Each Intelligence chat turn sends the user message twice** (`Intelligence.tsx` persists it, then `intelligence.py:818-831` appends it again).
- **P2 Prompts are sized by line count, not a budget.** Catch-up post-anchor fetch is unbounded (thousands of messages in a group the user last posted in months ago → timeout → fallback, every 30 minutes). Agent history is unbounded.
- **P2 Regenerate on a past briefing entry summarizes the *current* window** (`intelligence.py:1595`).
- **P2 The weekly briefing ignores `briefing_day`/`briefing_time`.** `briefing_time="99:99"` passes validation and then throws every minute.
- **P3 The citation/sources pipeline is dead.** There are no `[id:N]` prefixes, so `sources` is always empty. `_clean_summary` strips the `**Theme**` the prompt asks for and mangles `@foo_bar`.
- **Root cause:** three hand-copied briefing pipelines (endpoint, regenerate, scheduler) that already diverged: the scheduler drops org context, ignores `briefing_max_tokens` and uses a different timeout. There are also three `_get_llm_config` copies and two ~100-line identical summarizers.
- **Design:** `backend/briefing/pipeline.py` with `load_inputs() → build_transcript(msgs, char_budget) → summarize(kind) → run(sink)`, where the sink is SSE or DB. The endpoint, regenerate and the scheduler all call it, through the JobManager (§2-E).
  - Summarizers return a typed result (`ok | llm_unavailable | model_missing | timeout | empty`). Infrastructure errors abort the run, and the scheduler updates `last_run` only on success, with backoff.
  - The needs-reply query does the triage filtering server-side and is shared by the dashboard and the catch-up scheduler.
  - The backend is the sole writer of conversation turns.

### 2-G. The LLM provider abstraction is bypassed

- Intelligence, briefing, conversation titles and org context POST raw to `{base_url}/chat/completions` (with the config re-read from disk each call). Catch-up, translate and importance use `registry.get_any()` (whichever instance was registered first). The scam engine uses `get_by_model`.
- The Anthropic/OpenAI providers are stubs. `OrchestratorLLM` and `score_message_importance` are unused.
- The raw `/v1` paths miss `OllamaProvider`'s `think: False` fix, which is why `_summarize_chat` scrapes JSON out of `reasoning`, and they cannot set `num_ctx`.
- **Design:** every feature goes through `get_provider(role)` → `BaseLLMProvider.complete(tools=…)/stream()`. Each provider keeps one long-lived `httpx.AsyncClient`. Finish or delete the stubs.

### 2-H. Frontend infrastructure gaps

1. **WebSocket [V].** `useWebSocket.ts:78-83` cleanup closes the socket, but `onclose` always schedules `setTimeout(connect, 3000)`. Each unmount leaves a live, reconnecting socket bound to the dead page's handler. A vitest harness reproduced 3 mount/unmount cycles → 3 orphaned open sockets, each invoking a handler. The hook is used in 8 places (Layout, WorkspaceContext, Home, Chats, Tracked, Board, Shield, Intelligence).
   - Consequences: duplicate desktop notifications (`Chats.tsx:925`), duplicate `syncChat`/`loadChats` calls, and `ws_manager.connections` growing without bound.
   - **Design:** one `WebSocketProvider` at the root with `subscribe(type, handler)`, an intentional-close flag and backoff. Desktop notifications fire from one global place. The server closes sockets on token rotation.
2. **HTTP.** There are four copies of authenticated fetch:
   - `client.ts request()`;
   - `Settings.tsx:11-38`, duplicated byte-for-byte in `LlmSetup.tsx`;
   - four separate SSE parsers (`Intelligence.tsx:102,897,937`, `LlmSetup streamSSE`);
   - the XHR upload (`client.ts:565-616`), which doesn't route 401 to `notifySessionExpired`.

   Error normalisation shows `[object Object]` for 422s. Unknown `/api/*` GETs fall through to the SPA catch-all and return HTML 200.

   **Design:** one `http.ts` exposing `request`, `stream` (SSE async generator) and `upload`, with shared token/401/error handling, plus `/api/{path}` → 404 before the SPA route.
3. **Dialogs, confirms and toasts are inconsistent** (the user's consistency rule).
   - About 11 hand-rolled `fixed inset-0` modals with three backdrop styles and per-modal Escape handling.
   - Destructive actions are mixed: `DeleteConfirmDialog` (message delete); inline confirms (Board, WorkspaceDialog); native `window.confirm` (ban `ChatInfoPanel.tsx:307`, restrict `Chats.tsx:1358`, MCP key `Settings.tsx:591`); **no confirm at all** (org delete, position delete).
   - Errors use native `alert()` in Settings and Chats, three bespoke toasts, and 53 swallowed `.catch(() => {})`.
   - `DeleteConfirmDialog` stays mounted, so "Delete for everyone" stays ticked across deletes. Delete and Forward have no in-flight guard (double-click forwards twice).

   **Design:** `<Modal>` (with shortcut scope), `useConfirm()` (impact text, destructive styling), `ToastProvider`/`useToast()`, and `useAsyncAction()` (pending, error, single-flight). Mount dialogs conditionally.
4. **Datetimes.** The DB stores naive UTC. Dashboard, mentions and briefing history emit ISO without `Z`; `catchup.py` adds `Z`. `new Date()` parses the former as local time, so a UTC+8 user sees new messages as "8h ago", and one triage card mixes correct and wrong times. Shield and Settings patch this ad hoc. **Design:** one response serializer (Pydantic `response_model` + UTC encoder) and one `parseServerDate()`. Generate `types.ts` from OpenAPI to stop shape drift: `dashboard._chat_dict` duplicates `chats._chat_dict` but omits five non-optional TS fields and the username fallback.
5. **Theme** is applied only when Settings mounts (`useTheme` is only called there), so Light users get Dark until they visit Settings. Bootstrap it in `index.html`/`main.tsx` and hold it in context.
6. **Keyboard.** `useKeyboardShortcuts` calls `preventDefault` for any mapped key outside inputs. On Chats, Enter and Arrow keys are mapped, so Enter on any focused button is swallowed and arrows switch chats instead of scrolling. The listener re-registers every render. The ⌘1-9 map in `Layout.tsx:10` has drifted from the Sidebar (⌘9 = 8th item; `/board` unreachable). **Design:** handlers return a handled boolean, interactive and scroll targets are ignored, and shortcuts are derived from the Sidebar `NAV` array.
7. **Settings saves** are optimistic and fire-and-forget (`setX(v); apiSetX(v).catch(()=>{})`). Rejected values stay displayed, and the max-tokens input clamps per keystroke (8000 → 256 → 2560 …), sending a POST each time. **Design:** `useSettingField(get,set)` with debounce, serialized writes, and revert/echo.

### 2-I. Persistence and security safety

- **P2 The migration runner isn't transactional.** pysqlite emits no `BEGIN` before DDL, so a partial failure commits the leading `ALTER`/`CREATE INDEX` but not the migration row. The next start then fails permanently on a non-idempotent statement (reproduced on a scratch DB). **Design:** the SQLAlchemy pysqlite recipe (`isolation_level=None` + explicit `BEGIN`), one transaction per migration including its record, and stop swallowing "duplicate column" errors.
- **P2 Vault writes aren't atomic** (`secrets.py:97`: `write_text` then `chmod`), and any parse error is reported as "Incorrect password". A crash during `save_secret` leaves a truncated vault, and the only remedy is Reset, which is itself broken (P0-7). **Design:** a shared `atomic_write(path, bytes, 0o600)` (prefs and LLM config already do this), with "corrupt vault" distinguished from a wrong password.
- **P2 Unauthenticated `/api/auth/*` mutators with no Host/Origin check outside MCP.**
  - Any web page can POST `/api/auth/logout` (deletes the Telegram session) or `/setup` (overwrites API credentials and stacks a client).
  - With DNS rebinding, a page can read `/status` and `/resolve-1password`, which runs `op read <any ref>`.

  **Design:** require the token on all auth routes except vault-status/init/unlock, plus app-wide Host validation and Origin checks on mutating requests.
- **P2 Secrets and claims.**
  - `scam_detection_api_key`/`shield_api_key` sit in plaintext `prefs.json`, and `GET /api/settings/prefs` returns them raw, defeating the masked endpoint.
  - `Unlock.tsx:96` claims data is encrypted at rest, but `mingly.db` is plaintext.
  - Cloud model bundles are unpickled by `joblib` with no signature check, and the URL may be http.
  - Manifest `fname` is joined onto `DATA_DIR` without containment, so `..` paths escape.
  - `_evaluate_short_circuit` `eval`s node output in an f-string (latent today, but prompt-injectable once a cloud pipeline adds an LLM short-circuit).

  **Design:** keys in the vault and whitelisted prefs output; signed manifest and bundles over https; `resolve().is_relative_to()` checks; an AST evaluator instead of `eval`. Fix the UI copy, or adopt SQLCipher.
- **P3** Shield analytics counters do read-add-write and have no unique `period_start`, so they lose increments and can create duplicate day rows. Cloud sync has no change tracking: re-scores and manual corrections after the first upload never reach the cloud, and sessions are held across 30s POSTs. Use an outbox or `updated_at > synced_at` comparison, plus `UPDATE … SET n = n + :d` / `ON CONFLICT`.

---

## 3. Further findings by area

Findings that aren't already covered above.

### Chats / compose / media

- **P2 Topic view leaks other topics.** load-more, WS appends, reload-after-send and `/api/compose/send` (no topic param, so text typed in topic X posts to General) all ignore the topic. General-topic messages get `topic_id=NULL` (`sync.py:971`). `save_message` never back-fills `topic_id`. Make `{chatId, topicId}` the view key (§2-B/2-C), send with `reply_to=top_msg_id`, and map General explicitly.
- **P2 Albums, documents and GIFs are partly unrenderable.**
  - `MediaAlbumGrid.tsx:14` drops items without `media_url`, so video albums and old photo albums render empty. Album videos render as `<img src=*.mp4>`.
  - The album branch has no reply/react/delete/forward and no sender name; captions use plain `text`.
  - `media.py:28-36` excludes `document`/`gif`, so `DocumentCard` clicks do nothing.
  - Fix: route every media item through one `MediaView` dispatch inside the standard bubble chrome.
- **P2 Compose problems.**
  - Failed sends and edits are silent (`ComposeBar.tsx:153-175`, try/finally with no catch).
  - Edit mode overwrites the draft, and cancelling clears it; the edit text can be persisted as the draft.
  - Multi-file send is N separate `send_file` calls, so a retry after a mid-way failure duplicates files.
  - `/send` doesn't persist `reply_to`/`topic_id`.
  - Fix: separate draft/editBuffer state, inline errors, `send_file(list)` with a client nonce for idempotency.
- **P2 Telegram search cost and correctness.** Each debounced keystroke runs SearchGlobal + 2×`iter_dialogs(200)` + `contacts.Search` (`chats.py:280-457`), a FloodWait magnet. Search dialogs locally and use Telegram only for global lookup. Message-search clicks don't jump to the match. `search_messages_global` labels every supergroup as "channel", because `hasattr(c,'broadcast')` is always true.
- **P2 Re-render cost.** Every `user_typing` event for any chat re-renders the entire Chats page: re-sort, regroup, every un-memoised `MessageBubble`, plus `FormattedText` regexes per render. The list is not virtualized. Split into `ChatListPane` / `ChatPane` / a presence component; memoise `MessageBubble` with stable callbacks; virtualize (react-virtuoso handles reverse lists).
- **P3 Smaller issues.**
  - Reactions don't update aggregate counts.
  - Reply quotes aren't clickable.
  - `handlePinMessage` is never wired, so there is no way to pin.
  - The drop overlay's parent isn't `relative`.
  - `ChatAvatar` duplicates `SenderAvatar`.
  - Media players duplicate download/seek/`formatDuration` logic.

### Contacts / orgs / workspaces

- **P2 Adding a contact from Profile hides its positions.** `contacts.py:256` returns `positions: []` for a new contact. Re-adding then gives an unhandled 409.
- **P2 `PersonCard` never resolves data.** All 7 call sites pass `name`, so `useResolvedPerson` is skipped: status, current position and star never show; the person cache and the `/resolve*` endpoints are dead. The contact link goes to `/contacts` without selecting the person. `key={m.contact_id}` is null for non-contacts, giving duplicate keys. Resolve per missing field, use a keyed store invalidated on mutation, and key members by `telegram_id`.
- **P2 The contacts list is capped at 100** (`client.ts:422`) and status tabs filter client-side, so "Needs reply" or "Stalled" miss everyone after the first 100 alphabetically. Filter and paginate on the server.
- **P2 Profile requires live Telegram.** `Promise.all` with `getProfile`, which returns 503 when offline and is retried 3×, so local notes/positions are unreachable offline, and each visit costs 3-4 RPCs. Load local data independently and cache profile metadata.
- **P2 The workspace emoji can't be cleared** (`None` is treated as "unchanged").
- **P3 Positions routes reuse `{contact_id}` as a `telegram_id`** while their siblings use the DB id. Move them to `/api/people/{telegram_id}/positions`.
- **P3 Duplication.** Profile `PositionsSection` (~200 lines) duplicates `ContactInfoSection`'s positions block, including its bugs. Message-pane state is duplicated in Contacts, Tracked and Chats. Extract `PositionsEditor({telegramId})` and `useChatMessages`.

### Intelligence / Home

- **P2 N+1 and refetch storms.**
  - `/intelligence/mentions` makes about 200 queries per call (4 per group × 50).
  - Home refetches mentions on **every** incoming message with no debounce or sequence guard.
  - `CatchUpQueue` fetches context per item and refetches all of them every 60s, because `data` identity changes.
  - Fix: batch queries, a coalescing WS-refetch hook, a bulk `/catchup/contexts`.
- **P2 The catch-up note editor reopens empty**, so the next Save erases the note (`CatchUpQueue.tsx:326`).
- **P2 Org context recomputation.** Org context is recomputed per chat with full-table `ContactPosition ⨝ Contact` loads, and prefs getters re-read `prefs.json` from disk on every call.
- **P3 Code to split or remove.** `pages/Briefing.tsx` is dead. `Intelligence.tsx` (2,558 lines) should be split into `ChatTab`/`MentionsTab`/`BriefingTab` with shared `MentionCard`, `BriefingEntryCard` and `utils/time.ts`. Mention UIs are duplicated between Home and Intelligence, with inconsistent stale guards.

### Shield (extras)

- **P2 Chat "Check for scam" shows a stale result.** `MessageBubble.tsx:440` reports the *old* score immediately because it is non-null. `scam_check_safe` doesn't clear the red banner. `flaggedSenderIds` is loaded once and never updated by WS. Track by request id; keep flagged senders in one store shared with Shield.
- **P2 Multi-select drifts.** WS reloads don't prune `selected` and the shift-range anchor is an index into the previous list, so "N selected" doesn't match what the bulk action affects. Key selection and the anchor by id; refetch after mutations; keyset pagination.
- **P2 The ML ensemble loads on the event loop** (`joblib.load` ×9 ≈ 1.3s, ~80ms per predict) and ignores the per-model calibrated thresholds it loads. Load and predict in `to_thread`, swap atomically after sync, and apply per-model thresholds.
- **P3 Small issues.**
  - Export is silently capped at 10k rows, and export files are never cleaned up.
  - `duration_ms = monotonic()*1000` on the exception path.
  - `scam_check_requests.payload_json` grows without bound.
  - `Shield.tsx` duplicates the Sync-Now handler, the filter builder and table row rendering. Add a `useSelection` hook.
  - `clearMessageScam` is dead code.

### Infra

- **P2 Single global session token.** Unlocking in a second window invalidates the first. `App.tsx:27-34` renders the full app when the vault is unlocked but this tab has no token, which causes a 401 burst. `App.tsx:36` maps any vault-status failure (e.g. backend restarting) to "create vault". Bootstrap should validate the token; decide between a token set and an explicit single window.
- **P3 Startup and shutdown.**
  - Migrations run before uvicorn binds, but webview waits a fixed 15s, so a slow migration gives a blank window.
  - There is no shutdown hook: no `engine.dispose()` and no Telethon disconnect.
  - `_find_free_port` silently changes port when 8765 is busy, which changes the origin and resets localStorage. Use a readiness signal, a lifespan shutdown and a single-instance lock.
- **P3 Dependency floors.** `requirements.txt` allows `fastapi>=0.115`. On 0.115-0.117, yield dependencies close the DB session before a `StreamingResponse` runs (briefing regenerate). Pin `>=0.118`.

---

## 4. Checked and found sound

These were explicitly traced and need no action:

- **SQLite.** WAL, `busy_timeout=5000` and `synchronous=NORMAL` are applied per connection. No LLM or network call happens inside a *write* transaction (admin, catch-up, regenerate and scam apply all commit before or after network I/O).
- **prefs.json and LLM config.** Writes are atomic (tmp + `os.replace`). Read-modify-write is synchronous on the single loop, so no updates are lost. One caveat: a corrupt file loads as `{}` and the next save wipes it.
- **Scam data integrity.** The manual-verdict guard on automated scoring is atomic (`scam_manual_verdict IS NULL` in the UPDATE's WHERE). `ScamFlag` has no duplicates (`UNIQUE` + write lock).
- **DB constraints.** Unique constraints exist on `ChatTag`, `Contact.telegram_id`, `ContactPosition(telegram_id, organization)`, `WorkspaceColumn`, `catchup_items` and `catchup_summaries`.
- **Contact PUT** is a partial update, so a stale form can't overwrite unrelated fields.
- **Existing guards that work:**
  - Chats aborts presence, pinned and important-contact fetches on switch.
  - `ComposeBar` is keyed per chat, so drafts don't leak.
  - Enter double-submit is prevented.
  - WS `new_message` dedupes by id.
  - `messages_deleted` scopes chat-less deletions correctly.
  - The `get_offset_for_anchor` maths is correct.
  - Global search uses AbortController.
  - Profile aborts on user change.
  - Organizations, Contacts (message load), Board (tab switch), Shield (`loadScanned`) and Intelligence `MentionsTab` have sequence guards.
- **MCP.** Host/Origin validated, 404 while locked or disabled, constant-time bearer compare, every tool is a clamped SELECT.
- **Endpoint safety.** The media endpoint and export download check path containment. `/api/open-url` checks the scheme. `notifySessionExpired` has a stampede guard. `client.ts` retries only GETs.
- **LLM layer.** Registry add/remove are synchronous (atomic on the loop). The agent loop is bounded (8 rounds). Client disconnect cancels SSE tasks under Starlette (ASGI 2.3), and a cancelled briefing persists nothing.
- **Ollama install** verifies the checksum and extracts off-loop.
- **Onboarding** refresh mid-flow resumes correctly.
- **Telethon 1.45** runs on Python 3.9.6, and `Community` peers are handled (`profiles.py`).

---

## 5. Recommended order of work

1. **Safety net first (½ day).**
   - Add `ruff --select F` (or pyflakes) and a router import/smoke test to `build_app.sh`.
   - Seed an in-memory-DB test for search, briefing, the scam endpoints and forum topics.
   - Most of the P0 items fail these tests immediately, which proves the net works.
2. **P0 fixes.** These are small, mechanical changes: P0-1, P0-2, P0-3, P0-4 (via the typed loader), P0-5 (`functions.messages` + `peer=`) and P0-9.
3. **§2-A Telegram runtime owner** (with reconnect watchdog). Fixes P0-6, P0-7, client stacking, duplicate handlers and lock/teardown. Do it together with **§2-A2** `message_to_row` + real upsert + `MessageEdited` + `SyncCoordinator`, then re-sync to repair stored text.
4. **§2-H.1 WebSocket provider + §2-H.2 http client + §2-B `useLatestQuery` / `selectChat` / keyed chat store + dialog target capture.** These remove the cross-chat destructive races (delete, edit, forward, unpin).
5. **§2-D scam status/verdict model + targets-vs-context.** Also root-cause the Ollama `classify` JSON failures, which are 80% of all scans.
6. **§2-C keyset message windows + Telegram backfill.**
7. **§2-E JobManager / scan queue / atomic downloads**, then **§2-F/2-G** briefing pipeline and provider routing.
8. **§2-D kanban stage-tag ownership and the org FK migration**, then the §2-I security items.
9. Then the P2/P3 lists and the component splits (Chats, Intelligence, Shield). The splits get much easier once items 4 and 7 exist.

---

## 6. Resolution (2026-10-06)

Every finding above was implemented, then re-audited item by item against the code by a separate read-only pass. The gaps that audit found (partial fixes plus a few new bugs) were fixed in a second round. All changes are uncommitted on `main`.

**How it was verified**

- **`build_app.sh` gates** (the build stops if any fails):
  - pyflakes: undefined names and use-before-assignment;
  - scoped mypy with `check_untyped_defs`: `attr-defined`, `name-defined`, `call-arg`, `no-redef`. It catches the P0-4 class (`Message.telegram_id`);
  - 367 backend tests;
  - frontend type check of app **and** tests, plus 260 vitest tests;
  - `npm run build` and PyInstaller.
- **Test isolation:** tests run against a temp DB and temp data dir, never the user's data (`tests/conftest.py`, guarded by `tests/test_isolation.py`).
- **Migrations:** applied to a copy of the real DB, both migrated and idempotent, with `integrity_check` OK. Every migration is now one transaction, and "no such column" is tolerated only on a fresh DB.
- **Live HTTP smoke** (uvicorn with a copy of the real DB):
  - all data endpoints return 200, and unknown `/api` paths return a JSON 404;
  - Host/Origin checks, token checks, reset confirmation and Lock behave as designed.

**Decisions taken where the review offered options**

- **Hand-rolled data hooks** instead of TanStack Query: small and tested (`frontend/src/lib/README.md`).
- **`react-virtuoso`** for the chat list and message list; verified in headless Chrome.
- **Briefing conflict:** a different window while a briefing is running returns a 409 with an atomic `replace=1` option. Same window joins the running job.
- **Org delete** defaults to detach, keeping history. Positions are keyed by `organization_id`, and the user's own orgs are stored by id.
- **Logout** signs out of Telegram (server-side too) but keeps the app session. **Lock** revokes all sessions.

**Still open — needs a product decision**

- **Signed cloud model bundles (§2-I).** Bundles are https-only and sha256-checked, and all downloads go through `cloud_sync.verify_artifact`. Real signing requires the Shield cloud server to sign manifests and bundles, with a public key shipped in the app. This can't be completed client-side alone.
- **Project `CLAUDE.md` verify step.** `cd frontend && npx tsc --noEmit` type-checks nothing: the root tsconfig has `files: []`. The correct command is `npm run typecheck`, and `build_app.sh` already runs it.
