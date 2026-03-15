# Mingly — Telegram Relationship Manager

A desktop app (macOS) for managing Telegram relationships, powered by local LLMs. Built with FastAPI + React, packaged via PyInstaller + pywebview.

## Tech Stack

- **Backend**: FastAPI, SQLAlchemy async (SQLite/aiosqlite), Telethon (Telegram MTProto), httpx, pywebview
- **Frontend**: React 18, TypeScript, Vite, Tailwind CSS, Lucide icons
- **LLM**: Multi-provider registry — Ollama (local), Anthropic, OpenAI
- **Auth**: Fernet-encrypted vault (PBKDF2 key derivation), in-memory session tokens
- **Packaging**: PyInstaller (macOS .app bundle)

## Project Structure

```
backend/
  api/          # FastAPI routers (one per domain: chats, contacts, organizations, etc.)
  db/           # models.py, crud.py, migrations/runner.py
  llm/          # Provider registry, base class, ollama/anthropic/openai providers
  telegram/     # Telethon client wrapper + sync logic
  ai/           # Message importance scoring

frontend/src/
  pages/        # Route pages (Home, Chats, Contacts, Intelligence, Settings, Profile)
  components/   # Reusable UI (PersonCard, MessageBubble, CatchUpQueue, etc.)
  api/          # client.ts (typed fetch wrapper), types.ts (all interfaces), session.ts
```

## Dev Commands

```bash
# Backend (port 8765)
python3 run_dev.py

# Frontend (port 5173, proxies /api to backend)
cd frontend && npm run dev

# TypeScript check
cd frontend && npx tsc --noEmit

# Build macOS app
bash build_app.sh
```

Preview servers are configured in `.claude/launch.json` (backend + frontend).

## Architecture

### API Routes
- One router per domain in `backend/api/`, registered in `router.py`
- All routes (except `/api/auth/*`) require `X-Session-Token` header
- Auth dependency: `require_session_token()` in `dependencies.py`

### Database
- Async SQLAlchemy ORM models in `backend/db/models.py`
- CRUD helpers in `backend/db/crud.py`
- **Migrations**: Append-only numbered SQL tuples in `backend/db/migrations/runner.py` `MIGRATIONS` list. Auto-applied on startup. Currently at migration 20.

### LLM Integration
- Provider registry singleton in `backend/llm/registry.py`
- `BaseLLMProvider` with `complete()` and `stream()` methods
- Config stored in `data/llm_config.json` (instances + orchestrator name)
- Tool-calling orchestrator in `backend/llm/orchestrator.py`

### Auth Flow
1. Master password → Fernet vault (`vault.enc`) with PBKDF2 key derivation
2. Session token issued on unlock, validated on every request
3. Telegram credentials stored encrypted in vault
4. WebSocket auth via `?token=` query param

### Frontend Data Flow
- `api/client.ts`: typed `request<T>()` wrapper with session token header
- `api/types.ts`: single source of truth for all API response interfaces
- `api/session.ts`: token storage + expiry notification

## Key Conventions

- **Python**: snake_case everywhere. **TypeScript**: camelCase vars, PascalCase components/types.
- API response shapes must match TypeScript interfaces in `frontend/src/api/types.ts`
- `ContactPosition` uses `telegram_id` (BigInteger, not FK) — works for any Telegram user, not just saved contacts
- Frontend position CRUD calls pass `telegram_id` as the route identifier
- Preferences: `data/prefs.json` (notification, briefing, user orgs, etc.)
- LLM config: `data/llm_config.json`
- Org context: `backend/org_context.py` provides shared helpers for injecting colleague/org context into LLM prompts

## Data & Config

| Purpose | Dev Path | Prod Path |
|---------|----------|-----------|
| SQLite DB | `data/mingly.db` | `~/Library/Application Support/Mingly/` |
| Encrypted vault | `data/vault.enc` | same |
| Preferences | `data/prefs.json` | same |
| LLM config | `data/llm_config.json` | same |
| Media files | `data/media/` | same |

Vite proxy: `/api` → `http://localhost:8765` in dev (configured in `frontend/vite.config.ts`).

## Key Models

- **Chat** — Telegram chats (private/group/channel), flags: is_monitored, is_hidden, is_briefing, is_tracked
- **Message** — Chat messages with reactions, importance scoring, reply threading
- **Contact** — Saved Telegram contacts with relationship status tracking
- **ContactPosition** — Job history linked by `telegram_id` (not contact FK)
- **Organization** — Org directory with auto-generated context summaries
- **BriefingRun/Entry** — Scheduled LLM-generated briefing summaries
- **CatchUpItem/Summary** — Triage queue with LLM-generated catch-up summaries

## Adding a Migration

Add a numbered tuple to the `MIGRATIONS` list in `backend/db/migrations/runner.py`:

```python
(
    21,
    "Description of what this migration does",
    [
        "SQL STATEMENT 1",
        "SQL STATEMENT 2",
    ],
),
```

Migrations auto-apply on server startup. Never modify existing migrations.
