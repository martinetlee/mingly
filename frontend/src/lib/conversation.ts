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

/** Lower-case kind label for badges and sub-lines ("group", "email thread"…) — the one wording everywhere */
export function kindLabel(kind: ConversationKind | string): string {
  return KIND_LABEL[kind as ConversationKind] ?? kind.replace(/_/g, ' ')
}

/** Badge colours per conversation kind (pair with kindLabel) */
export const KIND_COLOR: Record<ConversationKind, string> = {
  direct: 'bg-indigo-500/20 text-indigo-400',
  group: 'bg-purple-500/20 text-purple-400',
  channel: 'bg-amber-500/20 text-amber-400',
  email_thread: 'bg-sky-500/20 text-sky-400',
}

/** The badge classes for a kind (unknown kinds get a neutral badge) */
export function kindColor(kind: ConversationKind | string): string {
  return KIND_COLOR[kind as ConversationKind] ?? 'bg-slate-600 text-slate-400'
}

/** A Telegram directory search hit that isn't stored yet (open via ensureTelegramConversation) */
export function isDirectoryHit(r: TelegramDirectoryResult): r is TelegramDirectoryHit {
  return r.id == null
}
