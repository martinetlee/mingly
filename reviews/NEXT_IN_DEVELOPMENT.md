# Mingly vs Telegram Official: Missing Functionality Review

Reviewed against the current codebase on 2026-03-16.

## What Mingly already does well

Mingly is not a thin Telegram clone. It already covers a useful subset of Telegram plus several differentiators:

- local auth + vault flow
- local chat sync and chat browsing
- text sending
- replies
- emoji reactions
- in-chat and global search
- basic profile/member lookups
- tracked chats / briefing / mentions / catch-up workflows
- contact, org, and AI workflows that the official app does not have

If the goal is "Telegram parity", the biggest gaps are in core messenger completeness rather than in intelligence features.

## Biggest gaps vs Telegram Official

### 1. Rich message composition is still very limited

Today composition is basically text plus optional reply context. The backend send API only accepts `chat_id`, `text`, and `reply_to_id`, and uses `client.send_message(...)` without attachment handling or other send modes ([backend/api/compose.py:12](/Users/martinetlee/Project/Personal/mingly/backend/api/compose.py#L12), [backend/api/compose.py:18](/Users/martinetlee/Project/Personal/mingly/backend/api/compose.py#L18)). The UI is a textarea and send button only ([frontend/src/components/ComposeBar.tsx:5](/Users/martinetlee/Project/Personal/mingly/frontend/src/components/ComposeBar.tsx#L5), [frontend/src/components/ComposeBar.tsx:86](/Users/martinetlee/Project/Personal/mingly/frontend/src/components/ComposeBar.tsx#L86)).

Missing compared with Telegram Official:

- send photos, videos, files, voice notes, video notes, stickers, GIFs, contacts, locations, polls
- drag-and-drop or paste upload flows
- forward/share messages
- edit sent messages
- delete/revoke messages
- send as scheduled / silent send / send when online
- saved drafts per chat
- multi-select and bulk actions on messages

### 2. Media support is mostly read-only and shallow

Messages can show text, reply previews, reactions, and inline photos. Non-photo media falls back to a generic `[media_type]` label instead of a native experience ([frontend/src/components/MessageBubble.tsx:218](/Users/martinetlee/Project/Personal/mingly/frontend/src/components/MessageBubble.tsx#L218), [frontend/src/components/MessageBubble.tsx:244](/Users/martinetlee/Project/Personal/mingly/frontend/src/components/MessageBubble.tsx#L244), [frontend/src/components/MessageBubble.tsx:261](/Users/martinetlee/Project/Personal/mingly/frontend/src/components/MessageBubble.tsx#L261)).

Missing compared with Telegram Official:

- video playback UI
- audio / voice message player
- document cards with filename, size, download/open actions
- animated sticker / emoji rendering
- GIF playback
- grouped media albums
- dedicated shared media / files / links tabs in chat info
- progress UI for downloads and uploads

### 3. Core chat management is much narrower than Telegram

The chat model and chat UI focus on Mingly-specific flags like `is_hidden`, `is_briefing`, and `is_tracked`, not Telegram-native management features ([backend/db/models.py:9](/Users/martinetlee/Project/Personal/mingly/backend/db/models.py#L9), [frontend/src/pages/Chats.tsx:92](/Users/martinetlee/Project/Personal/mingly/frontend/src/pages/Chats.tsx#L92)).

Missing compared with Telegram Official:

- pin / unpin chats
- archive / unarchive chats
- mute / unmute per chat
- per-chat notification settings
- mark unread
- send-to-saved-messages flow
- create new chat / new group / new channel from the app
- join chats via invite link
- manage Telegram folders from the app instead of only reading them

### 4. Presence, delivery, and message state are not close to Telegram parity

There is unread-count handling and a local mark-read endpoint, but not the richer delivery/presence layer users expect from Telegram ([backend/api/chats.py:160](/Users/martinetlee/Project/Personal/mingly/backend/api/chats.py#L160), [frontend/src/pages/Chats.tsx:454](/Users/martinetlee/Project/Personal/mingly/frontend/src/pages/Chats.tsx#L454)).

Missing compared with Telegram Official:

- typing indicators
- sending / sent / delivered / read states on messages
- last seen / online status surfaced throughout chat UI
- message edited labels
- pinned message banners
- unread separators and jump-to-first-unread behavior

### 5. Group and channel administration is mostly absent

Mingly can list members and roles, but that is essentially read-only ([backend/api/chats.py:329](/Users/martinetlee/Project/Personal/mingly/backend/api/chats.py#L329), [frontend/src/components/ChatInfoPanel.tsx:120](/Users/martinetlee/Project/Personal/mingly/frontend/src/components/ChatInfoPanel.tsx#L120)).

Missing compared with Telegram Official:

- create/edit groups and channels
- promote/demote admins
- ban/restrict members
- approve join requests
- manage invite links
- set group/channel permissions
- channel post management
- pinned messages management
- forum topics / threaded discussions
- slow mode and moderation tooling

### 6. Telegram-native surfaces are missing entirely

These are major product areas in the official app that do not appear in the current repo:
- blocked users / privacy controls / report spam flows

## Priority recommendation

If the target is practical parity with Telegram Official for daily use, I would prioritize gaps in this order:

1. rich composition: attachments, edit, delete, forward
2. richer media rendering and playback
3. chat-management basics: archive, mute, pin, per-chat settings
4. presence/read-state UX
5. group/channel admin workflows
