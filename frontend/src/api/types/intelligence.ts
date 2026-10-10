/** AI chat, mentions and briefings. */

import type { Conversation } from './conversations'
import type { Message } from './messages'

/** LLM chat history (/api/ai/conversations) */
export interface AiConversationSummary {
  id: number
  title: string | null
  created_at: string
  updated_at: string
  message_count: number
}

export interface AiConversationMessageItem {
  id: number
  role: string
  content: string
  created_at: string
}

export interface AiConversationDetail {
  id: number
  title: string | null
  created_at: string
  updated_at: string
  messages: AiConversationMessageItem[]
}

export interface MentionGroup {
  mention_msg: Message
  context_before: Message[]
  context_after: Message[]
  replied: boolean
  user_reply_msg: Message | null
  reply_has_gap: boolean
  conversation: Conversation & { tags: { id: number; name: string; color: string }[] }
}

export interface BriefingSource {
  conversation_id: number
  message_id: number
}

export interface BriefingSummary {
  /** null for important-person entries (link to `person_id` instead) */
  conversation_id: number | null
  person_id?: number | null
  conversation_title: string
  category: 'tracked' | 'briefing' | 'important_contact' | 'meetings'
  summary: string
  sources?: BriefingSource[]
}

export interface BriefingHistoryEntry {
  id: number
  /** null for important-person entries (link to `person_id` instead) */
  conversation_id: number | null
  person_id?: number | null
  conversation_title: string
  category: 'tracked' | 'briefing' | 'important_contact' | 'meetings'
  summary: string
  sources?: BriefingSource[]
}

export type BriefingRunStatus = 'running' | 'done' | 'partial' | 'failed'

export interface BriefingRun {
  id: number
  generated_at: string
  finished_at?: string | null
  days: number
  status?: BriefingRunStatus
  error?: string | null
  entries: BriefingHistoryEntry[]
}

/** GET /api/intelligence/briefing/status */
export type BriefingJobStatus =
  | { running: false }
  | {
      running: true
      job_id: string
      kind: 'briefing'
      started_at: number
      days: number
      trigger: 'manual' | 'scheduled'
      progress: { total?: number; current?: number; conversation_title?: string; run_id?: number | null; days?: number }
    }
