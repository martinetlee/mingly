/** Local and global search. */

import { request } from '../lib/http'
import type { ConversationKind, GlobalSearchResults } from './types'

/** Telegram global message search: the conversation is stored server-side, its messages may not be yet (open by external_id) */
export interface MessageSearchResult {
  conversation_id: number
  conversation_title: string
  conversation_kind: ConversationKind
  messages: { external_id: string; text: string; sender_name: string | null; date: string | null }[]
}

export const searchMessagesGlobal = (q: string, signal?: AbortSignal) =>
  request<MessageSearchResult[]>(`/api/conversations/search/messages?q=${encodeURIComponent(q)}`, { signal })

export const globalSearch = (query: string, limit = 20, signal?: AbortSignal) =>
  request<GlobalSearchResults>(`/api/search?q=${encodeURIComponent(query)}&limit=${limit}`, { signal })
