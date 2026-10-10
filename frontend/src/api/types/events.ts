/** WebSocket events pushed by the backend. */

import type { ArchiveStatus } from './archive'
import type { ChannelName, ConnectorState } from './channels'
import type { AttachmentKind, Message } from './messages'
import type { ScamCheckSource, ScamFlagChange, ScamManualVerdict, ScamMsgState } from './shield'

type ScamCheckEventBase = {
  request_id: string
  source: ScamCheckSource
  conversation_id: number
  message_ids: number[]
  identity_ids: number[]
  messages: ScamMsgState[]
}

export type WSEvent =
  /** message_id is null for a new-conversation ping */
  | { type: 'new_message'; conversation_id: number; message_id: number | null; conversation_title?: string | null; sender_name?: string | null; text?: string; is_tracked?: boolean; is_outgoing?: boolean; message?: Message; /** The conversation's channel (per-channel notification rules) */ channel?: ChannelName | null }
  | { type: 'importance_update'; message_id: number; score: number; reason: string }
  | { type: 'scan_complete'; scanned: number; flagged: number }
  | { type: 'summary_ready'; conversation_id: number }
  | { type: 'message_sent'; conversation_id: number; message_id: number }
  | { type: 'message_send_failed'; conversation_id: number; error: string }
  | { type: 'research_complete'; entity: 'person' | 'organization'; id: number }
  | { type: 'user_typing'; identity_id: number; conversation_id: number; name: string }
  | { type: 'user_status'; identity_id: number; status: string; last_seen?: string | null }
  /** Exactly one of scam_detected / scam_check_safe / scam_check_error per checked message (terminal). */
  | ({ type: 'scam_detected'; pipeline_is_scam: boolean; /** raw pipeline confidence, display only */ confidence: number; reason: string; category: string | null } & ScamCheckEventBase)
  | ({ type: 'scam_check_safe'; pipeline_is_scam: boolean; confidence: number; reason: string; category: string | null } & ScamCheckEventBase)
  | ({ type: 'scam_check_error'; error: string } & Omit<ScamCheckEventBase, 'request_id'> & { request_id: string | null })
  | { type: 'scam_users_flagged'; identity_ids: number[] }
  | { type: 'scam_verdict_changed'; scope: 'message' | 'sender'; verdict: ScamManualVerdict; count: number; messages: ScamMsgState[]; flags: ScamFlagChange[] }
  | { type: 'messages_deleted'; conversation_id: number | null; message_ids: number[] }
  | { type: 'navigate_to_conversation'; conversation_id: number }
  | { type: 'conversation_synced'; conversation_id: number }
  | { type: 'conversation_sync_empty'; conversation_id: number }
  | { type: 'bulk_sync_complete'; synced: number }
  | { type: 'conversations_synced'; conversation_ids: number[] }
  | { type: 'message_edited'; conversation_id: number; message_id: number; message: Message }
  | { type: 'messages_pinned'; conversation_id: number; message_ids: number[]; pinned: boolean }
  | { type: 'media_ready'; conversation_id: number; message_id: number; attachment_id: number; kind: AttachmentKind; url: string }
  | { type: 'conversation_sync_failed'; conversation_id: number; status: 'unavailable' | 'error' | 'skipped'; error: string | null }
  | { type: 'connector_status'; channel: ChannelName; account_key: string; state: ConnectorState; error: string | null }
  | { type: 'catchup_summaries_updated'; job_id: string }
  | ({ type: 'archive_progress' } & ArchiveStatus)
  | { type: 'person_notes_changed'; person_id: number }
  /** An MCP agent changed a person's fields or positions */
  | { type: 'person_changed'; person_id: number }
  /** Google Calendar events of a Gmail account changed (refetch calendar views) */
  | { type: 'calendar_synced'; account_id: number }
  /** Client-side only: emitted by WebSocketProvider after a reconnect — events may have been missed, refetch */
  | { type: 'resync' }
