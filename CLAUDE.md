# Mandatory Rules

## PLAN BEFORE CODE — NO EXCEPTIONS
Before making ANY non-trivial code change (anything beyond a single-line typo fix), you MUST:
1. Enter plan mode with `EnterPlanMode`
2. Explore the codebase to understand all affected code paths
3. Present the plan with options, pros/cons, and tradeoffs
4. Get explicit user approval via `ExitPlanMode`
5. Only THEN write code

Violating this rule wastes the user's time with rework and inconsistent UX. If you catch yourself about to edit a file without having entered plan mode first, STOP and enter plan mode.

## VERIFY EVERY CHANGE
After making code changes, always run all three checks before reporting done:
1. `cd frontend && npm run typecheck` (app + test files; plain `npx tsc --noEmit` checks nothing — the root tsconfig has `files: []`)
2. `cd frontend && npm run build`
3. `bash build_app.sh` (from project root)

Fix any errors before presenting the change as complete.

## UX CONSISTENCY
Before implementing any feature, trace ALL user-facing code paths that touch the same concept. Every entry point for the same action must behave consistently. Check frontend components, API endpoints, WebSocket handlers, and any other triggers.

---

# Mingly — Relationship Manager

A desktop app (macOS) for managing relationships across channels — Telegram, Signal, SMS/iMessage, Gmail and Google Calendar (see `docs/multichannel/`) — powered by local LLMs. Built with FastAPI + React, packaged via PyInstaller + pywebview.

## Tech Stack

- **Backend**: FastAPI, SQLAlchemy async (SQLite/aiosqlite), Telethon (Telegram MTProto), httpx, pywebview
- **Frontend**: React 18, TypeScript, Vite, Tailwind CSS, Lucide icons
- **LLM**: Multi-provider registry — Ollama (local), Anthropic, OpenAI
- **Auth**: Fernet-encrypted vault (PBKDF2 key derivation), in-memory session tokens
- **Packaging**: PyInstaller (macOS .app bundle)

## Project Structure

```
backend/
  api/          # FastAPI routers (one per domain: conversations, people, organizations, telegram, …)
  connectors/   # Channel connector contract + manager; gmail/ (+ Calendar), signal/, sms/ packages
  people/       # linking.py: cross-channel person merge / undo / detach
  ingest/       # fanout.py: what every channel's new/edited message triggers (WS, Shield, catch-up, notify)
  db/           # models.py, crud/ (one module per area), migrations/runner.py
  serializers.py# The JSON shapes of Conversation / Message / Person / Identity
  llm/          # Provider registry, base class, ollama/anthropic/openai providers
  telegram/     # Telethon runtime, sync, archive, media; ids.py maps Telegram ids ↔ internal ids
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
cd frontend && npm run typecheck

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
- Schema v2 contract: `docs/multichannel/SCHEMA_V2.md` (tables, id rules, payload shapes, route map, WS events). Read it before touching ids.
- Async SQLAlchemy ORM models in `backend/db/models.py`. Every row has an internal integer id; a channel's own ids live only in `external_id` text columns and only that channel's code (`backend/telegram/ids.py`) interprets them.
- CRUD helpers in the `backend/db/crud/` package (one module per area, each with `__all__`, re-exported so callers use `crud.<name>`)
- **Migrations**: `create_all` builds the full schema on a fresh DB; later changes are append-only numbered steps in `backend/db/migrations/runner.py` `MIGRATIONS` (SQL strings or async callables). An existing DB is backed up to `DATA_DIR/backups/` before pending migrations run. A DB from an older schema generation is backed up and replaced on startup (`init_db`).

### LLM Integration
- Provider registry singleton in `backend/llm/registry.py`
- `BaseLLMProvider` with `complete()` and `stream()` methods
- Config stored in `data/llm_config.json` (instances + orchestrator name)
- Tool-calling orchestrator in `backend/llm/orchestrator.py`

### Auth Flow
1. Master password → Fernet vault (`vault.enc`) with PBKDF2 key derivation
2. Session token issued on unlock, validated on every request
3. Channel credentials (Telegram API id/hash + session, …) stored encrypted in vault
4. WebSocket auth via `?token=` query param

### Frontend Data Flow
- `api/client.ts`: typed `request<T>()` wrapper with session token header
- `api/types.ts`: single source of truth for all API response interfaces
- `api/session.ts`: token storage + expiry notification

## Key Conventions

- **Python**: snake_case everywhere. **TypeScript**: camelCase vars, PascalCase components/types.
- API response shapes must match TypeScript interfaces in `frontend/src/api/types.ts`
- APIs, WS events and the frontend only carry internal ids (`conversation_id`, `message_id`, `person_id`, `identity_id`); never Telegram ids
- Telegram-only operations go through `backend/api/lookups.telegram_peer()`, which answers 409 `unsupported_channel` for other channels; the frontend gates such UI on `conversation.capabilities`
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

- **Account** — one signed-in account on one channel
- **Conversation** — a chat/group/channel/email thread on an account (kind direct/group/channel/email_thread), flags: is_monitored, is_hidden, is_briefing, is_tracked
- **Message** — messages with `attachments`, reactions, importance scoring, reply threading (`reply_to_external_id`), `seq` = channel ordering
- **Person** — a human across channels (the CRM record, relationship status); **Identity** — one handle of a person on one channel
- **PersonPosition** — job history of a person
- **Organization** — Org directory with auto-generated context summaries
- **BriefingRun/Entry** — Scheduled LLM-generated briefing summaries
- **CatchUpItem/Summary** — Triage queue with LLM-generated catch-up summaries

## Adding a Migration

Change the model in `backend/db/models.py` (fresh DBs get it from `create_all`), then add a numbered tuple to the `MIGRATIONS` list in `backend/db/migrations/runner.py` for existing DBs. Steps are SQL strings or async callables taking the connection (for data transforms); each migration runs in one transaction:

```python
(
    1,
    "Description of what this migration does",
    [
        "SQL STATEMENT 1",
        transform_rows,  # async def transform_rows(conn): ...
    ],
),
```

Migrations auto-apply on server startup. Never modify existing migrations.
