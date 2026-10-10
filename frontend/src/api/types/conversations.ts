/** Conversations (chats, groups, channels, email threads) and their per-chat views. */

import type { Capability, ChannelName } from './channels'
import type { Attachment } from './messages'

export type ConversationKind = 'direct' | 'group' | 'channel' | 'email_thread'

/** GET /api/telegram/folders (Telegram-only) */
export interface Folder {
  id: number
  title: string
  conversation_ids: number[]
  include_contacts: boolean
  include_non_contacts: boolean
  include_groups: boolean
  include_channels: boolean
  include_bots: boolean
}

export interface LastMessagePreview {
  text: string
  sender_name: string | null
  date: string | null
}

export interface Conversation {
  id: number
  account_id: number
  channel: ChannelName | null
  kind: ConversationKind
  title: string | null
  username: string | null
  avatar_url: string | null
  /** The other party of a `direct` conversation */
  peer: { person_id: number; identity_id: number } | null
  is_monitored: boolean
  is_hidden: boolean
  is_briefing: boolean
  is_tracked: boolean
  is_forum: boolean
  slow_mode_seconds: number
  is_pinned: boolean
  is_archived: boolean
  is_bot: boolean
  is_deleted_account: boolean
  is_self?: boolean
  /** Every message from the conversation's first up to the newest is stored locally */
  history_complete: boolean
  is_muted: boolean
  muted_until: string | null
  is_marked_unread: boolean
  unread_count: number
  members_count?: number | null
  last_synced_at: string | null
  last_activity_at: string | null
  last_message?: LastMessagePreview
  capabilities: Capability[]
  /** Channel labels / folders (Gmail: Inbox, Sent, user labels…); empty elsewhere */
  labels?: string[]
}

/** GET /api/conversations/{id}/shared-media — a message carrying media/files/links */
export interface SharedMediaItem {
  id: number
  message_id: number
  date: string | null
  sender_name: string | null
  text: string | null
  attachments: Attachment[]
}

export interface ChatMember {
  identity_id: number
  person_id: number | null
  name: string
  username: string | null
  is_bot: boolean
  role: 'creator' | 'admin' | 'member'
}

export interface ForumTopic {
  id: number
  title: string
  icon_color: number | null
  icon_emoji_id: string | null
  date: string | null
  is_closed: boolean
  is_pinned: boolean
}

export interface ChannelPostStats {
  /** The channel's post id (always set); message_id when the post is stored */
  external_id: string
  message_id: number | null
  text: string
  date: string | null
  views: number
  forwards: number
  replies_count: number
}

export interface UserPresence {
  status: 'online' | 'offline' | 'recently' | 'last_week' | 'last_month' | 'unknown'
  last_seen?: string | null
  error?: string
}

/** POST /api/conversations/{id}/backfill */
export interface BackfillResult {
  added: number
  exhausted: boolean
  status: 'changed' | 'unchanged'
}

/** POST /api/conversations/{id}/sync — typed sync result */
export interface ConversationSyncResult {
  status: 'changed' | 'unchanged'
  conversation_id: number
  fetched: number
  added: number
  updated: number
  deleted: number
  exhausted: boolean
  flood_wait: number | null
  error: string | null
}
