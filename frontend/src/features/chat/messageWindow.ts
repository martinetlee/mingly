/**
 * Pure helpers for the keyset message window (see useChatMessages).
 *
 * A window is a contiguous, ordered slice of one conversation's (or one
 * forum topic's) history: oldest → newest by (date, seq, id), with flags for
 * whether more exists on either side. Every merge dedupes by message id
 * (internal ids are globally unique), and a window never mixes conversations.
 */
import type { Message, MessageWindow } from '../../api/types'
import { parseServerDate } from '../../utils/time'

export interface WindowSlice {
  messages: Message[]
  hasOlder: boolean
  hasNewer: boolean
}

/** Upper bound on messages held in one window; the far end is trimmed past it. */
export const MAX_WINDOW = 500

/** The window API pages from a message id (`before=` / `after=`). */
export function cursorOf(m: Message): number {
  return m.id
}

function ts(m: Message): number {
  return parseServerDate(m.date)?.getTime() ?? 0
}

/** Window order: (date, seq, id) ascending — seq is the channel-native order within a conversation. */
export function compareMessages(a: Message, b: Message): number {
  const d = ts(a) - ts(b)
  if (d !== 0) return d
  const s = a.seq - b.seq
  return s !== 0 ? s : a.id - b.id
}

/** Union by message id (incoming wins), sorted. */
export function mergeMessages(base: Message[], incoming: Message[]): Message[] {
  if (incoming.length === 0) return base
  const byId = new Map<number, Message>()
  for (const m of base) byId.set(m.id, m)
  for (const m of incoming) byId.set(m.id, m)
  return [...byId.values()].sort(compareMessages)
}

export function fromPage(page: MessageWindow): WindowSlice {
  return {
    messages: mergeMessages([], page.messages),
    hasOlder: page.has_older,
    hasNewer: page.has_newer,
  }
}

/** Merge a `before=` page; trims the newest end past MAX_WINDOW. */
export function prependOlder(w: WindowSlice, page: MessageWindow, max = MAX_WINDOW): WindowSlice {
  let messages = mergeMessages(w.messages, page.messages)
  let hasNewer = w.hasNewer
  if (messages.length > max) {
    messages = messages.slice(0, max)
    hasNewer = true
  }
  return { messages, hasOlder: page.has_older, hasNewer }
}

/** Merge an `after=` page; trims the oldest end past MAX_WINDOW. */
export function appendNewer(w: WindowSlice, page: MessageWindow, max = MAX_WINDOW): WindowSlice {
  let messages = mergeMessages(w.messages, page.messages)
  let hasOlder = w.hasOlder
  if (messages.length > max) {
    messages = messages.slice(messages.length - max)
    hasOlder = true
  }
  return { messages, hasOlder, hasNewer: page.has_newer }
}

/**
 * Merge a fresh "latest" page into the window after a sync/reconnect.
 * Contiguous (or overlapping) → union and the window is at latest again.
 * A gap (more new messages than one page) → keep the window untouched and
 * mark it as not-at-latest so "Jump to latest" appears instead of yanking
 * the user's reading position.
 */
export function mergeLatest(w: WindowSlice, page: MessageWindow): WindowSlice {
  if (page.messages.length === 0) return w
  if (w.messages.length === 0) return fromPage(page)
  const newestLocal = w.messages[w.messages.length - 1]
  const contiguous = !page.has_older || compareMessages(page.messages[0], newestLocal) <= 0
  if (!contiguous) return { ...w, hasNewer: true }
  return { messages: mergeMessages(w.messages, page.messages), hasOlder: w.hasOlder, hasNewer: false }
}

/**
 * A live copy of a message (WS new/edited event, send/edit response) is
 * serialized without its reply preview, and some sources carry no
 * participants: keep what the stored copy already knows, as long as it still
 * replies to the same message.
 */
export function withKnownContext(prev: Message, next: Message): Message {
  let out = next
  if ((next.reply_to_external_id ?? null) === (prev.reply_to_external_id ?? null)) {
    if (next.reply_to_preview == null && prev.reply_to_preview != null) out = { ...out, reply_to_preview: prev.reply_to_preview }
    if (next.reply_to_message_id == null && prev.reply_to_message_id != null) out = { ...out, reply_to_message_id: prev.reply_to_message_id }
  }
  if (!next.participants?.length && prev.participants?.length) out = { ...out, participants: prev.participants }
  return out
}

/**
 * A live message (WS / own send). Replaces an existing copy (keeping its
 * known context, see withKnownContext); otherwise it is appended only when
 * the window is at latest — appending under an older window would fake a
 * contiguous history.
 */
export function applyLive(w: WindowSlice, msg: Message): WindowSlice {
  const idx = w.messages.findIndex((m) => m.id === msg.id)
  if (idx !== -1) {
    const messages = [...w.messages]
    messages[idx] = withKnownContext(messages[idx], msg)
    return { ...w, messages }
  }
  if (w.hasNewer) return w
  return { ...w, messages: mergeMessages(w.messages, [msg]) }
}

export interface ViewScope {
  conversationId: number
  /** null = whole conversation; 1 = General (includes legacy rows with no topic) */
  topicId: number | null
}

/** Does `msg` belong in a window scoped to `scope`? */
export function inScope(msg: Pick<Message, 'conversation_id' | 'topic_id'>, scope: ViewScope): boolean {
  if (msg.conversation_id !== scope.conversationId) return false
  if (scope.topicId == null) return true
  const t = msg.topic_id ?? null
  if (scope.topicId === 1) return t == null || t === 1
  return t === scope.topicId
}

/**
 * A `messages_deleted` event applies to this conversation when it names it.
 * Internal message ids are globally unique, so a conversation-less deletion
 * can safely be applied by id anywhere.
 */
export function deletionApplies(eventConversationId: number | null, conversationId: number): boolean {
  return eventConversationId == null || eventConversationId === conversationId
}

/** First unread message given the conversation's unread count at selection time. */
export function firstUnreadId(messages: Message[], unreadCount: number): number | null {
  if (unreadCount <= 0 || messages.length === 0) return null
  // Only incoming messages count as unread
  let remaining = unreadCount
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].is_outgoing) continue
    remaining--
    if (remaining === 0) return messages[i].id
  }
  // More unread than loaded: the separator sits above the oldest loaded incoming message
  const firstIncoming = messages.find((m) => !m.is_outgoing)
  return firstIncoming?.id ?? null
}
