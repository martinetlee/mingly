export interface AuthStatus {
  authenticated: boolean
  username?: string
  first_name?: string
  phone?: string
}

export interface Chat {
  id: number
  telegram_id: number
  name: string
  type: 'private' | 'group' | 'channel'
  username: string | null
  is_monitored: boolean
  last_synced_at: string | null
}

export interface Message {
  id: number
  telegram_id: number
  chat_id: number
  sender_id: number | null
  sender_name: string | null
  text: string | null
  date: string
  is_outgoing: boolean
  media_type: string | null
  importance_score: number | null
  user_flagged: boolean
}

export type WSEvent =
  | { type: 'new_message'; chat_id: number; message_id: number }
  | { type: 'importance_update'; message_id: number; score: number; reason: string }
  | { type: 'scan_complete'; scanned: number; flagged: number }
  | { type: 'summary_ready'; chat_id: number }
  | { type: 'message_sent'; chat_id: number; message_id: number }
  | { type: 'message_send_failed'; chat_id: number; error: string }
  | { type: 'research_complete'; entity: 'contact' | 'organization'; id: number }
