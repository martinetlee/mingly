<p align="center">
  <img src="assets/logo.png" alt="Mingly" width="120" />
</p>

# Mingly

AI-assisted desktop app that helps you stay on top of important Telegram conversations — chat browser, contact & org profiles, mentions tracking, daily briefings, catch-up triage, and AI-powered chat. All data stays on your machine.

---

## Features

### Home Dashboard
- **Catch-up triage queue** — LLM-generated summaries of conversations needing your attention, with one-click reply, snooze, or dismiss
- **Mentions tab** — all your @mentions with context, reply tracking, and keyboard shortcuts (j/k/a/r)
- **Important contacts overview** — relationship status cards showing who needs a check-in
- **Latest briefing** — preview of the most recent AI-generated briefing

### Chat Browser
- Browse all your Telegram chats synced locally
- Folder tabs to categorize chats (tracked, brief, hidden, etc.) with a slim scrollable header
- Resizable split-pane layout with chat list + message viewer
- Unread message counts per chat
- Full message history with photo previews and media indicators
- **Sender avatars** in group chats — profile photos with initial-letter fallback, clickable to profile
- **Reply-to quote blocks** — reply context shown inline inside the message bubble
- **Emoji reactions** — hover any message to add/remove emoji reactions
- Reply to any message directly from the app
- In-chat search (Cmd+F) with date-jump picker
- Infinite scroll to load older messages
- Keyboard shortcuts: j/k (navigate), / (search), i (toggle info), Cmd+F (in-chat search)

### Contacts & Organizations
- Import Telegram contacts with enriched profiles: organization, title, Twitter, LinkedIn, phone, notes
- **Relationship status tracking** — auto-calculated (active, nurturing, stalled, cold, needs_reply) or manually overridden
- **Position management** — add job positions (title + organization) to any Telegram user, even non-contacts
- Mark contacts as important for inclusion in briefings and dashboard
- Organization management: members, former employees, related group chats
- **AI-generated org context** — LLM summarizes organization purpose from member data
- Auto-discover organizations from existing contact positions
- Profile pages for any Telegram user — linked from group member lists, chat info, or message avatars

### Intelligence Page
Four tabs for staying informed:

**Chat with LLM**
- Persistent multi-conversation LLM sidebar — conversations survive page navigation
- LLM has tool access to read chats, search messages, find contacts, and tag chats
- Tool call results displayed inline with streaming responses
- Auto-suggested conversation titles (editable)
- **Organizational awareness** — LLM knows your org memberships and colleague relationships

**Briefing**
- Daily or weekly AI-generated summary of important chats (configurable in Settings)
- **Tracked chats**: one-line summary emphasizing messages involving you
- **Brief chats**: detailed summary of information-dense content, links, and technical discussion
- **Important contacts**: summary of recent messages from starred contacts
- **Org context injection** — briefings reference colleague relationships and org membership
- Source links to jump directly to representative messages
- Past briefings persisted and viewable as collapsible history cards
- Progress bar while streaming; manual refresh anytime

**Mentions**
- All @mentions across your chats with 2 messages of context before and 1 after
- Filter by group name, chat tag, or time window (7d / 14d / 1mo)
- Messages you've replied to marked as **[replied]**
- Click a mention to jump to that exact position in the chat

**Tracked**
- Quick overview of chats you're monitoring

### Global Search
- **Cmd+K** to search across chats, contacts, organizations, and messages
- Results grouped by type with keyboard navigation (arrow keys + Enter)

### Settings
- **Notifications**: All messages / Tracked chats only / Off
- **AI Models**: Multi-provider LLM config — Ollama (local), Anthropic Claude, OpenAI
- **Briefing schedule**: Daily/weekly timing, max tokens, custom prompts
- **Catch-up interval**: How often auto-summaries regenerate
- **User organizations**: Tag yourself with orgs you belong to (used for LLM context)
- **Tags**: Manage reusable chat tags (name, color, category) with AI-powered suggestions

---

## Prerequisites

