export interface AuthStatus {
  authenticated: boolean
  username?: string
  first_name?: string
  phone?: string
}

export interface Folder {
  id: number
  title: string
  chat_ids: number[]
  include_contacts: boolean
  include_non_contacts: boolean
  include_groups: boolean
  include_channels: boolean
  include_bots: boolean
}

export interface Chat {
  id: number
  telegram_id: number
  name: string
  type: 'private' | 'group' | 'channel'
  username: string | null
  is_monitored: boolean
  is_hidden: boolean
  is_briefing: boolean
  is_tracked: boolean
  last_synced_at: string | null
}

export interface Tag {
  id: number
  name: string
  color: string
  category: string | null
  is_default: boolean
}

export interface TagSuggestion {
  name: string
  color: string
  reason: string
}

export interface Reaction {
  emoji: string
  count: number
}

export interface Message {
  id: number
  telegram_id: number
  chat_id: number
  sender_id: number | null
  sender_name: string | null
  text: string | null
  text_html: string | null
  reactions: Reaction[] | null
  date: string
  is_outgoing: boolean
  media_type: string | null
  media_url: string | null
  importance_score: number | null
  user_flagged: boolean
}

export interface TelegramProfile {
  id: number
  type: 'user' | 'group' | 'channel' | 'unknown'
  first_name?: string
  last_name?: string
  title?: string
  username?: string | null
  phone?: string | null
  is_bot?: boolean
  is_self?: boolean
  is_verified?: boolean
  is_premium?: boolean
  status?: string
  bio?: string | null
  about?: string | null
  members_count?: number | null
  photo_url?: string | null
  common_chats?: { id: number; title: string }[]
}

export type WSEvent =
  | { type: 'new_message'; chat_id: number; message_id: number; chat_name?: string; sender_name?: string | null; text?: string; is_tracked?: boolean; is_outgoing?: boolean }
  | { type: 'importance_update'; message_id: number; score: number; reason: string }
  | { type: 'scan_complete'; scanned: number; flagged: number }
  | { type: 'summary_ready'; chat_id: number }
  | { type: 'message_sent'; chat_id: number; message_id: number }
  | { type: 'message_send_failed'; chat_id: number; error: string }
  | { type: 'research_complete'; entity: 'contact' | 'organization'; id: number }
