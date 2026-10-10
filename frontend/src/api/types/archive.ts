/** DM archive and history coverage. */

export type ArchiveState = 'idle' | 'running' | 'paused' | 'waiting_flood' | 'stopped'

/** GET /api/archive/status (also the `archive_progress` WS payload) */
export interface ArchiveStatus {
  state: ArchiveState
  /** While waiting on a channel rate limit (Telegram FloodWait): when the archive resumes */
  flood_wait_until: string | null
  dms: { total: number; complete: number; pending: number; failed: number; bots_skipped: number }
  current_chat: { conversation_id: number; title: string | null } | null
  started_at: string | null
  last_topup_at: string | null
  groups_queued: number
}

export interface HistoryCoverage {
  history_complete: boolean
  message_count: number
  oldest_seq: number | null
  newest_contiguous_seq: number | null
}

/** POST /api/conversations/{id}/archive-history */
export interface ChatArchiveResponse {
  queued: boolean
  coverage: HistoryCoverage
}
