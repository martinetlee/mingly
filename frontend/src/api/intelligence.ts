/** AI chat history and mentions. */

import { request } from '../lib/http'
import type { AiConversationDetail, AiConversationSummary, MentionGroup } from './types'

export const listAiConversations = () =>
  request<AiConversationSummary[]>('/api/ai/conversations/')

export const createAiConversation = () =>
  request<AiConversationDetail>('/api/ai/conversations/', { method: 'POST' })

export const getAiConversation = (id: number, signal?: AbortSignal) =>
  request<AiConversationDetail>(`/api/ai/conversations/${id}`, { signal })

export const updateAiConversationTitle = (id: number, title: string) =>
  request<AiConversationDetail>(`/api/ai/conversations/${id}`, {
    method: 'PUT',
    body: JSON.stringify({ title }),
  })

export const deleteAiConversation = (id: number) =>
  request<{ success: boolean }>(`/api/ai/conversations/${id}`, { method: 'DELETE' })

// Note: POST /api/intelligence/chat persists both the user message and the
// assistant reply itself — never append turn messages from the client.

/** Compare-and-set on the server: applied=false when the conversation already had a title */
export const suggestAiConversationTitle = (id: number) =>
  request<{ title: string | null; applied?: boolean }>(`/api/ai/conversations/${id}/suggest-title`, { method: 'POST' })

export const getMentions = (params?: { days?: number; groupName?: string; tagName?: string; channel?: string }, signal?: AbortSignal) => {
  const qs = new URLSearchParams()
  if (params?.channel) qs.set('channel', params.channel)
  if (params?.days) qs.set('days', String(params.days))
  if (params?.groupName) qs.set('group_name', params.groupName)
  if (params?.tagName) qs.set('tag_name', params.tagName)
  const q = qs.toString()
  return request<MentionGroup[]>(`/api/intelligence/mentions${q ? `?${q}` : ''}`, { signal })
}
