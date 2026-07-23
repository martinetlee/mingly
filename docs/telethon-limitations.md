# Telethon Limitations & Known Gaps

## Overview

We use [Telethon](https://github.com/LonamiWebs/Telethon) (Python MTProto client) for all Telegram communication. Telethon is a third-party reimplementation of the MTProto protocol — not the official library used by Telegram's own clients. This means certain features and behaviors differ from official Telegram apps.

---

## Known Limitations

### 1. Message Requests from Unknown Users (Critical)

**Issue:** Messages from unknown, non-contact users (especially verified spam/scam accounts) are sometimes not delivered to Telethon at all. The Telegram server appears to withhold these updates from third-party clients.

**Observed behavior:**
- A verified account ("Sk Roshni") sent a DM that appeared in official Telegram Desktop under "CONTACTS AND CHATS"
- Our Telethon client never received a `NewMessage` event
- The `events.Raw` handler never received any update type for this message
- The chat does not appear in `iter_dialogs()` (main or archived folder)
- The chat does not appear in `contacts.SearchRequest` results
- The chat does not appear in `messages.SearchGlobalRequest` results
- Official Telegram shows it because it uses TDLib which receives all update types

**Impact:** Some DMs from unknown users are invisible in our app. This is particularly ironic for a scam detection app, since spam/scam messages from unknown senders are exactly what we want to detect.

**Mitigations in place:**
- `events.Raw` handler catches `UpdateShortMessage` (helps with some missed messages, but not this case)
- `client.catch_up()` on startup processes missed updates
- Periodic `sync_dialogs()` every 2 minutes (main + archived folder) catches chats that appear in dialog lists
- WebSocket notification when new chats are discovered during periodic sync

**Potential fix:** Migrate from Telethon to TDLib (see "Migration Path" below).

### 2. Entity Resolution for Unknown Users

**Issue:** When Telethon receives a message from an unknown user, it may not have their entity (name, username, photo) cached. `msg.get_sender()` can return `None` or fail.

**Mitigations in place:**
- `client.get_entity(user_id)` force-fetches from server in the `NewMessage` handler
- Fallback to numeric ID display when name resolution fails

### 3. Dialog Filters (Custom Folders)

**Issue:** `iter_dialogs(folder=N)` only works with `folder=None` (main) and `folder=1` (archived). Custom dialog filter IDs (from `GetDialogFiltersRequest`) cannot be passed as folder IDs — Telethon raises "The folder you tried to use was not valid".

**Impact:** Chats that exist only in custom folders may not be found by our dialog search.

### 4. Update Delivery Blackouts

**Issue:** Telegram may not push updates for "low-priority" events (messages from non-contacts, very large channels) until the client explicitly requests them via `GetDialogs` or similar.

**Mitigations in place:**
- Periodic `sync_dialogs()` every 2 minutes forces a dialog refresh
- `client.catch_up()` on startup requests missed updates

### 5. No Access to "New Chats" / Contact Request Section

**Issue:** Official Telegram has a "New Chats" or contact request section that shows messages from users who aren't contacts. This is a client-side feature built on top of TDLib's complete update stream. Telethon has no equivalent — there's no API to fetch "pending contact requests" or "new chat requests".

---

## Why We Chose Telethon

- **Pure Python** — integrates directly with our FastAPI + async backend
- **Simple session management** — `StringSession` can be encrypted and stored in our vault
- **No compiled dependencies** — easy `pip install`, no C++ build toolchain needed
- **Async/await native** — fits our architecture
- **Good community** — well-documented, active maintenance
- **Rapid development** — got a working Telegram client in days, not weeks

---

## Migration Path: TDLib

If we need full parity with official Telegram, the path is migrating from Telethon to [TDLib](https://core.telegram.org/tdlib).

### What TDLib gives us
- Official C++ library maintained by the Telegram team
- Receives ALL updates (message requests, premium features, etc.)
- Always current with latest Telegram features
- Used by Telegram Desktop, Android, iOS, and all official clients

### What migration requires
- **Python bindings:** Use `python-telegram` or `telegram-text` packages (ctypes wrappers around the C++ library)
- **Rewrite `backend/telegram/`:** All of `client.py`, `sync.py`, event handlers — different API surface
- **Session management:** TDLib uses a file-based database directory, not a string session. Need to adapt our vault encryption approach
- **Build complexity:** Compiled C++ library needs to be bundled with our macOS .app (architecture-specific binaries for Intel + Apple Silicon)
- **Async adapter:** TDLib has its own event loop model that needs bridging to Python asyncio
- **Database conflict:** TDLib maintains its own SQLite database for caching — need to decide whether to use it or keep our own

### Estimated effort
Major refactor — essentially rewriting the entire Telegram integration layer. The API surface, session model, and event handling are fundamentally different. Estimate 2-3 weeks for a developer familiar with both libraries.

### Recommendation
Keep Telethon for now. The message request limitation is the only critical gap we've hit. If it becomes a frequent user complaint or if we need features that only TDLib supports (e.g., Telegram Premium features, stories, etc.), then plan the migration.

---

## References

- [Telethon docs](https://docs.telethon.dev/)
- [TDLib docs](https://core.telegram.org/tdlib)
- [Telethon GitHub issue: missing updates](https://github.com/LonamiWebs/Telethon/issues?q=is%3Aissue+missing+updates)
- [Telethon entity cache discussion](https://docs.telethon.dev/en/stable/concepts/entities.html)
