/**
 * A person's cross-channel timeline (GET /api/people/{id}/timeline): their
 * one-to-one conversations, what they sent in groups and the emails they take
 * part in, newest first.
 */
import { request } from '../lib/http'
import type { ChannelName, ConversationKind, Message } from './types'

export interface TimelineConversation {
  id: number
  title: string | null
  kind: ConversationKind
  channel: ChannelName | null
}

export interface TimelineItem {
  message: Message
  /** null only if the conversation vanished mid-request */
  conversation: TimelineConversation | null
}

export interface TimelinePage {
  items: TimelineItem[]
  has_more: boolean
}

/** Pass the oldest shown item's message as `before` for the next page */
export function getPersonTimeline(
  personId: number,
  opts: { limit?: number; before?: Pick<Message, 'id' | 'date'> | null; signal?: AbortSignal } = {},
) {
  const q = new URLSearchParams({ limit: String(opts.limit ?? 50) })
  if (opts.before) {
    q.set('before', opts.before.date)
    q.set('before_id', String(opts.before.id))
  }
  return request<TimelinePage>(`/api/people/${personId}/timeline?${q}`, { signal: opts.signal })
}
