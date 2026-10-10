/** Shield scam detection and cloud sync. */

import { request, requestBlob } from '../lib/http'
import type { CloudSyncResult, ReportContextMessage, ScamCategory, ScamFeedback, ScamFlag, ScamManualVerdict, ScamQueueStats, ScamReport, ScamVerdictFilter, ScamVerdictResponse, ScannedMessage, SyncHistoryStats } from './types'

export const getScamDetectionSettings = () =>
  request<{ mode: string; api_url: string; api_key: string; api_key_set: boolean; max_tokens: number }>('/api/settings/prefs/scam-detection')

export const setScamDetectionSettings = (mode: string, apiUrl: string, apiKey: string, maxTokens: number = 200) =>
  request<{ success: boolean; mode: string }>('/api/settings/prefs/scam-detection', {
    method: 'POST',
    body: JSON.stringify({ mode, api_url: apiUrl, api_key: apiKey, max_tokens: maxTokens }),
  })

export const getScamFlags = (includeDismissed = false) =>
  request<{ flags: ScamFlag[]; flagged_identity_ids: number[]; flagged_person_ids: number[]; flagged_conversation_ids: number[] }>(
    `/api/scam/flags${includeDismissed ? '?include_dismissed=true' : ''}`,
  )

/** Sender-scope verdict: 'scam' flags, 'not_scam' dismisses (+ marks unmarked flagged messages), null resets. */
export const setSenderVerdict = (identityId: number, verdict: ScamManualVerdict) =>
  request<ScamVerdictResponse>(`/api/scam/senders/${identityId}/verdict`, {
    method: 'PUT',
    body: JSON.stringify({ verdict }),
  })

export const getConversationScamStatus = (conversationId: number) =>
  request<{ has_scam_flags: boolean; flagged_message_count: number; flagged_message_ids: number[]; flagged_users: { identity_id: number; person_id: number | null; confidence: number | null; reason: string | null; source: string }[] }>(`/api/scam/status/${conversationId}`)

export const checkMessagesForScam = (conversationId: number, messageIds: number[]) =>
  request<{ status: 'queued'; message_count: number; queued_ids: number[]; already_running_ids: number[]; missing_ids: number[] }>('/api/scam/check-messages', {
    method: 'POST',
    body: JSON.stringify({ conversation_id: conversationId, message_ids: messageIds }),
  })

export interface ScannedMessageFilters {
  sender_identity_id?: number
  conversation_id?: number
  min_members?: number
  verdict_filter?: ScamVerdictFilter
}

function scannedFilterParams(filters?: ScannedMessageFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters?.sender_identity_id) params.set('sender_identity_id', String(filters.sender_identity_id))
  if (filters?.conversation_id) params.set('conversation_id', String(filters.conversation_id))
  if (filters?.min_members) params.set('min_members', String(filters.min_members))
  if (filters?.verdict_filter && filters.verdict_filter !== 'all') params.set('verdict_filter', filters.verdict_filter)
  return params
}

/** Keyset-paginated (newest first). Pass the previous page's next_cursor to load more. */
export const getScannedMessages = (
  opts: { cursor?: string | null; pageSize?: number; filters?: ScannedMessageFilters; signal?: AbortSignal } = {},
) => {
  const params = scannedFilterParams(opts.filters)
  params.set('page_size', String(opts.pageSize ?? 200))
  if (opts.cursor) params.set('cursor', opts.cursor)
  return request<{ messages: ScannedMessage[]; total: number; page: number; page_size: number; next_cursor: string | null }>(
    `/api/scam/scanned-messages?${params}`, { signal: opts.signal },
  )
}

/** Message-scope verdict: 'scam' | 'not_scam' | null (null clears the manual verdict). */
export const setMessageVerdict = (messages: { message_id: number; conversation_id?: number }[], verdict: ScamManualVerdict) =>
  request<ScamVerdictResponse>('/api/scam/scanned-messages/verdict', {
    method: 'PUT',
    body: JSON.stringify({ messages, verdict }),
  })

