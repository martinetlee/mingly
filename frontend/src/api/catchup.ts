/** Catch-up triage queue and summaries. */

import { request } from '../lib/http'
import type { CatchUpItem, CatchUpSummaryJob } from './types'

export const getCatchUpItems = () =>
  request<CatchUpItem[]>('/api/catchup/items')

export const snoozeCatchUpItem = (conversationId: number, hours = 24) =>
  request<CatchUpItem>(`/api/catchup/${conversationId}/snooze`, {
    method: 'POST',
    body: JSON.stringify({ hours }),
  })

export const markCatchUpHandled = (conversationId: number) =>
  request<CatchUpItem>(`/api/catchup/${conversationId}/handled`, {
    method: 'POST',
    body: JSON.stringify({}),
  })

export const reopenCatchUpItem = (conversationId: number) =>
  request<CatchUpItem>(`/api/catchup/${conversationId}/reopen`, {
    method: 'POST',
    body: JSON.stringify({}),
  })

/** Set the note. A blank note is a no-op on the server — use clearCatchUpNote to remove it. */
export const addCatchUpNote = (conversationId: number, note: string) =>
  request<CatchUpItem>(`/api/catchup/${conversationId}/note`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  })

export const clearCatchUpNote = (conversationId: number) =>
  request<CatchUpItem>(`/api/catchup/${conversationId}/note`, {
    method: 'POST',
    body: JSON.stringify({ clear: true }),
  })

export const updateCatchUpRelationship = (conversationId: number, status: string) =>
  request<{ person_id: number; relationship_status: string }>(`/api/catchup/${conversationId}/relationship-status`, {
    method: 'POST',
    body: JSON.stringify({ status }),
  })

/** Starts (or joins) the background summary job; returns its status immediately (202). */
export const triggerCatchUpSummaries = (conversationIds?: number[]) =>
  request<CatchUpSummaryJob>('/api/catchup/generate-summaries', {
    method: 'POST',
    body: JSON.stringify(conversationIds ? { conversation_ids: conversationIds } : {}),
  })

export const getCatchUpSummaryStatus = () =>
  request<CatchUpSummaryJob>('/api/catchup/generate-summaries/status')

export const setCatchUpSummaryInterval = (interval_minutes: number) =>
  request<{ success: boolean; interval_minutes: number }>('/api/settings/prefs/catchup-summary-interval', {
    method: 'POST',
    body: JSON.stringify({ interval_minutes }),
  })
