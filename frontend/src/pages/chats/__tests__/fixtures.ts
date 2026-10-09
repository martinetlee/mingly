import type { Capability, Conversation, Message, MessageWindow, Person } from '../../../api/types'

/** Message `n` (id = seq = external id) in conversation 100 by default, one second apart by n. */
export function msg(n: number, over: Partial<Message> = {}): Message {
  return {
    id: n, conversation_id: 100, external_id: String(n), seq: n,
    sender: { identity_id: 1, person_id: 11, name: 'A', avatar_url: null },
    text: `m${n}`, text_html: null, subject: null,
    reactions: null, date: `2026-10-06T10:${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}Z`,
    is_outgoing: false, attachments: [], importance_score: null, user_flagged: false, ...over,
  }
}

export function page(ids: number[], over: Partial<MessageWindow> = {}, msgOver: Partial<Message> = {}): MessageWindow {
  const messages = ids.map((t) => msg(t, msgOver))
  return {
    messages, has_older: false, has_newer: false,
    anchor_found: true, syncing: false, ...over,
  }
}

/** Everything a Telegram conversation supports today */
export const ALL_CAPABILITIES: Capability[] = [
  'send', 'edit', 'delete', 'react', 'reply', 'media_send', 'pin', 'forward', 'read_receipts', 'forum_topics',
  'admin_moderation', 'history_backfill', 'typing', 'presence',
]

export const makeConversation = (id: number, title: string, over: Partial<Conversation> = {}): Conversation => ({
  id, account_id: 1, channel: 'telegram', kind: 'group', title, username: null, avatar_url: null, peer: null,
  is_monitored: false, is_hidden: false, is_briefing: false, is_tracked: false, is_forum: false, slow_mode_seconds: 0,
  is_pinned: false, is_archived: false, is_bot: false, is_deleted_account: false, history_complete: false,
  is_muted: false, muted_until: null, is_marked_unread: false, unread_count: 0,
  last_synced_at: '2026-07-01T00:00:00Z', last_activity_at: '2026-07-01T00:00:00Z',
  capabilities: ALL_CAPABILITIES, ...over,
})

/** A promise you resolve/reject from the test. */
export function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

/** A person with one Telegram identity (id = person id + 1000) */
export const makePerson = (id: number, name: string, over: Partial<Person> = {}): Person => ({
  id, name, display_name: null, avatar_url: null, is_contact: true, is_important: false, is_self: false,
  identities: [{
    id: id + 1000, person_id: id, channel: 'telegram', external_id: String(id + 5000), display_name: name,
    username: null, phone: null, avatar_url: null, is_contact: true,
  }],
  twitter_handle: null, linkedin_url: null, notes: null, relationship_status: null, relationship_status_manual: false,
  last_interaction_at: null, last_outgoing_at: null, last_incoming_at: null, positions: [], ...over,
})
