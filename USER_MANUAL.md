# Mingly User Manual

Mingly is a desktop app for macOS that connects to your Telegram account and adds relationship management, AI-powered intelligence, and organizational awareness on top of the standard Telegram experience. All your data stays on your machine.

This manual covers every feature and how it compares to the standard Telegram client.

---

## Table of Contents

1. [Getting Started](#getting-started)
2. [Home Dashboard](#home-dashboard)
3. [Chat Browser](#chat-browser)
4. [Contacts](#contacts)
5. [Organizations](#organizations)
6. [Intelligence](#intelligence)
7. [Tracked Chats](#tracked-chats)
8. [Global Search](#global-search)
9. [Settings](#settings)
10. [Keyboard Shortcuts](#keyboard-shortcuts)
11. [What Mingly Adds Over Standard Telegram](#what-mingly-adds-over-standard-telegram)

---

## Getting Started

### First Launch

1. **Set a master password** — this encrypts all your credentials locally. Pick something strong; it cannot be recovered.
2. **Enter your Telegram API credentials** — get them free at [my.telegram.org](https://my.telegram.org) → API development tools. You need the API ID and API Hash.
3. **Sign in with your phone number** — enter the OTP sent to your Telegram app, plus your 2FA password if enabled.

Once authenticated, Mingly syncs your chat list and begins downloading recent messages. This may take a minute on first run.

### Unlocking

Each time you open Mingly, enter your master password to decrypt the vault and connect to Telegram.

### Setting Up AI

Go to **Settings → AI Models** to configure an LLM provider:
- **Ollama** (recommended for privacy) — runs locally, requires [Ollama](https://ollama.com) installed
- **Anthropic Claude** or **OpenAI** — cloud providers, require API keys

The AI powers briefings, catch-up summaries, importance scoring, tag suggestions, and the Intelligence chat.

---

## Home Dashboard

The Home page is your daily command center. It shows what needs your attention right now.

### Catch Up Tab

The catch-up queue shows conversations that need your reply, each with an **LLM-generated summary** so you can quickly understand what happened without reading every message.

Each card shows:
- **Chat name** and time since last message
- **AI summary** of the conversation since your last activity (includes 3 messages of pre-context)
- **New message count** since the summary was generated

**Actions on each card:**
- **Reply now** — jumps directly to the chat with the compose bar focused
- **Remind later** — snooze for 1 hour, 4 hours, 1 day, 3 days, or 1 week
- **Mark handled** — removes from the queue
- **Add note** — attach a sticky note for future reference
- **Set relationship status** (private chats) — manually set the relationship as active, nurturing, stalled, etc.

Use the **refresh button** at the top to regenerate all summaries with the latest messages.

### Mentions Tab

Shows every message where you were @mentioned across all your chats. Each mention includes:
- 2 messages of context before the mention
- 1 message of context after
- Whether you've already replied

**Filters:** Not acted / All / Acknowledged / Replied

**Keyboard shortcuts:**
- `j` / `k` — navigate between mentions
- `a` — acknowledge with an emoji reaction
- `r` — jump to the mention in its chat

### Important Contacts

Shows contacts you've starred as important, split into:
- **Needs check-in** — contacts with stalled, cold, or needs_reply status
- **All contacts** — full list with relationship status indicators

Click any contact to jump to your chat with them.

### Latest Briefing

A preview of the most recent AI-generated briefing, if available. Click to expand in the Intelligence page.

---

## Chat Browser

Navigate to **Chats** in the sidebar. The layout is a resizable split pane: chat list on the left, message viewer on the right.

### Chat List

- **Search** chats by name
- **Sort** by recent activity, unread count, or alphabetical
- **Folder tabs** across the top — your Telegram folders plus custom categories (Tracked, Brief, Hidden)
- **Unread badges** on each chat
- **Last message preview** with sender name

**Hover actions** on each chat:
- **Track** (radio icon) — add to tracked chats for monitoring
- **Hide** (eye-off icon) — remove from the default chat list
- **Briefing** (document icon) — include in daily briefing summaries

### Message Viewer

Messages display with full formatting, media previews, reply-to quote blocks, and reactions.

**In group chats**, incoming messages show:
- **Sender avatar** — profile photo (if cached) or colored initial-letter circle
- **Sender name** — clickable, links to their profile page
- Consecutive messages from the same sender are grouped (avatar shown only on the first)

**Hover on any message** to reveal action buttons:
- **Reply** — opens the compose bar with reply context
- **React** — opens an emoji picker with 12 quick reactions

**In-chat search** — press `Cmd+F` to search within the current chat. Results appear as a scrollable list; click to jump to the message (highlighted for 3 seconds). A date picker lets you jump to a specific date.

**Compose bar** at the bottom lets you type and send messages. When replying, it shows the quoted message above the input.

### Chat Info Panel

Press `i` to toggle the info panel on the right side, showing:
- Chat details (type, member count, username)
- Member list (for groups)
- Chat tags with AI-powered tag suggestions
- Track/hide/briefing toggles

---

## Contacts

Navigate to **Contacts** in the sidebar. Similar split-pane layout with contact list + detail view.

### Contact List

- **Search** by name or username
- **Filter** by relationship status: All, Needs Reply, Active, Nurturing, Stalled, Cold
- **Sync** button to import/update contacts from Telegram

### Contact Detail

Selecting a contact shows their full profile with editable fields:

**Basic Info:**
- Phone number
- Twitter handle
- LinkedIn URL
- Notes (freeform text)

**Positions:**
Add job positions to track where people work. Each position has:
- **Organization** — autocompletes from existing orgs; new orgs are auto-created
- **Title** (optional)
- **Active/Former status** — mark someone as having left, with an optional end date
- **Reactivate** — restore a former position to active

Positions are linked by Telegram ID, so you can add them to **any Telegram user** from their profile page — they don't need to be a saved contact.

**Relationship Status:**
Mingly auto-calculates relationship status based on message history:
- **Active** — recent two-way conversation
- **Nurturing** — you've been in touch, but could be more frequent
- **Stalled** — conversation has gone quiet
- **Cold** — no interaction for a long time
- **Needs reply** — they messaged you and you haven't responded (emoji reactions count as responses)

You can manually override the status. The explanation text shows why the auto status was calculated and suggests a recommended action.

**Important flag** — star a contact to include them in your daily briefing and dashboard attention list.

---

## Organizations

Navigate to **Orgs** in the sidebar.

### Organization List

- **Search** organizations
- **Create** new organizations manually
- **Auto-discover** — scans all contact positions and creates Organization entries for each unique org name

### Organization Detail

- **Name** (editable)
- **Context** — freeform description of the organization. Click **Generate with AI** to have the LLM summarize the org based on its members and related group activity.
- **Members** — current members with titles, clickable to their profiles
- **Former members** — collapsible section showing who left and when
- **Related chats** — group chats whose names match the org, with the latest message from each

### User Organizations (Settings)

In **Settings**, you can tag yourself with the organizations you belong to. This information is injected into LLM prompts so the AI knows your colleagues and organizational context when generating briefings, catch-up summaries, and chat responses.

---

## Intelligence

Navigate to **Intelligence** in the sidebar. This is the AI-powered analysis hub with multiple tabs.

### Chat Tab

An interactive conversation interface where you can ask the LLM questions about your Telegram data.

The LLM has **tool access** to:
- Read messages from any synced chat
- Search messages by text
- Search chats by name or tag
- Apply tags to chats

**Examples of what you can ask:**
- "What did the marketing team discuss this week?"
- "Summarize my conversation with Alice about the project"
- "Find all messages mentioning the Q3 deadline"
- "Tag the 'Design Review' chat as 'Projects'"

Conversations are **persistent** — they survive page navigation and are saved for later reference. You can:
- Create new conversations
- Browse past conversations in the sidebar
- Auto-generate or edit conversation titles
- Delete old conversations

The LLM also has **organizational awareness** — it knows which orgs you belong to and who your colleagues are.

### Briefing Tab

View and manage AI-generated briefings.

**Briefing content includes:**
- **Tracked chats** — one-line summaries emphasizing messages involving you
- **Briefing chats** — detailed summaries of information-dense content (filters out casual chit-chat)
- **Important contacts** — summaries of recent messages from starred contacts

Each summary card has source links to jump to representative messages in the chat.

**Briefing history** — past briefings are saved and shown as collapsible cards with timestamps. You can delete old ones.

**Manual refresh** — click the regenerate button to create a fresh briefing on demand.

### Mentions Tab

Alternative view of your @mentions (same data as the Home page mentions tab) with additional filters.

### Tracked Tab

Quick overview of chats you're monitoring, with message viewer.

---

## Tracked Chats

Navigate to **Tracked** in the sidebar for a dedicated view of chats you've marked for monitoring. Same message viewer and compose functionality as the Chats page, focused on your tracked subset.

---

## Global Search

Press **Cmd+K** (or click the Search button in the sidebar) to open global search. It searches across:

- **Chats** — by name
- **Contacts** — by name, username, or notes
- **Organizations** — by name or context
- **Messages** — full-text search in message bodies

Results are grouped by type. Use arrow keys to navigate, Enter to jump to the result.

---

## Settings

### Notifications
- **All** — native macOS notification for every incoming message
- **Tracked** — only messages from tracked chats
- **Off** — no notifications

### AI Models
Configure one or more LLM providers:
- **Ollama** — local inference (privacy-first), configure endpoint URL and model name
- **Anthropic Claude** — cloud provider, requires API key
- **OpenAI** — cloud provider, requires API key

Set which provider is the **default** (orchestrator) for all AI features.

### Briefing Schedule
- **Enable/disable** scheduled briefing generation
- **Day of week** — which day to generate (for weekly)
- **Time** — hour and minute for auto-generation
- **Max tokens** — limit summary length

### Catch-up Summary Interval
How frequently (in hours) the background cronjob regenerates catch-up summaries. Lower values mean fresher summaries but more LLM usage.

### User Organizations
Tag yourself with organizations you belong to. The LLM uses this to identify your colleagues and inject organizational context into all AI features.

### Tags
Create and manage reusable tags for organizing chats. Each tag has a name, color, and optional category. Tags can be assigned to chats from the chat info panel, with AI-powered suggestions.

---

## Keyboard Shortcuts

| Shortcut | Where | Action |
|----------|-------|--------|
| `Cmd+K` or `Cmd+/` | Anywhere | Open global search |
| `j` or `↓` | Chats, Contacts, Home | Navigate down in list |
| `k` or `↑` | Chats, Contacts, Home | Navigate up in list |
| `/` | Chats, Contacts | Focus search input |
| `i` | Chats, Contacts, Tracked | Toggle info panel |
| `Cmd+F` | Chat viewer | In-chat message search |
| `Enter` | Chat list | Select first/focused chat |
| `r` | Home (Mentions, Catch Up) | Reply / Jump to message |
| `h` | Home (Catch Up) | Mark as handled |
| `a` | Home (Mentions) | Acknowledge with emoji |
| `Escape` | Global search, dialogs | Close |

---

## What Mingly Adds Over Standard Telegram

Mingly is not a replacement for the Telegram client — it's a **relationship management layer** on top of it. Here's what it provides that standard Telegram does not:

### Relationship Intelligence
| Feature | Standard Telegram | Mingly |
|---------|-------------------|--------|
| Contact relationship status (active/stalled/cold) | No | Auto-calculated from message patterns |
| "Needs reply" detection (respects emoji reactions) | No | Yes, with dashboard visibility |
| Important contact flagging | No | Star contacts for briefing inclusion |
| Recommended actions for contacts | No | LLM suggests what to do next |

### AI-Powered Summaries
| Feature | Standard Telegram | Mingly |
|---------|-------------------|--------|
| Daily/weekly briefings | No | LLM-generated summaries of tracked chats |
| Catch-up triage queue | No | Prioritized queue with AI summaries |
| Conversation summaries | No | LLM summarizes what you missed since last activity |
| Organization context generation | No | Auto-summarize org purpose from member data |

### Organizational Awareness
| Feature | Standard Telegram | Mingly |
|---------|-------------------|--------|
| Job positions per person | No | Track title + org for any user |
| Organization directory | No | Full org management with members |
| Colleague detection | No | LLM knows your org memberships |
| Cross-org context in AI | No | Briefings and chat reference org relationships |

### Intelligence & Search
| Feature | Standard Telegram | Mingly |
|---------|-------------------|--------|
| Ask questions about your messages | No | LLM chat with tool access to your data |
| Cross-chat search | Limited | Global search across messages, chats, contacts, orgs |
| Mention tracking with context | Basic | Dedicated view with context, reply tracking, keyboard nav |
| Message importance scoring | No | LLM rates messages 0-1 with explanations |

### Workflow Features
| Feature | Standard Telegram | Mingly |
|---------|-------------------|--------|
| Snooze conversations | No | Remind later: 1h, 4h, 1d, 3d, 1w |
| Triage notes | No | Attach notes to catch-up items |
| Chat tagging | Folders only | Custom tags with colors, categories, AI suggestions |
| Relationship status history | No | Track how relationships evolve over time |

### Privacy Advantage
| Feature | Standard Telegram | Mingly |
|---------|-------------------|--------|
| Local-only data storage | Messages on Telegram servers | Everything in local SQLite |
| Local AI inference | N/A | Ollama for fully offline AI |
| No telemetry | Unknown | Zero analytics, tracking, or crash reporting |
| Encrypted credential storage | Managed by app | Fernet-encrypted vault with your master password |
