# Mingly — Technical Specification

## 1. Overview

Mingly is an AI-assisted desktop application that helps users stay on top of important Telegram conversations. It surfaces summaries, extracts actionable items, builds rich contact and organisation profiles, and tracks the user's relationships with people and companies — all from within Telegram activity.

**Starting platform:** macOS desktop
**Expansion path:** Windows, Linux (same stack), iOS/Android (future)

---

## 2. Goals

- Aggregate and monitor Telegram messages locally
- Hybrid importance flagging: user-defined rules + AI scoring
- AI-generated summaries and todo extraction per conversation
- Contact management: manual + AI-enriched profiles linked to Telegram
- Organisation management: company profiles, relationship status, contact roster
- Deep contact research (chat history + public web)
- Send and reply to Telegram messages from within the app
- AI-assisted message drafting (intent → draft, smart replies, tone adjustment)
- Support multiple AI providers (cloud and local)
- All data stays on the user's machine
- Distribute as a signed, notarised macOS DMG

## Non-Goals (v1)

- Mobile app (deferred)
- Multi-Telegram-account support
- Real-time message streaming to AI (batched instead)

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                  Desktop Window                          │
│            pywebview (native OS webview)                 │
│                                                          │
│         React + TypeScript frontend                      │
│   - Chat browser + importance flags                      │
│   - Summaries, todos, digest view                        │
│   - Compose + reply with AI drafting                     │
│   - Contact profiles + organisation views                │
│   - AI provider settings                                 │
└─────────────────────┬───────────────────────────────────┘
                      │ localhost HTTP + WebSocket (FastAPI)
