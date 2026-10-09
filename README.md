<p align="center">
  <img src="assets/logo.png" alt="Mingly" width="120" />
</p>

# Mingly

AI-assisted desktop app that helps you stay on top of the people and conversations that matter — across Telegram, Signal, SMS/iMessage, Gmail and Google Calendar. One person-centric view of everyone you talk to, chat and mail clients, mentions tracking, daily briefings, catch-up triage, and AI-powered chat. All data stays on your machine.

---

## Channels

Connect accounts in **Settings → Connections**. Each person's accounts on different
channels are linked into one person automatically when they share a verified phone
number or email (otherwise suggested for you to merge).

| Channel | How it connects | Notes |
|---|---|---|
| Telegram | Your own API credentials ([my.telegram.org](https://my.telegram.org)) + phone login | Full history, sending, reactions, admin tools |
| Signal | [signal-cli](https://github.com/AsamK/signal-cli) linked as a secondary device (`brew install signal-cli`, scan a QR code) | Messages from the time of linking on; send, reply, react |
| SMS / iMessage | Reads macOS Messages' local database (needs Full Disk Access) | Read-only |
| Gmail | Your own Google OAuth client (Desktop app) | Mail space with folders, labels, reply, compose |
| Google Calendar | Same Google sign-in as Gmail | Calendar space, meetings on person pages and in briefings |

**Setting up Gmail / Calendar:** in [Google Cloud Console](https://console.cloud.google.com/)
create a project, enable the Gmail API and Google Calendar API, configure the OAuth
consent screen (add yourself as a test user), and create an OAuth client of type
**Desktop app**. Enter its client ID and secret when adding Gmail in Connections.

## Features

### Home Dashboard
- **Catch-up triage queue** — LLM-generated summaries of conversations needing your attention, with one-click reply, snooze, or dismiss
- **Mentions tab** — all your @mentions with context, reply tracking, and keyboard shortcuts (j/k/a/r)
- **Important contacts overview** — relationship status cards showing who needs a check-in
- **Latest briefing** — preview of the most recent AI-generated briefing

### Chats (Telegram, Signal, SMS)
- Messengers in one list with channel chips (All / Telegram / Signal / SMS)
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

### Mail (Gmail)
- Email client with account switcher, Inbox / Sent / Archive / All mail and labels
- Thread view with sanitized HTML (remote images blocked until you allow a sender), attachments
- Reply, reply all, compose, archive, mark read/unread

### Calendar (Google Calendar)
- Agenda and week views across your Gmail accounts; attendees link to their person pages
- Upcoming meetings on each person page; today's meetings at the top of briefings

### People, Contacts & Organizations
- One person per human across channels: linked accounts, merge suggestions, merge / undo / detach
- Cross-channel timeline on each person page: chats, group messages, emails and meetings
- Address-book contacts with enriched profiles: organization, title, Twitter, LinkedIn, notes
- **Relationship status tracking** — auto-calculated (active, nurturing, stalled, cold, needs_reply) or manually overridden
- **Position management** — add job positions (title + organization) to any person, even non-contacts
- Mark contacts as important for inclusion in briefings and dashboard
- Organization management: members, former employees, related group chats
- **AI-generated org context** — LLM summarizes organization purpose from member data
- Auto-discover organizations from existing contact positions
- Person pages for anyone you've talked to — linked from member lists, chat info, message avatars and mail

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
- Accounts for the channels you want to connect (see [Channels](#channels)); none is required to start

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

This builds the React frontend and packages everything into `dist/Common Defense.app`.

### 3. Launch

```bash
open "dist/Common Defense.app"
# or just double-click it in Finder
```

On first launch you will be guided through:

1. **Set a master password** — encrypts every channel's credentials and sessions locally
2. **Connect an account** (optional, can be skipped) — any channel from [Channels](#channels); more later in Settings → Connections
3. **Pick an AI model**

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
│   ├── api/                 # FastAPI routers (one per domain): conversations, people,
│   │                        #   accounts, telegram, compose, calendar, intelligence, scam, …
│   ├── connectors/          # Channel connectors behind one contract (sign-in steps,
│   │   ├── base.py          #   capabilities, send / fetch attachment / react / mark read)
│   │   ├── manager.py       # Lifecycle of every connected account
│   │   ├── gmail/           # Gmail + Google Calendar (httpx, OAuth PKCE)
│   │   ├── signal/          # signal-cli JSON-RPC
│   │   └── sms/             # macOS Messages chat.db (read-only)
│   ├── telegram/            # Telethon runtime, sync, archive, media; ids.py maps Telegram ids
│   ├── ingest/              # Normalized shapes + the one writer for non-Telegram channels;
│   │                        #   fanout.py: what every new message triggers (WS, Shield, notify)
│   ├── people/linking.py    # Cross-channel person linking (merge / undo / detach)
│   ├── serializers.py       # JSON shapes of Conversation / Message / Person / Identity
│   ├── llm/                 # Multi-provider registry (Ollama, Anthropic, OpenAI)
│   ├── briefing/            # Briefing pipeline
│   ├── scam/                # Shield scam detection + cloud sync
│   ├── db/
│   │   ├── models.py        # Schema v2 (accounts, people, identities, conversations, messages, …)
│   │   ├── crud/            # Async DB helpers, one module per area
│   │   └── migrations/      # Append-only migrations (auto-applied, DB backed up first)
│   ├── config.py            # DATA_DIR (~/Library/Application Support/Mingly when bundled)
│   └── secrets.py           # Fernet-encrypted vault (vault.enc)
├── frontend/
│   └── src/
│       ├── pages/           # Home, Chats, Mail, Calendar, Person, Contacts, Organizations,
│       │                    #   Intelligence, Settings (Connections), …
│       ├── components/      # MessageBubble, EmailMessageView, AuthFlow, PersonTimeline, …
│       └── api/             # Typed fetch wrappers, SSE helpers, session management
├── docs/multichannel/       # Schema v2 contract, phase plan, Shield cloud contract v2
├── mingly.spec              # PyInstaller spec
├── build_app.sh             # One-command build script
├── run_dev.py               # Dev server launcher
└── requirements.txt
```

### Key Design Decisions

- **Internal ids, person-centric** — every row has an internal id; a channel's own ids live only in `external_id` columns read by that channel's code. A *person* owns *identities* (one per channel handle). See `docs/multichannel/SCHEMA_V2.md`.
- **Connector contract** — every channel implements the same lifecycle, sign-in steps and capabilities; the UI gates channel-specific actions on a conversation's `capabilities`.
- **Hybrid UI** — Chats for messengers, Mail for email, Calendar for meetings; people pages, Home, catch-up, briefings and search stay cross-channel.
- **Multi-provider LLM** — Ollama for local inference, Anthropic/OpenAI for cloud; switchable in Settings
- **Session-based auth** — master password → Fernet vault → in-memory session token; all API routes (except vault/auth bootstrap and the Google OAuth redirect) require `X-Session-Token`

---

## Data Storage

All data is stored locally under `~/Library/Application Support/Mingly/` (app) or `data/` (dev).

| Data | File |
|---|---|
| People, conversations, messages, emails, calendar events, orgs, briefings, catch-up state | `mingly.db` (SQLite) |
| Encrypted credentials + sessions (Telegram, Google) | `vault.enc` (Fernet-encrypted) |
| Signal device keys (signal-cli) | `signal/` (directory mode 0700) |
| Database backups before migrations / schema changes | `backups/` |
| Notification, briefing & UI preferences | `prefs.json` |
| LLM provider configuration | `llm_config.json` |
| Downloaded attachments | `media/<conversation>/` |
| Avatars | `media/avatars/` |

---

## Notifications

Mingly sends native macOS notifications when new messages arrive. Configure in **Settings → Notifications** (All messages / Tracked chats only / Off), with a separate setting per email account (off by default).

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
| Messages, emails, calendar events, people | `mingly.db` (SQLite, local) |
| Channel credentials & sessions | `vault.enc` (Fernet-encrypted, local) |
| App preferences & LLM config | `prefs.json`, `llm_config.json` (local) |
| Downloaded media & profile photos | `media/` folder (local) |

### Credentials & Encryption

- Your **channel credentials and session tokens** (Telegram API ID/hash and session, Google OAuth client and refresh tokens) are encrypted at rest using [Fernet](https://cryptography.io/en/latest/fernet/) (AES-128-CBC + HMAC-SHA256), derived from a master password you set on first launch via PBKDF2.
- The master password itself is **never stored**. Only the encrypted vault file `vault.enc` is written to disk.
- Decrypted credentials are held only in memory while the app is running and discarded when it exits.

### Channel Connections

- **Telegram:** directly to Telegram's servers with [Telethon](https://docs.telethon.dev) (MTProto).
- **Gmail / Google Calendar:** directly to Google's APIs with your own OAuth client.
- **Signal:** through your local signal-cli, linked as one of your devices; its keys stay in `signal/` (outside the encrypted vault — protect your Mac account).
- **SMS / iMessage:** read from the local Messages database; nothing is sent anywhere.
- **No messages or metadata are relayed through any third-party server.**

### AI / LLM

- When using **Ollama** (default), all inference runs locally — no data leaves your machine.
- When using **Anthropic** or **OpenAI**, message excerpts used for summarization and chat are sent to their APIs. Use a local provider to keep everything private.

### Network Requests

The only external network connections Mingly makes are:
1. **The channels you connect** — Telegram, Google (Gmail, Calendar) and Signal's servers via signal-cli.
2. **Your configured LLM endpoint** — for AI inference (local by default).
3. **The Shield cloud endpoint, only if you enable Shield cloud sync** — scam verdicts; private handles (emails, phone numbers) are sent as per-install salted hashes, and email content is not uploaded unless you allow it.

No analytics, telemetry, crash reporting, or third-party trackers of any kind are included.
