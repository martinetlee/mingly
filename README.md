# Mingly

AI-assisted desktop app that helps you stay on top of important Telegram conversations — summaries, todos, contact profiles, and AI-drafted replies. All data stays on your machine.

---

## Prerequisites

- Python 3.11+
- Node.js 18+
- A Telegram account
- Telegram API credentials (free) — get them at [my.telegram.org](https://my.telegram.org)

---

## Setup

### 1. Clone and create a virtual environment

```bash
git clone <repo-url>
cd mingly
python -m venv .venv
source .venv/bin/activate
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Install frontend dependencies

```bash
cd frontend
npm install
cd ..
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your Telegram API credentials:

```
TG_API_ID=your_api_id
TG_API_HASH=your_api_hash
```

Get these from [my.telegram.org](https://my.telegram.org) → **API development tools**.

---

## Running in Development

Run the backend and frontend in two separate terminals.

**Terminal 1 — Backend (FastAPI on port 8765):**

```bash
source .venv/bin/activate
python run_dev.py
```

**Terminal 2 — Frontend (Vite dev server):**

```bash
cd frontend
npm run dev
```

The frontend will be available at `http://localhost:5173` and connects to the backend at `http://127.0.0.1:8765`.

On first run, the backend will prompt you to authenticate with Telegram (phone number → OTP → optional 2FA). The session is stored securely in the local SQLite database.

---

## Running as Desktop App (pywebview)

This bundles the frontend into the native desktop window:

```bash
# Build the frontend first
cd frontend && npm run build && cd ..

# Copy the build into the backend static folder
cp -r frontend/dist backend/static

# Launch the desktop app
source .venv/bin/activate
python -m backend.main
```

---

## Project Structure

```
mingly/
├── backend/          # Python FastAPI backend
│   ├── main.py       # App entry point
│   ├── api/          # REST API routes
│   ├── telegram/     # Telethon client + sync
│   ├── ai/           # LLM engine + prompts
│   ├── db/           # SQLAlchemy models + CRUD
│   ├── config.py     # App config (reads .env)
│   └── secrets.py    # OS Keychain wrapper
├── frontend/         # React + TypeScript frontend
│   └── src/
│       ├── pages/    # Inbox, Chats, Todos, Contacts, etc.
│       └── api/      # Typed fetch wrappers
├── run_dev.py        # Dev backend entry point
├── requirements.txt
└── .env.example
```

---

## Data Storage

| Data | Location |
|---|---|
| Messages, chats, summaries, todos | `data/mingly.db` (SQLite) |
| AI provider API keys | OS Keychain (never written to disk) |
| Telegram session | `data/mingly.db` (encrypted) |

---

## Environment Variables

| Variable | Description |
|---|---|
| `TG_API_ID` | Telegram API ID from my.telegram.org |
| `TG_API_HASH` | Telegram API hash from my.telegram.org |

AI provider API keys (Claude, OpenAI, etc.) are entered through the **Settings** page in the app and stored in your OS Keychain — not in `.env`.