┌─────────────────────▼───────────────────────────────────┐
│                 Python Core                              │
│                                                          │
│  ┌─────────────┐  ┌──────────────────────────────────┐  │
│  │  Telegram   │  │         AI Engine                │  │
│  │  Telethon   │  │         (LiteLLM)                │  │
│  │             │  │                                  │  │
│  │  - auth     │  │  Cloud:       Local:             │  │
│  │  - sync     │  │  Claude       Ollama             │  │
│  │  - stream   │  │  GPT-4o       (llama, mistral)   │  │
│  └─────────────┘  │  Gemini                          │  │
│                   │                                  │  │
│  ┌─────────────┐  │  - summarise conversations       │  │
│  │  Research   │  │  - extract todos                 │  │
│  │  Engine     │  │  - score importance              │  │
│  │             │  │  - synthesise research           │  │
│  │  - search   │  │  - draft messages                │  │
│  │             │  └──────────────────────────────────┘  │
│  │  - scrape   │                                        │
│  │  - synthesise│  ┌──────────────────────────────────┐ │
│  └─────────────┘  │  SQLite (local)                  │ │
│                   │  - messages, summaries, todos     │ │
│  ┌─────────────┐  │  - counterparty profiles         │ │
│  │  Scheduler  │  │  - importance flags              │ │
│  │  APScheduler│  │  - provider config               │ │
│  │             │  └──────────────────────────────────┘ │
│  │  - batch    │                                        │
│  │  - on-demand│  ┌──────────────────────────────────┐ │
│  └─────────────┘  │  OS Keychain (keyring)           │ │
│                   │  - Telegram session passphrase   │ │
│  ┌─────────────┐  │  - AI provider API keys          │ │
│  │  rumps      │  └──────────────────────────────────┘ │
│  │  menu bar   │                                        │
│  └─────────────┘                                        │
└─────────────────────────────────────────────────────────┘
```

---

## 4. Tech Stack

| Layer | Library | Version | Notes |
|---|---|---|---|
| Telegram client | `telethon` | latest | Pure Python MTProto |
| API server | `fastapi` + `uvicorn` | latest | Async, WebSocket support |
| AI abstraction | `litellm` | latest | Unified interface to all providers |
| Local LLM | `ollama` (via LiteLLM) | latest | User runs models locally |
| Web search | `tavily-python` | latest | AI-optimised search |
| Scraping | `playwright` + `beautifulsoup4` | latest | Deep counterparty research |
| Database | `sqlalchemy` + `aiosqlite` | latest | Async SQLite ORM |
| Scheduler | `apscheduler` | latest | Periodic + on-demand jobs |
| Desktop window | `pywebview` | latest | Native OS webview wrapper |
| Menu bar | `rumps` | latest | macOS menu bar app |
| Secrets | `keyring` | latest | OS Keychain abstraction |
| Frontend | React + TypeScript | latest | Vite, TailwindCSS |
| Packaging | `py2app` + `create-dmg` | latest | macOS DMG distribution |

---

## 5. Project Structure

```
mingly/
├── backend/
│   ├── main.py                  # App entry point, pywebview + FastAPI launcher
│   ├── api/
│   │   ├── router.py            # FastAPI route registration
│   │   ├── chats.py             # Chat + message endpoints
│   │   ├── compose.py           # Send message + AI drafting endpoints
│   │   ├── todos.py             # Todo endpoints
│   │   ├── contacts.py          # Contact CRUD + research trigger
│   │   ├── organizations.py     # Organisation CRUD + contact linking
│   │   ├── scan.py              # On-demand scan trigger
│   │   └── settings.py          # Provider config endpoints
│   ├── telegram/
│   │   ├── client.py            # Telethon session management + auth
│   │   └── sync.py              # Message fetch + incremental sync
│   ├── ai/
│   │   ├── engine.py            # LiteLLM wrapper, provider switching
│   │   ├── summarise.py         # Conversation summarisation
│   │   ├── todos.py             # Action item extraction
│   │   ├── importance.py        # Importance scoring
│   │   ├── research.py          # Counterparty synthesis prompt
│   │   └── compose.py           # Message drafting + smart replies
│   ├── research/
│   │   ├── search.py            # Tavily search queries
│   │   └── scraper.py           # Playwright page scraping
│   ├── db/
│   │   ├── database.py          # SQLAlchemy engine + session
│   │   ├── models.py            # ORM models
│   │   └── crud.py              # DB read/write helpers
│   ├── scheduler.py             # APScheduler job definitions
│   ├── secrets.py               # keyring wrapper
│   └── config.py                # App-wide constants + env
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── pages/
│   │   │   ├── Inbox.tsx        # Important messages feed
│   │   │   ├── Chats.tsx        # All monitored chats
│   │   │   ├── Todos.tsx        # Extracted action items
│   │   │   ├── Contacts.tsx     # Contact list + profiles
│   │   │   ├── Organizations.tsx# Organisation list + detail
│   │   │   └── Settings.tsx     # Provider + schedule config
│   │   ├── components/
│   │   └── api/                 # Typed fetch wrappers
│   ├── index.html
│   └── vite.config.ts
├── setup.py                     # py2app packaging config
├── build.sh                     # DMG build script
├── requirements.txt
└── SPEC.md
```

---

## 6. Database Schema

### messages
```sql
CREATE TABLE messages (
    id              INTEGER PRIMARY KEY,
    telegram_id     BIGINT NOT NULL,
    chat_id         BIGINT NOT NULL,
    sender_id       BIGINT,
    sender_name     TEXT,
    text            TEXT,
    date            DATETIME NOT NULL,
    is_outgoing     BOOLEAN DEFAULT FALSE,
    media_type      TEXT,                    -- photo, document, etc.
    importance_score FLOAT,                  -- 0.0 to 1.0, NULL = unscored
    importance_reason TEXT,
    user_flagged    BOOLEAN DEFAULT FALSE,
    scanned_at      DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### chats
```sql
CREATE TABLE chats (
    id              INTEGER PRIMARY KEY,
    telegram_id     BIGINT UNIQUE NOT NULL,
    name            TEXT,
    type            TEXT,                    -- private, group, channel
    is_monitored    BOOLEAN DEFAULT FALSE,
    last_synced_at  DATETIME,
    last_message_id BIGINT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### summaries
```sql
CREATE TABLE summaries (
    id              INTEGER PRIMARY KEY,
    chat_id         BIGINT NOT NULL,
    content         TEXT NOT NULL,
    message_range_start BIGINT,
    message_range_end   BIGINT,
    generated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    provider        TEXT,                    -- which AI generated this
    FOREIGN KEY (chat_id) REFERENCES chats(telegram_id)
);
```

### todos
```sql
CREATE TABLE todos (
    id              INTEGER PRIMARY KEY,
    chat_id         BIGINT NOT NULL,
    message_id      BIGINT,
    content         TEXT NOT NULL,
    due_date        DATE,
    is_done         BOOLEAN DEFAULT FALSE,
    source          TEXT,                    -- 'ai' or 'user'
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chat_id) REFERENCES chats(telegram_id)
);
```

### contacts
```sql
CREATE TABLE contacts (
    id                  INTEGER PRIMARY KEY,
    telegram_id         BIGINT UNIQUE,           -- NULL if contact has no Telegram
    telegram_username   TEXT,
    full_name           TEXT NOT NULL,
    email               TEXT,
    phone               TEXT,
    relationship_type   TEXT,                    -- 'client', 'prospect', 'partner',
                                                 --   'investor', 'colleague', 'personal'
    notes               TEXT,                    -- free-form user notes
    ai_profile          TEXT,                    -- JSON: AI-synthesised research profile
    last_researched_at  DATETIME,
    last_interaction_at DATETIME,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### organizations
```sql
CREATE TABLE organizations (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    website             TEXT,
    industry            TEXT,
    relationship_type   TEXT,                    -- 'client', 'prospect', 'partner',
                                                 --   'vendor', 'investor', 'other'
    relationship_status TEXT,                    -- 'active', 'inactive', 'churned', 'target'
    description         TEXT,
    notes               TEXT,                    -- free-form user notes
    ai_profile          TEXT,                    -- JSON: AI-synthesised org research
    last_researched_at  DATETIME,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### contact_organization
```sql
-- Many-to-many: a contact can work at multiple orgs over time
CREATE TABLE contact_organization (
    id              INTEGER PRIMARY KEY,
    contact_id      INTEGER NOT NULL,
    organization_id INTEGER NOT NULL,
    role            TEXT,                        -- job title at this org
    is_current      BOOLEAN DEFAULT TRUE,
    start_date      DATE,
    end_date        DATE,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    UNIQUE (contact_id, organization_id)
);
```

### interactions
```sql
-- Log of notable interactions (auto-populated from messages + user-added)
CREATE TABLE interactions (
    id              INTEGER PRIMARY KEY,
    contact_id      INTEGER,
    organization_id INTEGER,
    type            TEXT NOT NULL,               -- 'message', 'meeting', 'call', 'note'
    summary         TEXT,
    message_id      BIGINT,                      -- linked Telegram message if type='message'
    chat_id         BIGINT,
    occurred_at     DATETIME NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL,
    FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL
);
```

### vip_contacts
```sql
CREATE TABLE vip_contacts (
    id              INTEGER PRIMARY KEY,
    telegram_id     BIGINT UNIQUE NOT NULL,
    name            TEXT,
    added_at        DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### importance_rules
```sql
CREATE TABLE importance_rules (
    id              INTEGER PRIMARY KEY,
    type            TEXT NOT NULL,           -- 'keyword', 'sender', 'chat'
    value           TEXT NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### drafts
```sql
CREATE TABLE drafts (
    id              INTEGER PRIMARY KEY,
    chat_id         BIGINT NOT NULL,
    reply_to_id     BIGINT,                  -- NULL if new message, else message being replied to
    content         TEXT NOT NULL,
    source          TEXT NOT NULL,           -- 'user', 'ai_suggestion', 'ai_from_intent'
    intent          TEXT,                    -- user's original intent description (if AI-generated)
    tone            TEXT,                    -- 'formal', 'casual', 'concise' etc.
    is_sent         BOOLEAN DEFAULT FALSE,
    sent_at         DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chat_id) REFERENCES chats(telegram_id)
);
```

### provider_config
```sql
CREATE TABLE provider_config (
    id              INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,    -- 'claude', 'openai', 'ollama', etc.
    model           TEXT NOT NULL,
    is_active       BOOLEAN DEFAULT FALSE,
    base_url        TEXT,                    -- for Ollama: http://localhost:11434
    scan_interval_minutes INTEGER DEFAULT 15,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
-- API keys stored in OS Keychain, NOT here
```

---

## 7. API Endpoints

### Chats
```
GET  /chats                        # List all chats with monitoring status
POST /chats/{chat_id}/monitor      # Toggle monitoring on/off
GET  /chats/{chat_id}/messages     # Paginated messages for a chat
GET  /chats/{chat_id}/summary      # Latest AI summary
```

### Inbox
```
GET  /inbox                        # Important messages feed (scored + user-flagged)
POST /inbox/{message_id}/flag      # User manually flags a message
POST /inbox/{message_id}/unflag    # Remove user flag
```

### Todos
```
GET  /todos                        # All todos, filterable by chat/done
POST /todos                        # Create manual todo
PATCH /todos/{id}                  # Mark done, update due date
DELETE /todos/{id}                 # Delete
```

### Contacts
```
GET    /contacts                          # List all contacts (search, filter by org/type)
POST   /contacts                          # Create contact manually
GET    /contacts/{id}                     # Get contact profile + interaction history
PATCH  /contacts/{id}                     # Update name, email, notes, relationship type
DELETE /contacts/{id}                     # Delete contact
POST   /contacts/{id}/research            # Trigger AI + web research for this contact
POST   /contacts/from-telegram/{tg_id}   # Auto-create contact from Telegram user
GET    /contacts/{id}/chats               # All Telegram chats linked to this contact
GET    /contacts/{id}/interactions        # Full interaction timeline
```

### Organizations
```
GET    /organizations                     # List all orgs (search, filter by type/status)
POST   /organizations                     # Create organisation manually
GET    /organizations/{id}               # Get org profile + contacts + interaction history
PATCH  /organizations/{id}               # Update details, notes, relationship status
DELETE /organizations/{id}               # Delete org
POST   /organizations/{id}/research      # Trigger AI + web research for this org
POST   /organizations/{id}/contacts      # Link a contact to this org { contact_id, role }
DELETE /organizations/{id}/contacts/{contact_id}  # Unlink contact from org
GET    /organizations/{id}/interactions  # All interactions across all contacts in this org
```

### Compose
```
POST /compose/send                 # Send a message immediately
                                   #   body: { chat_id, text, reply_to_id? }
POST /compose/draft                # Save a draft without sending
POST /compose/ai/suggest-replies   # Get 3 AI smart reply suggestions
                                   #   body: { chat_id, context_message_ids[] }
POST /compose/ai/draft-from-intent # AI writes a full draft from user intent
                                   #   body: { chat_id, intent, tone?, reply_to_id? }
POST /compose/ai/adjust-tone       # Rewrite existing draft in a different tone
                                   #   body: { text, tone: "formal"|"casual"|"concise" }
GET  /compose/drafts/{chat_id}     # List unsent drafts for a chat
DELETE /compose/drafts/{id}        # Delete a draft
```

### Scan
```
POST /scan/now                     # On-demand full AI scan
GET  /scan/status                  # Last scan timestamp + status
```

### Settings
```
GET  /settings/providers           # List configured providers
POST /settings/providers           # Add/update provider (API key → Keychain)
DELETE /settings/providers/{name}  # Remove provider
GET  /settings/rules               # List importance rules
POST /settings/rules               # Add rule (keyword / VIP sender)
DELETE /settings/rules/{id}        # Remove rule
```

### WebSocket
```
WS   /ws                           # Push events to frontend:
                                   #   - new_message
                                   #   - importance_update
                                   #   - scan_complete
                                   #   - summary_ready
```

---

## 8. Telegram Integration

### Authentication Flow
1. User enters phone number in onboarding UI
2. Telethon sends code request to Telegram
3. User enters OTP (and 2FA password if enabled)
4. Session string generated and encrypted
5. Encryption passphrase stored in OS Keychain
6. Session string stored in SQLite (encrypted)

### Message Sync
- On startup: fetch messages from all monitored chats since `last_message_id`
- Incremental: Telethon event listener captures new messages in real-time
- New messages → local pre-filter → importance queue
- Sync stores raw messages in SQLite; AI processing is decoupled

### Monitored Chats
- User explicitly opts chats into monitoring (not all chats by default)
- Supports: private chats, group chats, channels

### Sending Messages
- `client.send_message(chat_id, text, reply_to=reply_to_id)` via Telethon
- Sent messages are immediately written to the local `messages` table with `is_outgoing=True`
- Draft is marked `is_sent=True` after successful send
- Failed sends surface an error via WebSocket event

---

## 9. Importance Pipeline

```
New message arrives
        │
        ▼
┌─────────────────────────┐
│   Local pre-filter      │  instant, zero AI cost
│                         │
│   Rules checked:        │
│   - sender in VIP list  │──── match → flag immediately
│   - keyword match       │
│   - is reply to user    │
└────────────┬────────────┘
             │ no match
             ▼
     add to scan queue
             │
      ┌──────┴──────┐
      │             │
  Periodic       On-demand
  batch scan     scan
  (interval      (app open,
  configurable)  user trigger,
                 menu bar)
      │             │
      └──────┬──────┘
             ▼
   AI importance scoring
   (per conversation batch)
             │
             ▼
   Score 0.0–1.0 + reason
   stored in SQLite
             │
             ▼
   WebSocket push → frontend
```

### AI Importance Prompt Structure
```
Given the following Telegram messages from [chat_name],
score each message's importance from 0.0 to 1.0 and provide
a one-line reason.

Consider:
- Action required from the user
- Time-sensitive content
- Financial or legal implications
- Decisions requested
- Important updates from key contacts

Messages:
[{id, sender, text, date}, ...]

Return JSON: [{id, score, reason}, ...]
```

---

## 10. AI Engine

### Provider Abstraction (LiteLLM)
```python
import litellm

async def complete(prompt: str, system: str = "") -> str:
    config = get_active_provider()          # reads from SQLite
    api_key = keyring.get_password("mingly", config.name)

    response = await litellm.acompletion(
        model=config.model,                 # "claude-sonnet-4-6",
                                            # "gpt-4o",
                                            # "ollama/llama3.2"
        api_key=api_key,
        api_base=config.base_url,           # for Ollama: localhost:11434
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content
```

### AI-Assisted Compose

Three drafting modes, all using the same `complete()` abstraction:

**1. Smart replies** — called with recent message context, returns 3 short suggestions
```
Given this Telegram conversation:
[last 10 messages]

Suggest 3 short reply options for the user. Each should be:
- Under 20 words
- Distinct in intent (e.g. agree / ask for more info / defer)

Return JSON: [{"label": "...", "text": "..."}, ...]
```

**2. Draft from intent** — user describes what they want to say, AI writes it
```
The user wants to send a message in this conversation:
[last 10 messages for context]

User's intent: "[what the user typed]"
Tone: [formal | casual | concise]

Write a single ready-to-send message. Return only the message text.
```

**3. Tone adjustment** — rewrites an existing draft
```
Rewrite the following message in a [formal | casual | concise] tone.
Keep the same meaning. Return only the rewritten message.

Original: "[draft text]"
```

All AI-generated drafts are saved to the `drafts` table before display.
The user always reviews and can edit before sending — AI never sends autonomously.

### Supported Providers (v1)
| Provider | Model examples | Key source |
|---|---|---|
| Anthropic Claude | claude-sonnet-4-6, claude-haiku-4-5 | Keychain |
| OpenAI | gpt-4o, gpt-4o-mini | Keychain |
| Google Gemini | gemini/gemini-2.0-flash | Keychain |
| Ollama (local) | ollama/llama3.2, ollama/mistral | None needed |

---

## 11. Research Engine

Research runs for both **contacts** (people) and **organisations** (companies). Same pipeline, different prompts and search queries.

### Contact Research Flow
```
User triggers research for a contact
        │
        ▼
1. Pull all Telegram messages with this contact from SQLite
        │
        ▼
2. Extract known facts from chat history (AI)
   - topics discussed, inferred role/company, tone, commitments made
        │
        ▼
3. Web search queries (Tavily)
   - "[name] [company]"
   - "[name] LinkedIn"
   - "[name] [industry]"
        │
        ▼
4. Scrape top results (Playwright)
        │
        ▼
5. AI synthesis → structured contact profile
        │
        ▼
6. Store in contacts.ai_profile (JSON)
   Auto-link to organisation if found
```

### Organisation Research Flow
```
User triggers research for an organisation
        │
        ▼
1. Pull all interactions with contacts at this org from SQLite
        │
        ▼
2. Web search queries (Tavily)
   - "[org name] company"
   - "[org name] news [current year]"
   - "[org name] LinkedIn"
   - "[org name] funding / revenue" (if relevant)
        │
        ▼
3. Scrape top results (Playwright)
        │
        ▼
4. AI synthesis → structured org profile
        │
        ▼
5. Store in organizations.ai_profile (JSON)
```

### Contact Profile JSON
```json
{
  "name": "...",
  "current_role": "...",
  "current_org": "...",
  "background": "...",
  "topics_discussed": ["..."],
  "commitments_made": ["..."],
  "communication_style": "...",
  "web_sources": ["url1", "url2"],
  "last_updated": "..."
}
```

### Organisation Profile JSON
```json
{
  "name": "...",
  "industry": "...",
  "size": "...",
  "description": "...",
  "key_people": ["..."],
  "recent_news": ["..."],
  "our_relationship_summary": "...",
  "web_sources": ["url1", "url2"],
  "last_updated": "..."
}

---

## 12. Secrets Management

| Secret | Storage |
|---|---|
| Telegram session passphrase | OS Keychain via `keyring` |
| Claude / OpenAI / Gemini API keys | OS Keychain via `keyring` |
| Telegram session data (encrypted) | SQLite |
| Tavily API key | OS Keychain via `keyring` |

```python
import keyring

APP_NAME = "mingly"

def save_secret(key: str, value: str):
    keyring.set_password(APP_NAME, key, value)

def get_secret(key: str) -> str | None:
    return keyring.get_password(APP_NAME, key)
```

API keys are **never** stored in SQLite or config files.

---

## 13. Scheduler

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()

# Periodic importance scan — interval read from provider_config table
scheduler.add_job(
    run_importance_scan,
    trigger="interval",
    minutes=get_scan_interval(),
    id="importance_scan",
    replace_existing=True
)

# Daily summary generation for monitored chats
scheduler.add_job(
    run_daily_summaries,
    trigger="cron",
    hour=7,                          # 7am local time
    id="daily_summaries"
)
```

On-demand scan exposed via `POST /scan/now` — same function, called directly.

---

## 14. Desktop App

### Entry Point
```python
# main.py
import threading
import webview
import uvicorn
from api.router import app

def start_api():
    uvicorn.run(app, host="127.0.0.1", port=8765)

if __name__ == "__main__":
    t = threading.Thread(target=start_api, daemon=True)
    t.start()
    webview.create_window("Mingly", "http://127.0.0.1:8765", width=1200, height=800)
    webview.start()
```

### Menu Bar (rumps)
```python
import rumps

class MinglyMenuBar(rumps.App):
    def __init__(self):
        super().__init__("Mingly", icon="assets/icon.png")
        self.menu = [
            rumps.MenuItem("Open Mingly", callback=self.open_window),
            rumps.MenuItem("Sync Now", callback=self.sync_now),
            rumps.separator,
            rumps.MenuItem("Quit", callback=rumps.quit_application)
        ]
```

---

## 15. Frontend Pages

| Page | Description |
|---|---|
| **Inbox** | Feed of important messages (AI-scored + user-flagged). Filter by chat, date, score threshold. Click a message to open the compose panel with smart replies pre-loaded. |
| **Chats** | All monitored chats. Click to open full conversation view with compose bar at the bottom. Supports reply-to threading. |
| **Compose Panel** | Slide-in panel available from Inbox and Chats. Shows: smart reply chips, "draft from intent" text field, tone selector, editable draft area, send button. |
| **Todos** | Action items extracted from conversations. Filter by chat, done status, due date. |
| **Contacts** | Address-book view of all contacts. Search, filter by relationship type or org. Click to open contact detail. |
| **Contact Detail** | Full profile: manual fields, linked org(s) + role, AI research profile, interaction timeline (messages + manual notes), Telegram chat links, research trigger button. |
| **Organisations** | Company list view. Filter by relationship type/status. Click to open org detail. |
| **Organisation Detail** | Org profile: manual fields, AI research, contact roster (people who work/worked there), interaction history across all linked contacts, relationship status badge. |
| **Settings** | Add AI providers, set API keys, configure scan interval, manage VIP list, keyword rules. |

### WebSocket Events (frontend listener)
```typescript
type WSEvent =
  | { type: "new_message"; chat_id: number; message_id: number }
  | { type: "importance_update"; message_id: number; score: number; reason: string }
  | { type: "scan_complete"; scanned: number; flagged: number }
  | { type: "summary_ready"; chat_id: number }
  | { type: "message_sent"; chat_id: number; message_id: number }
  | { type: "message_send_failed"; chat_id: number; error: string }
  | { type: "research_complete"; entity: "contact" | "organization"; id: number }
```

---

## 16. Contact & Organisation Model

### Relationship Types

**Contacts**
| Value | Meaning |
|---|---|
| `client` | Paying customer |
| `prospect` | Potential client |
| `partner` | Business partner |
| `investor` | Investor / VC |
| `colleague` | Internal team or peer |
| `personal` | Personal relationship |
| `vendor` | Supplier / service provider |

**Organisations**
| Value | Meaning |
|---|---|
| `client` | Paying customer org |
| `prospect` | Target org |
| `partner` | Partner org |
| `vendor` | Supplier |
| `investor` | VC / fund |
| `other` | Uncategorised |

**Relationship Status (org-level)**
`active` → `inactive` → `churned` / `target` (for prospects)

### Auto-population from Telegram

When a new Telegram chat starts with an unknown user:
1. Backend checks if a contact with that `telegram_id` exists
2. If not, prompts frontend: "Add [name] as a contact?"
3. User confirms → `POST /contacts/from-telegram/{tg_id}` creates a minimal contact record
4. User can enrich manually or trigger AI research

### Interaction Logging

Interactions are auto-logged when:
- A monitored message is received from or sent to a contact
- User adds a manual note from the Contact Detail page

The `interactions` table creates a unified timeline visible from both
the Contact Detail and Organisation Detail views.

---

## 17. Onboarding Flow

```
First launch
    │
    ▼
1. Welcome screen
    │
    ▼
2. Telegram login
   - Enter phone number
   - Enter OTP
   - Enter 2FA (if enabled)
   - Session stored securely
    │
    ▼
3. Select chats to monitor
   (list of recent chats, user toggles)
    │
    ▼
4. Configure AI provider
   - Choose provider (Claude / GPT / Gemini / Ollama)
   - Enter API key (stored in Keychain)
   - Test connection
    │
    ▼
5. Optional: add Tavily key for research
    │
    ▼
6. Set scan interval (default: 15 min)
    │
    ▼
Ready — initial sync begins
```

---

## 18. Packaging & Distribution

### Build Pipeline
```bash
# build.sh

# 1. Build React frontend
cd frontend && npm run build && cd ..

# 2. Copy frontend build into backend static folder
cp -r frontend/dist backend/static

# 3. Bundle Python app
python setup.py py2app

# 4. Sign the .app
codesign --deep --force --verify --verbose \
  --sign "Developer ID Application: <name>" \
  dist/Mingly.app

# 5. Notarise
xcrun notarytool submit dist/Mingly.app \
  --apple-id $APPLE_ID \
  --team-id $TEAM_ID \
  --password $APP_PASSWORD \
  --wait

# 6. Create DMG
create-dmg \
  --volname "Mingly" \
  --background "assets/dmg-background.png" \
  --window-size 660 400 \
  --icon-size 160 \
  --icon "Mingly.app" 180 170 \
  --app-drop-link 480 170 \
  "dist/Mingly.dmg" \
  "dist/Mingly.app"
```

---

## 19. Cross-Platform Expansion Path

| Platform | Strategy |
|---|---|
| **Windows / Linux** | Same Python backend. Replace `rumps` with `pystray`. `py2app` → `PyInstaller`. `pywebview` works on all platforms. |
| **iOS** | Expose backend as a network service. React Native frontend. Telethon session lives on desktop, iOS connects over LAN or via a sync layer. |
| **Android** | Same as iOS path. |
| **Web** | FastAPI already serves HTTP. Deploy backend to a VPS, React frontend to CDN. Requires rethinking local-only assumptions. |

---

## 20. Open Questions (deferred)

- Multi-account Telegram support
- End-to-end encrypted chat support (Telegram secret chats — TDLib handles these, Telethon has limited support)
- Notification system (macOS native notifications for high-importance messages)
- Export/backup of summaries and todos
- Self-hosted search (replace Tavily with SearXNG for fully offline research)