/** Exports every matching row (no cap). Resolves to how many rows were written and whether the file was saved. */
export const exportScannedMessages = async (filters?: ScannedMessageFilters) => {
  const params = scannedFilterParams(filters)

  // Backend writes to temp file and returns path
  const result = await request<{ temp_path: string; filename: string; count: number }>(
    `/api/scam/scanned-messages/export?${params}`,
  )

  // In pywebview: open native save dialog
  const pywebview = (window as any).pywebview
  if (pywebview?.api?.save_file_dialog) {
    const saved = await pywebview.api.save_file_dialog(result.temp_path, result.filename)
    return { saved: !!saved, count: result.count, path: saved }
  }

  // Dev mode (browser): download the temp file via a download endpoint
  const blob = await requestBlob(`/api/scam/scanned-messages/export/download?path=${encodeURIComponent(result.temp_path)}`)
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = result.filename
  a.click()
  setTimeout(() => URL.revokeObjectURL(a.href), 0)
  return { saved: true, count: result.count, path: null }
}

export const getScamPipeline = () =>
  request<{
    version: string
    updated_at: string
    categories: ScamCategory[]
    nodes: { id: string; type: string; config: Record<string, any>; short_circuit?: any }[]
    edges: { from: string; to: string }[]
    output: string
  }>('/api/scam/pipeline')

export const reloadScamPipeline = () =>
  request<{ status: string; version: string }>('/api/scam/pipeline/reload', { method: 'POST' })

export const getReportContext = (conversationId: number, targetMessageId: number) =>
  request<{ messages: ReportContextMessage[] }>(`/api/scam/reports/context?conversation_id=${conversationId}&target_message_id=${targetMessageId}`)

export const createScamReport = (data: {
  reported_identity_id: number
  reported_in_conversation_id: number
  reporter_verdict: string
  scam_category?: string
  custom_category?: string
  included_message_ids: number[]
}) =>
  request<{ report_id: string; status: string; created_at: string }>('/api/scam/reports', {
    method: 'POST',
    body: JSON.stringify(data),
  })

export const getScamReports = () =>
  request<{ reports: ScamReport[] }>('/api/scam/reports')

export const triggerMassScan = (maxMessages: number = 200, maxAgeHours: number = 48) =>
  request<{ status: string; messages_queued: number; queue?: ScamQueueStats }>(`/api/scam/mass-scan?max_messages=${maxMessages}&max_age_hours=${maxAgeHours}`, { method: 'POST' })

export const getShieldCloudSettings = () =>
  request<{ cloud_url: string; api_key_set: boolean; last_synced_at: string | null; sync_dismissals: boolean }>('/api/settings/prefs/shield-cloud')

export const setShieldCloudSettings = (cloudUrl: string, apiKey: string, syncDismissals?: boolean) =>
  request<{ success: boolean }>('/api/settings/prefs/shield-cloud', {
    method: 'POST',
    body: JSON.stringify({ cloud_url: cloudUrl, api_key: apiKey, sync_dismissals: syncDismissals }),
  })

export const getScanQueue = () => request<ScamQueueStats>('/api/scam/queue')

export const getSyncStatus = () =>
  request<{ configured: boolean; cloud_url: string; url_error: string | null; running: boolean; last_synced_at: string | null; pipeline_version: string | null }>('/api/scam/sync/status')

/** Concurrent calls join the single sync in flight on the server, so every caller gets the same result. */
export const triggerSync = () =>
  request<CloudSyncResult>('/api/scam/pipeline/sync', { method: 'POST' })

export const getSyncHistory = () =>
  request<import('./types').SyncHistoryStats>('/api/scam/sync/history')

export const submitFeedback = (data: {
  feedback_type: string
  message: string
  related_conversation_id?: number
  related_message_id?: number
  related_flag_identity_id?: number
}) =>
  request<{ id: number; status: string; sync_error: string | null; created_at: string }>('/api/scam/feedback', {
    method: 'POST',
    body: JSON.stringify(data),
  })

export const getFeedbackList = () =>
  request<{ feedback: import('./types').ScamFeedback[] }>('/api/scam/feedback')
