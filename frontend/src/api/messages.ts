/** Message windows, history backfill, reactions and pins. */

import { request } from '../lib/http'
import type { BackfillResult, Message, MessageWindow } from './types'

export interface MessageWindowParams {
  limit?: number
  /** Message id: the `limit` messages immediately older than it */
  before?: number | null
  /** Message id: the `limit` messages immediately newer than it */
  after?: number | null
  /** Message id to centre the window on */
  anchor?: number | null
  /** The channel's id of a message that may not be stored yet (e.g. a reply target): fetched from the channel */
  aroundExternalId?: string | null
  /** ISO datetime: centre on the first message at/after it */
  date?: string | null
  /** Forum topic scope (1 = General, includes untagged rows) */
  topicId?: number | null
}

/** Keyset message window (oldest → newest). Use at most one of before/after/anchor/aroundExternalId/date. */
export const getMessageWindow = (conversationId: number, p: MessageWindowParams = {}, signal?: AbortSignal) => {
  const params = new URLSearchParams()
  if (p.limit != null) params.set('limit', String(p.limit))
  if (p.before != null) params.set('before', String(p.before))
  if (p.after != null) params.set('after', String(p.after))
  if (p.anchor != null) params.set('anchor', String(p.anchor))
  if (p.aroundExternalId) params.set('around_external_id', p.aroundExternalId)
  if (p.date) params.set('around_date', p.date)
  if (p.topicId != null) params.set('topic_id', String(p.topicId))
  return request<MessageWindow>(`/api/conversations/${conversationId}/messages/window?${params}`, { signal })
}

/** Fetch history older than the oldest stored message from the channel. */
export const backfillConversation = (conversationId: number, opts: { limit?: number; topicId?: number | null } = {}, signal?: AbortSignal) => {
  const params = new URLSearchParams({ limit: String(opts.limit ?? 100) })
  if (opts.topicId != null) params.set('topic_id', String(opts.topicId))
  return request<BackfillResult>(`/api/conversations/${conversationId}/backfill?${params}`, { method: 'POST', signal })
}

export const searchConversationMessages = (conversationId: number, query: string, limit = 50, signal?: AbortSignal) =>
  request<Message[]>(`/api/conversations/${conversationId}/messages/search?q=${encodeURIComponent(query)}&limit=${limit}`, { signal })

export const getPinnedMessages = (conversationId: number, signal?: AbortSignal) =>
  request<Message[]>(`/api/conversations/${conversationId}/pinned`, { signal })

export const getPinnedMessage = async (conversationId: number, signal?: AbortSignal): Promise<Message | null> => {
  const msgs = await getPinnedMessages(conversationId, signal)
  return msgs && msgs.length > 0 ? msgs[0] : null
}

export const reactToMessage = (messageId: number, emoji: string | null) =>
  request<{ success: boolean; my_reaction: string | null }>(`/api/messages/${messageId}/react`, {
    method: 'POST',
    body: JSON.stringify({ emoji }),
  })

export const pinMessage = (messageId: number, pinned: boolean, notify = false) =>
  request<{ success: boolean; is_pinned: boolean }>(`/api/messages/${messageId}/pin`, {
    method: 'POST',
    body: JSON.stringify({ pinned, notify }),
  })
