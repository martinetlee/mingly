/** DM archive. */

import { request } from '../lib/http'
import type { ArchiveStatus, ChatArchiveResponse } from './types'

export const getArchiveStatus = (signal?: AbortSignal) =>
  request<import('./types').ArchiveStatus>('/api/archive/status', { signal })

export const startArchive = () =>
  request<import('./types').ArchiveStatus>('/api/archive/start', { method: 'POST' })

export const pauseArchive = () =>
  request<import('./types').ArchiveStatus>('/api/archive/pause', { method: 'POST' })

/** Queue a full-history walk of one conversation (DM, group or channel) */
export const archiveConversation = (conversationId: number) =>
  request<import('./types').ChatArchiveResponse>(`/api/conversations/${conversationId}/archive-history`, { method: 'POST' })
