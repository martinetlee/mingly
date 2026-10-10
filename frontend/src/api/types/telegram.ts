/** Telegram-only features: directory search, profiles, group administration. */

import type { Conversation, ConversationKind } from './conversations'

/** A Telegram directory hit not stored yet: open via POST /api/telegram/conversations/ensure {peer_id} */
export interface TelegramDirectoryHit {
  id: null
  peer_id: number
  channel: 'telegram'
  title: string | null
  kind: ConversationKind
  username: string | null
}

/** GET /api/telegram/search: stored hits are full Conversations, unstored ones are directory hits */
export type TelegramDirectoryResult = Conversation | TelegramDirectoryHit

/** GET /api/telegram/identities/{identity_id}/profile — live Telegram profile (Telegram-only) */
export interface TelegramProfile {
  identity_id: number
  person_id: number
  type: 'user' | 'group' | 'channel' | 'unknown'
  first_name?: string | null
  last_name?: string | null
  username?: string | null
  phone?: string | null
  is_bot?: boolean
  is_self?: boolean
  is_verified?: boolean
  is_premium?: boolean
  status?: string | null
  bio?: string | null
  avatar_url?: string | null
  /** Conversations shared with this user (always stored locally) */
  common_conversations?: { conversation_id: number; title: string | null }[]
  /** Served from the backend cache because Telegram is unreachable */
  stale?: boolean
  /** Epoch seconds of the underlying Telegram fetch */
  fetched_at?: number
}

export interface AdminPermissions {
  change_info: boolean
  post_messages: boolean
  edit_messages: boolean
  delete_messages: boolean
  ban_users: boolean
  invite_users: boolean
  pin_messages: boolean
  manage_call: boolean
  anonymous: boolean
  add_admins: boolean
}

export interface InviteLink {
  link: string
  title: string | null
  expire_date: string | null
  usage_limit: number | null
  usage: number
  request_needed: boolean
  revoked: boolean
}

export interface GroupSettings {
  title: string
  about: string
}

export interface AdminActionLog {
  id: number
  conversation_id: number
  action: string
  target_identity_id: number | null
  target_person_id?: number | null
  details: string | null
  created_at: string | null
}
