/** Shield scam detection: verdicts, flags, reports and cloud sync. */

import type { ConversationKind } from './conversations'

export type ScamManualVerdict = 'scam' | 'not_scam' | null

/** null = never scanned; pending = queued with no earlier result */
export type ScamStatus = null | 'pending' | 'completed' | 'short_circuited' | 'failed'

/** The scam fields every scam API response / WS `messages[]` entry carries (backend scam_fields()). */
export interface ScamFields {
  /** Stored pipeline score (0.5 ± conf/2); null = no successful scan */
  scam_score: number | null
  scam_reason: string | null
  scam_category: string | null
  scam_manual_verdict: ScamManualVerdict
  scam_status: ScamStatus
  /** Last check error; can be set while status is completed (a re-check failed, earlier result kept) */
  scam_error: string | null
  effective_is_scam: boolean
}

/** Stored row state after a check/verdict change, keyed by message_id. */
export type ScamMsgState = ScamFields & { conversation_id: number; message_id: number; identity_id: number | null; person_id?: number | null }

export type ScamFlagStatus = 'active' | 'dismissed'

export interface ScamFlagChange { identity_id: number; person_id?: number | null; status: ScamFlagStatus | null }

export interface ScamVerdictResponse {
  status: string
  scope: 'message' | 'sender'
  verdict: ScamManualVerdict
  count: number
  messages: ScamMsgState[]
  flags: ScamFlagChange[]
}

export type ScamCheckSource = 'manual' | 'rescan' | 'realtime' | 'edit' | 'resume' | 'scan'

export interface ScamFlag {
  identity_id: number
  /** The flagged identity's person (profile link) */
  person_id?: number | null
  sender_name?: string | null
  status: ScamFlagStatus
  source: 'local' | 'cloud' | 'manual'
  confidence: number | null
  reason: string | null
  flagged_at: string
  dismissed_at?: string | null
  last_conversation_id?: number | null
  last_conversation_title?: string | null
  last_message_id?: number | null
  last_active?: string | null
  last_message?: string | null
  category?: string | null
  flagged_message_text?: string | null
  flagged_conversation_id?: number | null
  flagged_message_id?: number | null
  synced_to_cloud?: boolean
}

export interface ScannedMessage extends ScamFields {
  message_id: number
  conversation_id: number
  conversation_title: string
  sender_identity_id: number | null
  sender_person_id?: number | null
  sender_name: string | null
  text: string
  date: string | null
  scam_attempts: number
  scam_next_retry_at: string | null
  scam_updated_at: string | null
  llm_prompt: string | null
  llm_response: string | null
  trace_json: string | null
  /** true only if the latest change was uploaded */
  synced_to_cloud: boolean
}

export interface ScamQueueStats { queued: number; running: number; workers: number }

/** POST /api/scam/pipeline/sync result (status: success | partial | failed | not_configured | reloaded | no_pipeline) */
export interface CloudSyncResult {
  status: string
  error?: string | null
  errors?: string[]
  files_updated?: string[]
  reports_submitted?: number
  flags_submitted?: number
  scan_results_submitted?: number
  dismissals_submitted?: number
  analytics_submitted?: number
  version?: string
  /** legacy fields some callers still read */
  pipeline_updated?: boolean
  scenarios_updated?: boolean
  signatures_updated?: boolean
}

/** verdict_filter values accepted by GET /api/scam/scanned-messages (+ export) */
export type ScamVerdictFilter = 'scam' | 'not_scam' | 'failed' | 'pending' | 'manual' | 'unmarked' | 'all'

export interface ScamCategory {
  id: string
  label: string
  description: string
}

export interface ReportContextMessage {
  message_id: number
  sender_identity_id: number | null
  sender_name: string | null
  text: string | null
  date: string | null
  is_outgoing: boolean
  scam_score: number | null
}

export interface ScamReport {
  id: number
  report_id: string
  reported_identity_id: number
  reported_person_id?: number | null
  reported_sender_name: string | null
  reported_in_conversation_id: number
  conversation_title: string
  conversation_kind: ConversationKind | null
  reporter_verdict: string
  scam_category: string | null
  custom_category: string | null
  status: 'submitted' | 'confirmed' | 'false_positive'
  synced_at: string | null
  created_at: string
}

export interface SyncHistoryStats {
  last_synced_at: string | null
  last_sync_status: 'success' | 'partial' | 'failed' | null
  last_sync_error: string | null
  last_sync_details: {
    files_updated: string[]
    reports_submitted: number
    flags_submitted: number
    scan_results_submitted: number
    dismissals_submitted?: number
  } | null
  flags: { total: number; synced: number; pending: number }
  reports: { total: number; synced: number; pending: number }
  scans: { total: number; synced: number; pending: number; total_scanned: number; scam_detected: number; failed: number; queued?: number }
  /** total = synced + pending + skipped; skipped = never uploaded because their chat / message / sender was deleted first (cloud_skipped_reason 'source rows deleted') */
  dismissals: { total: number; synced: number; pending: number; skipped: number }
  /** A cloud sync is in flight (single-flight on the server) */
  running?: boolean
  analytics: {
    total: number
    synced: number
    pending: number
    latest: {
      period_start: string
      total_scanned: number
      scam_detected: number
      safe_detected: number
      pipeline_errors: number
      avg_pipeline_ms: number
    } | null
  }
}

export interface ScamFeedback {
  id: number
  feedback_type: string
  message: string
  related_conversation_id: number | null
  related_message_id: number | null
  related_flag_identity_id: number | null
  status: string
  synced_at: string | null
  created_at: string | null
}