- Python 3.9+
- Node.js 18+
- A Telegram account
- Telegram API credentials (free) — get them at [my.telegram.org](https://my.telegram.org)

---

## Running as a macOS App (Recommended)

Mingly ships as a self-contained `.app` bundle. Build it once, then double-click to launch.

### 1. Install dependencies

```bash
pip install -r requirements.txt
pip install pyinstaller

cd frontend
npm install
cd ..
```

### 2. Build the app

```bash
bash build_app.sh
```

This builds the React frontend and packages everything into `dist/Mingly.app`.

### 3. Launch

```bash
open dist/Mingly.app
# or just double-click it in Finder
```

On first launch you will be guided through:

1. **Set a master password** — encrypts your credentials and Telegram session locally
2. **Enter Telegram API credentials** — get them at [my.telegram.org](https://my.telegram.org) → API development tools
3. **Sign in with your phone number** — OTP + optional 2FA

Credentials are encrypted with [Fernet](https://cryptography.io/en/latest/fernet/) (AES-128-CBC + HMAC) derived from your master password via PBKDF2. Nothing is sent to any server.

---

## Running in Development

Run the backend and frontend in two separate terminals.

**Terminal 1 — Backend (FastAPI on port 8765):**

```bash
python3 run_dev.py
```

**Terminal 2 — Frontend (Vite dev server):**

```bash
cd frontend
npm run dev
```

Open `http://localhost:5173`. The Vite proxy forwards all `/api` requests to the backend.

---

## Architecture

```
mingly/
├── backend/
│   ├── main.py              # Entry point (dev + pywebview desktop mode)
│   ├── api/                 # FastAPI routers (one per domain)
│   │   ├── auth.py          # Vault unlock, Telegram login
│   │   ├── chats.py         # Chat + message endpoints (sync, send, reply, react)
│   │   ├── contacts.py      # Contact CRUD + position management (by telegram_id)
│   │   ├── organizations.py # Org CRUD + member/former employee management
│   │   ├── intelligence.py  # Mentions, LLM chat, briefing SSE stream
│   │   ├── dashboard.py     # Home page stats, needs-reply, attention contacts
│   │   ├── profiles.py      # Telegram profile lookup + photo serving
│   │   ├── settings.py      # Prefs, briefing schedule, user orgs
│   │   ├── search.py        # Global search across chats, contacts, messages, orgs
│   │   └── tags.py          # Chat tag/folder management
│   ├── telegram/            # Telethon client, message sync, event handlers
│   ├── llm/                 # Multi-provider registry (Ollama, Anthropic, OpenAI)
│   │   ├── registry.py      # Provider singleton + config
│   │   ├── orchestrator.py  # Tool-calling LLM orchestrator
│   │   └── briefing_prompts.py  # Briefing system prompts
│   ├── ai/                  # Message importance scoring
│   ├── org_context.py       # Shared org awareness helpers for LLM features
│   ├── catchup_summary_scheduler.py  # Cronjob-powered catch-up summaries
│   ├── db/
│   │   ├── database.py      # SQLAlchemy async engine + session
│   │   ├── models.py        # ORM models (Message, Chat, Contact, ContactPosition, Org, …)
│   │   ├── crud.py          # Async DB helpers + relationship status computation
│   │   └── migrations/      # Append-only numbered SQL migrations (auto-applied)
│   ├── config.py            # DATA_DIR (~/Library/Application Support/Mingly when bundled)
│   └── secrets.py           # Fernet-encrypted vault (vault.enc)
├── frontend/
│   └── src/
│       ├── pages/           # Home, Chats, Contacts, Organizations, Intelligence, Settings, Profile, Tracked, Briefing
│       ├── components/      # MessageBubble, SenderAvatar, ComposeBar, CatchUpQueue, PersonCard, …
│       └── api/             # Typed fetch wrappers, SSE helpers, session management
├── mingly.spec              # PyInstaller spec
├── build_app.sh             # One-command build script
├── run_dev.py               # Dev server launcher
└── requirements.txt
```

### Key Design Decisions

- **ContactPosition uses `telegram_id`** (not a contact FK) — positions can be added to any Telegram user without creating a Contact record
- **Multi-provider LLM** — Ollama for local inference, Anthropic/OpenAI for cloud; switchable in Settings
- **Organizational awareness** — LLM features (chat, briefings, catch-up) inject colleague/org context from user's org memberships
- **Session-based auth** — master password → Fernet vault → in-memory session token; all API routes (except auth) require `X-Session-Token`
- **Append-only migrations** — numbered SQL tuples in `backend/db/migrations/runner.py`, auto-applied on startup

---

## Data Storage

All data is stored locally under `~/Library/Application Support/Mingly/` (app) or `data/` (dev).

| Data | File |
|---|---|
| Messages, chats, contacts, orgs, positions, briefings, catch-up state | `mingly.db` (SQLite) |
| Encrypted credentials + session | `vault.enc` (Fernet-encrypted) |
| Notification, briefing & UI preferences | `prefs.json` |
| LLM provider configuration | `llm_config.json` |
| Downloaded media thumbnails | `media/` |
| Profile photos | `media/profile_photos/` |

---

## Notifications

Mingly sends native macOS notifications when new messages arrive. Configure in **Settings → Notifications** (All messages / Tracked chats only / Off).

---

## AI Models

Mingly supports multiple LLM providers for:
- **Message importance scoring** — rates messages 0–1 with a reason
- **LLM chat** — ask questions about your conversations with tool-calling access
- **Briefing generation** — summarizes tracked and brief chats daily or weekly
- **Catch-up summaries** — cronjob-powered conversation summaries for triage
- **Org context generation** — auto-summarize organization purpose from member data
- **Tag suggestions** — AI-powered chat tag recommendations

Configure providers in **Settings → AI Models**. Supported: Ollama (local), Anthropic Claude, OpenAI.

---

## Privacy

Mingly is designed as a **fully local, offline-first application**. Your data never leaves your device unless you explicitly configure a cloud LLM provider.

### What stays on your machine

| Data | Where it lives |
|---|---|
| Telegram messages, chats, contacts | `mingly.db` (SQLite, local) |
| Telegram API credentials & session | `vault.enc` (Fernet-encrypted, local) |
| App preferences & LLM config | `prefs.json`, `llm_config.json` (local) |
| Downloaded media & profile photos | `media/` folder (local) |

### Credentials & Encryption

- Your **Telegram API ID/hash** and **session token** are encrypted at rest using [Fernet](https://cryptography.io/en/latest/fernet/) (AES-128-CBC + HMAC-SHA256), derived from a master password you set on first launch via PBKDF2.
- The master password itself is **never stored**. Only the encrypted vault file `vault.enc` is written to disk.
- Decrypted credentials are held only in memory while the app is running and discarded when it exits.

### Telegram Connection

- Mingly connects to Telegram's servers using the official [Telethon](https://docs.telethon.dev) library (MTProto protocol).
- **No messages or metadata are relayed through any third-party server.** The connection is directly between your machine and Telegram's infrastructure.

### AI / LLM

- When using **Ollama** (default), all inference runs locally — no data leaves your machine.
- When using **Anthropic** or **OpenAI**, message excerpts used for summarization and chat are sent to their APIs. Use a local provider to keep everything private.

### Network Requests

The only external network connections Mingly makes are:
1. **Telegram's MTProto servers** — to sync messages and send replies.
2. **Your configured LLM endpoint** — for AI inference (local by default).

No analytics, telemetry, crash reporting, or third-party trackers of any kind are included.
