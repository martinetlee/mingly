/** Catch-up triage queue. */

import type { Attachment } from './messages'

/** POST/GET /api/catchup/generate-summaries[/status] */
export interface CatchUpSummaryJob {
  job_id?: string
  status: 'idle' | 'running' | 'done' | 'error'
  started?: boolean
  conversation_ids?: number[] | null
  total?: number
  done?: number
  summaries_generated?: number
  started_at?: string | number | null
  finished_at?: string | number | null
  error?: string | null
  llm_error?: string | null
}

export interface CatchUpItem {
  id: number
  conversation_id: number
  person_id: number | null
  status: 'pending' | 'handled' | 'snoozed'
  note: string | null
  snoozed_until: string | null
  handled_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface CatchUpContextMessage {
  message_id?: number | null
  sender_name: string | null
  text: string | null
  date: string | null
  is_outgoing: boolean
  attachments?: Attachment[]
}

export interface CatchUpContext {
  /** null when no summary has been generated yet */
  summary: string | null
  summary_messages: CatchUpContextMessage[]
  recent_messages: CatchUpContextMessage[]
  last_activity_at: string | null
  generated_at: string | null
  new_messages_since_summary: number
  /** True when the summary is the non-LLM fallback (AI was unavailable) */
  degraded?: boolean
}
