/** Messages, attachments, reactions and the message window. */

import type { ScamManualVerdict, ScamStatus } from './shield'

export interface Reaction {
  emoji: string
  count: number
}

export interface ReplyPreview {
  sender_name: string | null
  text: string | null
  media_type: string | null
}

export type AttachmentKind = 'photo' | 'video' | 'video_note' | 'voice' | 'audio' | 'gif' | 'document' | 'sticker'

export interface Attachment {
  id: number
  kind: AttachmentKind
  filename: string | null
  mime_type: string | null
  size: number | null
  /** Served file; null until downloaded (POST /api/media-download/{id}) */
  url: string | null
}

export interface MessageSender {
  /** null for channel posts / anonymous admins (a name only) */
  identity_id: number | null
  person_id: number | null
  name: string | null
  avatar_url: string | null
}

/** One sender / recipient of a message (email from/to/cc/bcc) */
export interface MessageParticipant {
  identity_id: number
  person_id: number
  role: 'from' | 'to' | 'cc' | 'bcc'
  display_name: string | null
  /** The channel handle (lowercased email address) */
  external_id: string
}

export interface Message {
  id: number
  conversation_id: number
  /** The channel's own message id (data, never a key) */
  external_id: string
  /** Channel-native ordering within the conversation */
  seq: number
  sender: MessageSender | null
  /** Email subject (null elsewhere) */
  subject?: string | null
  text: string | null
  text_html: string | null
  date: string
  is_outgoing: boolean
  /** 'service' = a system line (joined, pinned, call…); `text` holds the readable description */
  kind?: 'message' | 'service'
  /** Structured content of polls, locations, contacts, forwards and service actions */
  meta?: Record<string, unknown> | null
  attachments: Attachment[]
  reactions: Reaction[] | null
  my_reaction?: string | null
  importance_score: number | null
  user_flagged: boolean
  /** Internal id of the replied-to message when stored, else null */
  reply_to_message_id?: number | null
  /** The channel's id of the replied-to message (an unstored reply target is fetched via around_external_id) */
  reply_to_external_id?: string | null
  reply_to_preview?: ReplyPreview | null
  topic_id?: number | null
  is_pinned?: boolean
  grouped_id?: number | null
  delivery_state?: 'sending' | 'sent' | 'delivered' | 'read'
  is_edited?: boolean
  fwd_from_name?: string | null
  scam_score?: number | null
  scam_reason?: string | null
  scam_manual_verdict?: ScamManualVerdict
  scam_category?: string | null
  scam_status?: ScamStatus
  scam_error?: string | null
  /** THE verdict (manual wins, else a successful scan > 0.5). Never re-derive it from scam_score. */
  effective_is_scam?: boolean
  /** From/to/cc/bcc identities (email); empty for Telegram */
  participants?: MessageParticipant[]
}

/** GET /api/conversations/{id}/messages/window — keyset window (oldest → newest) */
export interface MessageWindow {
  messages: Message[]
  has_older: boolean
  has_newer: boolean
  anchor_found: boolean
  /** Why anchor_found is false (the server already asked the channel) */
  anchor_reason?: AnchorReason | null
  syncing: boolean
}

export type AnchorReason = 'not_found' | 'unavailable' | 'rate_limited' | 'error'

/** POST /api/compose/send-files — per-file outcome, same order as uploaded */
export interface SendFileResult {
  filename: string
  status: 'sent' | 'failed'
  message_id?: number
  error?: string
}

export interface SendFilesResponse {
  /** Every file sent */
  success: boolean
  /** Some sent, some failed (HTTP 207) */
  partial?: boolean
  message_ids: number[]
  messages?: Message[]
  files?: SendFileResult[]
}
