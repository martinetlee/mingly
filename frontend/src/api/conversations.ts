/** Conversation lists, per-chat state and group/channel views. */

import { request } from '../lib/http'
import type { ChannelPostStats, ChatMember, Conversation, ConversationKind, ConversationSyncResult, ForumTopic, SharedMediaItem, UserPresence } from './types'

/** Narrowing of GET /api/conversations beyond tag / workspace */
export interface ConversationListFilter {
  /** One channel (`telegram`, `signal`…) */
  channel?: string | null
  /** Leave out one conversation kind (the Chats space drops `email_thread`) */
  excludeKind?: ConversationKind | null
}

/** Chats is the messengers space: every chat-only list leaves email threads to Mail */
export const MESSENGERS_ONLY: ConversationListFilter = { excludeKind: 'email_thread' }

export const getConversations = (
  limit = 50, offset = 0, sortBy = 'recent', tagId?: number, workspaceId?: number, signal?: AbortSignal,
  filter: ConversationListFilter = {},
) => {
  const q = new URLSearchParams({ limit: String(limit), offset: String(offset), sort_by: sortBy })
  if (tagId != null) q.set('tag_id', String(tagId))
  if (workspaceId != null) q.set('workspace_id', String(workspaceId))
  if (filter.channel) q.set('channel', filter.channel)
  if (filter.excludeKind) q.set('exclude_kind', filter.excludeKind)
  return request<Conversation[]>(`/api/conversations?${q}`, { signal })
}

export const getConversation = (conversationId: number, signal?: AbortSignal) =>
  request<Conversation>(`/api/conversations/${conversationId}`, { signal })

export const getConversationsByIds = (ids: number[]) =>
  request<Conversation[]>('/api/conversations/by-ids', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  })

export const searchConversationsLocal = (q: string) =>
  request<{ local: Conversation[] }>(`/api/conversations/search?q=${encodeURIComponent(q)}`)

export const bulkSync = () =>
  request<{ synced: number }>('/api/conversations/bulk-sync', { method: 'POST' })

export const syncConversation = (conversationId: number, limit?: number) =>
  request<ConversationSyncResult>(`/api/conversations/${conversationId}/sync${limit != null ? `?limit=${limit}` : ''}`, { method: 'POST' })

export const getConversationMembers = (conversationId: number, limit = 100) =>
  request<ChatMember[]>(`/api/conversations/${conversationId}/members?limit=${limit}`)

export const getSharedMedia = (conversationId: number, mediaFilter: 'media' | 'files' | 'links' = 'media', limit = 50, offset = 0, signal?: AbortSignal) =>
  request<SharedMediaItem[]>(`/api/conversations/${conversationId}/shared-media?media_filter=${mediaFilter}&limit=${limit}&offset=${offset}`, { signal })

export const markConversationRead = (conversationId: number) =>
  request<{ success: boolean }>(`/api/conversations/${conversationId}/mark-read`, { method: 'POST' })

export const getHiddenConversations = () =>
  request<Conversation[]>('/api/conversations/hidden')

export const getArchivedConversations = () =>
  request<Conversation[]>('/api/conversations/archived')

export const setPinned = (conversationId: number, pinned: boolean) =>
  request<{ conversation_id: number; is_pinned: boolean }>(`/api/conversations/${conversationId}/pin`, {
    method: 'POST',
    body: JSON.stringify({ pinned }),
  })

export const setArchived = (conversationId: number, archived: boolean) =>
  request<{ conversation_id: number; is_archived: boolean }>(`/api/conversations/${conversationId}/archive`, {
    method: 'POST',
    body: JSON.stringify({ archived }),
  })

export const setMuted = (conversationId: number, muted: boolean, duration?: string) =>
  request<{ conversation_id: number; is_muted: boolean; muted_until: string | null }>(`/api/conversations/${conversationId}/mute`, {
    method: 'POST',
    body: JSON.stringify({ muted, duration }),
  })

export const setMarkedUnread = (conversationId: number, marked_unread: boolean) =>
  request<{ conversation_id: number; is_marked_unread: boolean }>(`/api/conversations/${conversationId}/mark-unread`, {
    method: 'POST',
    body: JSON.stringify({ marked_unread }),
  })

export const setHidden = (conversationId: number, hidden: boolean) =>
  request<{ conversation_id: number; is_hidden: boolean }>(`/api/conversations/${conversationId}/hide`, {
    method: 'POST',
    body: JSON.stringify({ hidden }),
  })

export const getTrackedConversations = () =>
  request<Conversation[]>('/api/conversations/tracked')

export const setTracked = (conversationId: number, tracked: boolean) =>
  request<{ conversation_id: number; is_tracked: boolean }>(`/api/conversations/${conversationId}/track`, {
    method: 'POST',
    body: JSON.stringify({ tracked }),
  })

export const getBriefingConversations = () =>
  request<Conversation[]>('/api/conversations/briefing')

export const setBriefing = (conversationId: number, briefing: boolean) =>
  request<{ conversation_id: number; is_briefing: boolean }>(`/api/conversations/${conversationId}/briefing`, {
    method: 'POST',
    body: JSON.stringify({ briefing }),
  })

/** Presence of a direct conversation's peer (capability 'presence') */
export const getPeerPresence = (conversationId: number, signal?: AbortSignal) =>
  request<UserPresence>(`/api/conversations/${conversationId}/presence`, { signal })

export const sendTypingAction = (conversationId: number) =>
  request<{ success: boolean }>(`/api/conversations/${conversationId}/typing`, { method: 'POST' })

export const getForumTopics = (conversationId: number) =>
  request<ForumTopic[]>(`/api/conversations/${conversationId}/topics`)

export const createForumTopic = (conversationId: number, title: string) =>
  request<{ success: boolean; topic_id: number | null }>(`/api/conversations/${conversationId}/topics`, {
    method: 'POST',
    body: JSON.stringify({ title }),
  })

export const setTopicState = (conversationId: number, topic_id: number, closed: boolean) =>
  request<{ success: boolean; topic_id: number; closed: boolean }>(`/api/conversations/${conversationId}/topics/${topic_id}/state`, {
    method: 'POST',
    body: JSON.stringify({ closed }),
  })

export const setSlowMode = (conversationId: number, seconds: number) =>
  request<{ success: boolean; slow_mode_seconds: number }>(`/api/conversations/${conversationId}/slow-mode`, {
    method: 'POST',
    body: JSON.stringify({ seconds }),
  })

export const getPostStats = (conversationId: number, limit = 20) =>
  request<ChannelPostStats[]>(`/api/conversations/${conversationId}/post-stats?limit=${limit}`)
