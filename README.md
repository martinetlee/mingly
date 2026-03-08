# Mingly

AI-assisted desktop app that helps you stay on top of important Telegram conversations — summaries, todos, contact profiles, and AI-drafted replies. All data stays on your machine.

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
python3 -m backend.main
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
│   ├── api/              # FastAPI routers (auth, chats, compose, llm, settings, …)
│   ├── telegram/         # Telethon client, message sync, event handlers
│   ├── llm/              # Multi-LLM registry + Ollama provider
│   ├── ai/               # Importance scoring
│   ├── db/               # SQLAlchemy models + async CRUD
│   ├── config.py         # DATA_DIR (~/Library/Application Support/Mingly when bundled)
│   └── secrets.py        # Fernet-encrypted vault (vault.enc)
├── frontend/
│   └── src/
│       ├── pages/        # Chats, Settings, Onboarding, Unlock, …
│       └── api/          # Typed fetch wrappers
├── mingly.spec           # PyInstaller spec
├── build_app.sh          # One-command build script
└── requirements.txt
```

---

## Data Storage

All data is stored locally under `~/Library/Application Support/Mingly/` (app) or `data/` (dev).

| Data | File |
|---|---|
| Messages, chats, todos | `mingly.db` (SQLite) |
| Encrypted credentials + session | `vault.enc` (Fernet-encrypted) |
| Notification & UI preferences | `prefs.json` |
| Downloaded media thumbnails | `media/` |

---

## Notifications

Mingly sends native macOS notifications when new messages arrive. Configure the preference in **Settings → Notifications** (All messages / Tracked chats only / Off). No browser permission is required.

---

## AI Models

Mingly uses a local LLM (Qwen 2.5 via [Ollama](https://ollama.com)) for message importance scoring and reply drafting. Download and configure it from **Settings → AI Models**. Cloud providers (Claude, GPT-4o) are planned.
