/**
 * Pure helpers for the keyset message window (see useChatMessages).
 *
 * A window is a contiguous, ordered slice of one chat's (or one forum
 * topic's) history: oldest → newest by (date, tg_message_id), with flags for
 * whether more exists on either side. Every merge dedupes by tg_message_id —
 * tg ids are unique within a chat, and a window never mixes chats.
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

/** Cursor format of the window API: "<date as returned>~<tg_message_id>". */
export function cursorOf(m: Message): string {
  return `${m.date}~${m.tg_message_id}`
}

function ts(m: Message): number {
  return parseServerDate(m.date)?.getTime() ?? 0
}

/** Window order: (date, tg_message_id) ascending. */
export function compareMessages(a: Message, b: Message): number {
  const d = ts(a) - ts(b)
  return d !== 0 ? d : a.tg_message_id - b.tg_message_id
}

/** Union by tg_message_id (incoming wins), sorted. */
export function mergeMessages(base: Message[], incoming: Message[]): Message[] {
  if (incoming.length === 0) return base
  const byId = new Map<number, Message>()
  for (const m of base) byId.set(m.tg_message_id, m)
  for (const m of incoming) byId.set(m.tg_message_id, m)
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
 * A live message (WS / own send). Replaces an existing copy; otherwise it is
 * appended only when the window is at latest — appending under an older
 * window would fake a contiguous history.
 */
export function applyLive(w: WindowSlice, msg: Message): WindowSlice {
  const idx = w.messages.findIndex((m) => m.tg_message_id === msg.tg_message_id)
  if (idx !== -1) {
    const messages = [...w.messages]
    messages[idx] = msg
    return { ...w, messages }
  }
  if (w.hasNewer) return w
  return { ...w, messages: mergeMessages(w.messages, [msg]) }
}

export interface ViewScope {
  chatId: number
  /** null = whole chat; 1 = General (includes legacy rows with no topic) */
  topicId: number | null
}

/** Does `msg` belong in a window scoped to `scope`? */
export function inScope(msg: Pick<Message, 'chat_id' | 'topic_id'>, scope: ViewScope): boolean {
  if (msg.chat_id !== scope.chatId) return false
  if (scope.topicId == null) return true
  const t = msg.topic_id ?? null
  if (scope.topicId === 1) return t == null || t === 1
  return t === scope.topicId
}

/**
 * Message ids in a `messages_deleted` event apply to this chat when the event
 * names it — or, for chat-less deletions, only in the shared private/basic
 * group id namespace (channels/supergroups have per-channel ids).
 */
export function deletionApplies(eventChatId: number | null, chatId: number): boolean {
  if (eventChatId != null) return eventChatId === chatId
  return chatId > -1000000000000
}

/** First unread message given the chat's unread count at selection time. */
export function firstUnreadId(messages: Message[], unreadCount: number): number | null {
  if (unreadCount <= 0 || messages.length === 0) return null
  // Only incoming messages count as unread
  let remaining = unreadCount
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].is_outgoing) continue
    remaining--
    if (remaining === 0) return messages[i].tg_message_id
  }
  // More unread than loaded: the separator sits above the oldest loaded incoming message
  const firstIncoming = messages.find((m) => !m.is_outgoing)
  return firstIncoming?.tg_message_id ?? null
}
