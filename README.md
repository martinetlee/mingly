<p align="center">
  <img src="assets/logo.png" alt="Mingly" width="120" />
</p>

# Mingly

AI-assisted desktop app that helps you stay on top of important Telegram conversations — chat browser, contact & org profiles, mentions tracking, daily briefings, and AI-powered chat. All data stays on your machine.

---

## Features

### Chat Browser
- Browse all your Telegram chats synced locally
- Folder tabs to categorize chats (tracked, brief, hidden, etc.) with a slim scrollable header
- Unread message counts per chat
- Full message history with photo previews and media indicators
- Telegram-style **reply-to quote blocks** — reply context is shown inline inside the message bubble
- Reply to any message directly from the app — hover a message to reveal the reply button
- Infinite scroll to load more chats as you scroll down

### Contacts & Organizations
- Import your Telegram contacts with enriched profiles: organization, title, Twitter, LinkedIn, phone
- Add contacts from any private chat's info panel
- **Organization management**: members list, current titles, former employees with past roles
- Searchable organization dropdown in the profile editor (create new orgs inline)
- Profile pages for any Telegram user linked from group member lists or chat info

### Intelligence Page
Three tabs for staying informed:

**Chat with LLM**
- Persistent multi-conversation LLM sidebar — conversations survive page navigation
- Auto-suggested conversation titles (editable)
- Switch between past conversations from the sidebar
- LLM has read access to your synced messages via a tool

**Mentions**
- View all messages where you were @mentioned across your chats
- Shows 2 messages of context before and 1 after each mention
- Collapsed by default; click to expand
- Tracked chats are highlighted
- Filter by group name, chat tag, or time window (7 days / 14 days / 1 month)
- Messages you've already replied to are marked **[replied]**
- Click a mention to jump to that exact position in the chat with temporary highlight

**Briefing**
- Daily or weekly AI-generated summary of your important chats (configurable in Settings)
- Auto-triggers at the configured time; also triggers on app open if the scheduled run was missed
- **Tracked chats**: one-line summary, emphasizing messages involving you
- **Brief chats**: detailed summary focusing on information-dense content, links, technical discussion, and emerging topics — filters out chit-chat
- **Important contacts**: summary of recent messages from contacts you've starred as important
- Source links on each summary card to jump directly to the representative message in the chat
- Past briefings are persisted and viewable as collapsible history cards (with delete)
- Progress bar while streaming; trigger a manual update anytime

### Contacts & Profiles
- Mark any Telegram user as **important** (⭐ star) from their profile page or the Contacts panel — important contacts are included in daily/weekly briefings

### Settings
- **General**: configure briefing schedule (daily / weekly) and the time it auto-runs
- **Notifications**: All messages / Tracked chats only / Off
- **AI Models**: configure Ollama endpoint and model, start/stop the Ollama service
- **Tags**: manage reusable chat tags (name, color, category); assign tags to chats from the chat info panel with AI-powered tag suggestions

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

## Project Structure

```
mingly/
├── backend/
│   ├── main.py           # Entry point (dev + pywebview desktop mode)
│   ├── api/              # FastAPI routers
│   │   ├── chats.py      # Chat + message endpoints (sync, send, reply)
│   │   ├── contacts.py   # Contact CRUD + position management
│   │   ├── organizations.py  # Org CRUD + member/former employee management
│   │   ├── intelligence.py   # Mentions, LLM chat, briefing SSE stream
│   │   ├── settings.py   # Prefs, briefing schedule, notification settings
│   │   └── tags.py       # Chat tag/folder management
│   ├── telegram/         # Telethon client, message sync, event handlers
│   ├── llm/              # Multi-LLM registry + Ollama provider
│   ├── ai/               # Importance scoring
│   ├── db/
│   │   ├── database.py   # SQLAlchemy engine + migrations
│   │   ├── models.py     # ORM models (Message, Chat, Contact, Org, …)
│   │   └── crud.py       # Async DB read/write helpers
│   ├── config.py         # DATA_DIR (~/Library/Application Support/Mingly when bundled)
│   └── secrets.py        # Fernet-encrypted vault (vault.enc)
├── frontend/
│   └── src/
│       ├── pages/        # Chats, Intelligence, Contacts, Organizations, Settings, Profile, Onboarding, Unlock
│       ├── components/   # MessageBubble, ComposeBar, MessageList, ChatInfo, …
│       └── api/          # Typed fetch wrappers + SSE helpers
├── mingly.spec           # PyInstaller spec
├── build_app.sh          # One-command build script
├── run_dev.py            # Dev server launcher
└── requirements.txt
```

---

## Data Storage

All data is stored locally under `~/Library/Application Support/Mingly/` (app) or `data/` (dev).

| Data | File |
|---|---|
| Messages, chats, contacts, orgs | `mingly.db` (SQLite) |
| Encrypted credentials + session | `vault.enc` (Fernet-encrypted) |
| Notification, briefing & UI preferences | `prefs.json` |
| Downloaded media thumbnails | `media/` |

---

## Notifications

Mingly sends native macOS notifications when new messages arrive. Configure the preference in **Settings → Notifications** (All messages / Tracked chats only / Off).

---

## AI Models

Mingly uses a local LLM via [Ollama](https://ollama.com) for:
- **Message importance scoring** — rates messages 0–1 with a reason
- **LLM chat** in the Intelligence page — ask questions about your conversations
- **Briefing generation** — summarizes tracked and brief chats daily or weekly

Configure the Ollama endpoint and model in **Settings → AI Models**. You can start/stop the Ollama service from the same page.

---

## Privacy

Mingly is designed as a **fully local, offline-first application**. Your data never leaves your device unless you explicitly configure it to.

### What stays on your machine

| Data | Where it lives |
|---|---|
| Telegram messages, chats, contacts | `mingly.db` (SQLite, local) |
| Telegram API credentials & session | `vault.enc` (Fernet-encrypted, local) |
| App preferences | `prefs.json` (local) |
| Downloaded media | `media/` folder (local) |

### Credentials & Encryption

- Your **Telegram API ID/hash** and **session token** are encrypted at rest using [Fernet](https://cryptography.io/en/latest/fernet/) (AES-128-CBC + HMAC-SHA256), derived from a master password you set on first launch via PBKDF2.
- The master password itself is **never stored**. Only the encrypted vault file `vault.enc` is written to disk.
- Decrypted credentials are held only in memory while the app is running and discarded when it exits.

### Telegram Connection

- Mingly connects to Telegram's servers using the official [Telethon](https://docs.telethon.dev) library, which implements the MTProto protocol. This is the same protocol used by all official Telegram clients.
- **No messages or metadata are relayed through any third-party server.** The connection is directly between your machine and Telegram's infrastructure.
- Mingly only downloads messages from chats it has been explicitly told to sync (via the Sync button). It does not scan or read all your Telegram data automatically.

### AI / LLM

- Mingly uses a **locally running** LLM via [Ollama](https://ollama.com). All inference runs on your machine — no message content is sent to any external AI service.
- If you configure a custom Ollama endpoint pointing to a remote server, message excerpts used for summarization and briefing will be sent to that server. Use a local endpoint to keep everything private.

### Network Requests

The only external network connections Mingly makes are:
1. **Telegram's MTProto servers** — to sync messages and send replies.
2. **Your configured Ollama endpoint** — for LLM inference (local by default).

No analytics, telemetry, crash reporting, or third-party trackers of any kind are included.
