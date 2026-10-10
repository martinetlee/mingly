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
backend/                       (layers, lowest first — see "Layering" below)
  config, errors, prefs, secrets, secret_prefs, sessions, tasks, jobs, aio, …   # core
  db/           # models.py, database.py (engine/Base), startup.py (init_db), crud/ (one module per area), migrations/
  serializers.py# The JSON shapes of Conversation / Message / Person / Identity
  ws/, notify.py, media.py                                   # shared: WS broadcaster, notifications, media storage
  llm/          # Provider registry, base class, ollama/anthropic/openai providers
  people/       # identities.py (upsert + linking), linking.py (merge / undo / detach)
  ingest/       # service.py (normalized writer), fanout.py (post-save side effects + MessageInspector port)
  connectors/   # base.py (Connector contract), roles.py (optional roles), manager.py, autosync.py,
                # sync_state.py; gmail/ (+ Calendar), signal/, sms/ packages
  telegram/     # Telethon: client (connected_client), runtime, sync/coordinator/archive, media, ids.py,
                # connector.py + outgoing/dialogs/admin/directory (the role implementations)
  briefing/, scam/, ai/ (chat agent + tools), org_context, user_orgs, *_scheduler   # features
  api/          # FastAPI routers (thin), lookups.py, views.py, sse.py, idempotency.py, mcp*.py
  wiring.py     # composition root: plugs features into core extension points
  main.py       # entry point (uvicorn thread + pywebview window)

frontend/src/
  pages/        # Route pages; big screens have a sub-folder (pages/chats, mail, calendar, intelligence, settings, shield)
  features/     # Logic shared across screens, by domain (chat, calendar, mail, briefing)
  components/   # Reusable UI by domain: layout, chat, mail, media, people, channels, intelligence, shield, ui
  hooks/, context/
  api/          # client.ts (barrel over per-domain modules + base.ts request helper), types/ (per domain, index.ts)
  lib/          # framework-free plumbing: http.ts, ws.ts, capabilities, routes, toast…
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

### Layering (enforced by tests/test_architecture.py and frontend/src/__tests__/architecture.test.ts)
- Backend layers, lowest first: **core** → **data** (`db`) → **shared** (`serializers`, `ws`, `notify`, `media`) →
  **llm** → **domain** (`people`, `ingest`) → **channels** (`connectors`, `telegram`) → **features**
  (`briefing`, `scam`, `ai`, schedulers, `org_context`) → **api** (+ `wiring`) → **entry** (`main`).
  Every import (function-level included) points to the same or a lower layer; the module graph is acyclic.
  A new top-level module must be added to `LAYERS` in the test.
- `fastapi` / `starlette` only in `api/`, `ws/` and `main.py`. Lower layers raise `backend.errors.DomainError`
  subclasses (`ServiceUnavailable` 503, `NotAuthenticated` 401, `NotFound` 404, `InvalidRequest` 400,
  `RateLimited` 429) which the API maps in one handler.
- Telethon and the Telegram client/runtime are imported only inside `backend/telegram/`.
- Lower layers never import features: they expose ports and `backend/wiring.py` (run by `create_app()`)
  connects them (e.g. Shield is an `ingest.fanout.MessageInspector`).
- Prefer module access (`prefs.load_prefs()`, `cloud_sync.get_cloud_sync_manager()`) for singletons and
  functions that tests patch; a `from x import f` binds before the patch.

### Channels & connectors
- `connectors/base.py`: what every channel has (lifecycle, sign-in steps, send / send_files, react,
  mark_read, fetch_attachment, capabilities, `on_reauthorized`).
- `connectors/roles.py`: optional roles a connector also inherits — `MessageActions`, `DialogActions`,
  `GroupMembers`, `ForumTopics`, `GroupAdmin`, `HistorySync`, `GlobalSearch`, `AddressBook`.
- Routes resolve `api.lookups.connector_with(conv, Role, "What", Capability?)` (409 `unsupported_channel`
  otherwise) and wrap calls in `channel_call(...)` for the HTTP error mapping. Never branch on
  `channel == "telegram"` in a route.
- `ConnectorManager` owns lifecycles and sign-in flows; `manager.with_role(Role)` lists connectors with a role.

### API Routes
- One router per domain in `backend/api/`, registered in `router.py`; routers parse requests and shape
  responses — queries live in `db/crud`, behaviour in services (`scam/views.py`, `scam/reports.py`,
  `ai/agent.py`, …). Exception: MCP tool handlers (`api/mcp_tools.py`) own their paginated queries.
- All routes (except `/api/auth/*`) require `X-Session-Token` header
- Auth dependency: `require_session_token()` in `dependencies.py`; the token store is `backend/sessions.py`

### Database
- Schema v2 contract: `docs/multichannel/SCHEMA_V2.md` (tables, id rules, payload shapes, route map, WS events). Read it before touching ids.
- Async SQLAlchemy ORM models in `backend/db/models.py`. Every row has an internal integer id; a channel's own ids live only in `external_id` text columns and only that channel's code (`backend/telegram/ids.py`) interprets them.
- CRUD helpers in the `backend/db/crud/` package (one module per area, each with `__all__`, re-exported so callers use `crud.<name>`)
- **Migrations**: `create_all` builds the full schema on a fresh DB; later changes are append-only numbered steps in `backend/db/migrations/runner.py` `MIGRATIONS` (SQL strings or async callables). An existing DB is backed up to `DATA_DIR/backups/` before pending migrations run. A DB from an older schema generation is backed up and replaced on startup (`init_db`).

### LLM Integration
- Provider registry singleton in `backend/llm/registry.py`; `backend/llm/resolve.py` picks a provider by role
- `BaseLLMProvider` with `complete()` and `stream()` methods
- Config stored in `data/llm_config.json` (instances + orchestrator name)
- In-app AI chat: `backend/ai/agent.py` (tool-calling loop) + `backend/ai/chat_tools.py`

### Auth Flow
1. Master password → Fernet vault (`vault.enc`) with PBKDF2 key derivation
2. Session token issued on unlock, validated on every request
3. Channel credentials (Telegram API id/hash + session, …) stored encrypted in vault
4. WebSocket auth via `?token=` query param

### Frontend Data Flow
- `api/client.ts`: barrel over per-domain modules; typed `request<T>()` (`api/base.ts`) adds the session token header.
  Import API functions from `api/client` (tests mock that module).
- `api/types/`: single source of truth for all API response interfaces (import from `api/types`)
- `api/session.ts`: token storage + expiry notification

## Key Conventions

- **Python**: snake_case everywhere. **TypeScript**: camelCase vars, PascalCase components/types.
- API response shapes must match TypeScript interfaces in `frontend/src/api/types/`
- APIs, WS events and the frontend only carry internal ids (`conversation_id`, `message_id`, `person_id`, `identity_id`); never Telegram ids
- Channel operations go through the conversation's connector (`api/lookups.connector_with`), which answers 409 `unsupported_channel` when the channel lacks the role; the frontend gates such UI on `conversation.capabilities`
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
