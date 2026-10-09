/**
 * A person's cross-channel timeline (GET /api/people/{id}/timeline): their
 * one-to-one conversations, what they sent in groups, the emails they take
 * part in and the meetings they attended, newest first.
 */
import { request } from '../lib/http'
import type { CalendarEvent } from './calendar'
import type { ChannelName, ConversationKind, Message } from './types'

export interface TimelineConversation {
  id: number
  title: string | null
  kind: ConversationKind
  channel: ChannelName | null
}

export interface TimelineMessageItem {
  kind: 'message'
  message: Message
  /** null only if the conversation vanished mid-request */
  conversation: TimelineConversation | null
}

/** A calendar event the person attends (that has started) */
export interface TimelineMeetingItem {
  kind: 'meeting'
  event: CalendarEvent
}

export type TimelineItem = TimelineMessageItem | TimelineMeetingItem

export interface TimelinePage {
  items: TimelineItem[]
  has_more: boolean
}

/**
 * Pages are cut by messages: pass the oldest shown *message* as `before` for
 * the next page (a page carries the meetings between its oldest message and
 * `before`).
 */
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
