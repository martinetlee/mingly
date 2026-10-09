/**
 * The paginated chat list for one filter key {sortBy, tagId, workspaceId,
 * channel}. Chats is the messengers space: email threads are never listed.
 * Latest-only paging, reloads and local patches: hooks/usePagedList.
 */
import { useCallback, useRef } from 'react'
import type { Conversation } from '../../api/types'
import { getConversations, MESSENGERS_ONLY } from '../../api/client'
import { errorMessage, reportError } from '../../lib/toast'
import { dedupeById, usePagedList } from '../../hooks/usePagedList'

export const CHAT_PAGE = 50
/** Upper bound for one reload request (server max is generous; keep it bounded) */
const MAX_RELOAD = 500

export interface ChatListKey {
  sortBy: 'recent' | 'unread' | 'alpha'
  tagId: number | null
  workspaceId: number | null
  /** One messenger channel; null/absent = all of them */
  channel?: string | null
}

export const dedupeChats = (chats: Conversation[]): Conversation[] => dedupeById(chats)

export function useChatList(listKey: ChatListKey) {
  const key = JSON.stringify(listKey)
  const paramsRef = useRef(listKey)
  paramsRef.current = listKey

  const fetchPage = useCallback((limit: number, offset: number, signal: AbortSignal) => {
    const p = paramsRef.current
    return getConversations(
      limit, offset, p.sortBy, p.tagId ?? undefined, p.workspaceId ?? undefined, signal,
      { ...MESSENGERS_ONLY, channel: p.channel ?? null },
    )
  }, [])

  const list = usePagedList<Conversation>(key, fetchPage, {
    pageSize: CHAT_PAGE,
    maxReload: MAX_RELOAD,
    // A failed first page is shown in the list (`error`, with Retry); later
    // failures keep the current rows and toast. request() already retried
    // transient failures.
    onError: (e, phase) => { if (phase !== 'load') reportError(e, phase === 'more' ? 'Couldn’t load more chats' : 'Couldn’t refresh chats') },
  })
  const { items, setItems, error, ...rest } = list
  return {
    ...rest,
    chats: items,
    setChats: setItems,
    /** The last load/reload failed (cleared by a successful page and on a key change); load-more failures only toast */
    error: error == null ? null : errorMessage(error),
  }
}
