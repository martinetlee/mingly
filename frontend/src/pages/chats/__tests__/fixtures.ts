import type { Chat, Message, MessageWindow } from '../../../api/types'
import { cursorOf } from '../messageWindow'

/** Message `tg` in chat `chat_id` (default -100), one second apart by tg id. */
export function msg(tg: number, over: Partial<Message> = {}): Message {
  return {
    id: tg, tg_message_id: tg, chat_id: -100, sender_id: 1, sender_name: 'A', text: `m${tg}`, text_html: null,
    reactions: null, date: `2026-10-06T10:${String(Math.floor(tg / 60)).padStart(2, '0')}:${String(tg % 60).padStart(2, '0')}Z`,
    is_outgoing: false, media_type: null, media_url: null, importance_score: null, user_flagged: false, ...over,
  }
}

export function page(tgs: number[], over: Partial<MessageWindow> = {}, msgOver: Partial<Message> = {}): MessageWindow {
  const messages = tgs.map((t) => msg(t, msgOver))
  return {
    messages, has_older: false, has_newer: false,
    older_cursor: messages[0] ? cursorOf(messages[0]) : null,
    newer_cursor: messages.length ? cursorOf(messages[messages.length - 1]) : null,
    anchor_found: true, syncing: false, ...over,
  }
}

export const makeChat = (telegram_id: number, name: string, over: Partial<Chat> = {}): Chat => ({
  id: telegram_id, telegram_id, name, type: 'group', username: null,
  is_monitored: false, is_hidden: false, is_briefing: false, is_tracked: false, is_pinned: false,
  is_archived: false, is_muted: false, muted_until: null, is_marked_unread: false, unread_count: 0,
  last_synced_at: '2026-07-01T00:00:00Z', last_activity_at: '2026-07-01T00:00:00Z', last_message: null, ...over,
})

/** A promise you resolve/reject from the test. */
export function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
