/** Home dashboard. */

import type { CatchUpContext, CatchUpItem } from './catchup'
import type { Conversation } from './conversations'
import type { BriefingSource } from './intelligence'
import type { Person } from './people'

export interface DashboardStats {
  unread_today: number
}

export interface DashboardNeedsReply {
  conversation: Conversation
  last_message: {
    sender_name: string | null
    text: string | null
    date: string
  }
  /** Triage record (null = never triaged). Handled/snoozed filtering is done server-side. */
  triage: CatchUpItem | null
  /** Inline catch-up context (summary null = not generated yet) */
  context: CatchUpContext | null
}

export interface DashboardBriefingEntry {
  id: number
  /** null for important-person entries (link to `person_id` instead) */
  conversation_id: number | null
  person_id?: number | null
  conversation_title: string
  category: 'tracked' | 'briefing' | 'important_contact' | 'meetings'
  summary: string
  /** Citation targets for `[n]` markers (sources[n-1]) */
  sources?: BriefingSource[]
}

export interface DashboardBriefing {
  id: number
  generated_at: string
  days: number
  entries: DashboardBriefingEntry[]
}

export interface DashboardData {
  stats: DashboardStats
  needs_reply: DashboardNeedsReply[]
  /** Handled items still unanswered (≤10), newest handled first */
  recently_handled?: DashboardNeedsReply[]
  attention_people: Person[]
  latest_briefing: DashboardBriefing | null
}
