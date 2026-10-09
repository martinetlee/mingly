/** Display helpers for Conversation payloads (schema v2). */
import type { Conversation, ConversationKind, TelegramDirectoryHit, TelegramDirectoryResult } from '../api/types'

const KIND_FALLBACK: Record<ConversationKind, string> = {
  direct: 'Direct message',
  group: 'Group',
  channel: 'Channel',
  email_thread: 'Email thread',
}

/** The title to show (the server already falls back to @username) */
export function conversationTitle(c: Pick<Conversation, 'title' | 'kind'>): string {
  return c.title?.trim() || KIND_FALLBACK[c.kind] || 'Conversation'
}

/** Groups, channels and email threads have several participants; direct conversations don't */
export function isMultiParty(c: Pick<Conversation, 'kind'>): boolean {
  return c.kind !== 'direct'
}

const KIND_LABEL: Record<ConversationKind, string> = {
  direct: 'direct message',
  group: 'group',
  channel: 'channel',
  email_thread: 'email thread',
}

/** Lower-case kind label for sub-lines ("group", "email thread"…) */
export function kindLabel(kind: ConversationKind): string {
  return KIND_LABEL[kind] ?? kind
}

/** A Telegram directory search hit that isn't stored yet (open via ensureTelegramConversation) */
export function isDirectoryHit(r: TelegramDirectoryResult): r is TelegramDirectoryHit {
  return r.id == null
}
