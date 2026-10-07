/**
 * The paginated chat list for one filter key {sortBy, tagId, workspaceId}.
 *
 * - Latest-only: a response is applied only if its key is still current and
 *   no newer replace (key change / reload) started since — a slow page for
 *   workspace A can never render under workspace B, and a load-more from the
 *   previous filter can't append to the new one.
 * - Offset consistency: reload refetches as many rows as are loaded (so the
 *   pages the user scrolled through don't vanish), and load-more continues
 *   from the number of rows actually held; merges dedupe by telegram_id.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Chat } from '../../api/types'
import { getChats, isAbortError } from '../../api/client'
import { reportError } from '../../lib/toast'

export const CHAT_PAGE = 50
/** Upper bound for one reload request (server max is generous; keep it bounded) */
const MAX_RELOAD = 500

export interface ChatListKey {
  sortBy: 'recent' | 'unread' | 'alpha'
  tagId: number | null
  workspaceId: number | null
}

export interface ChatListState {
  key: string
  chats: Chat[]
  hasMore: boolean
  /** First page for this key not loaded yet */
  loading: boolean
  loadingMore: boolean
}

export function dedupeChats(chats: Chat[]): Chat[] {
  const seen = new Set<number>()
  return chats.filter((c) => (seen.has(c.telegram_id) ? false : (seen.add(c.telegram_id), true)))
}

export function useChatList(listKey: ChatListKey) {
  const key = JSON.stringify(listKey)
  const [state, setState] = useState<ChatListState>({ key, chats: [], hasMore: false, loading: true, loadingMore: false })
  const stateRef = useRef(state)
  stateRef.current = state
  const keyRef = useRef(key)
  const paramsRef = useRef(listKey)
  paramsRef.current = listKey
  /** Bumped by every replace (key change or reload) */
  const genRef = useRef(0)
  const ctlRef = useRef<AbortController | null>(null)

  const fetchPage = useCallback((limit: number, offset: number, signal?: AbortSignal) => {
    const p = paramsRef.current
    return getChats(limit, offset, p.sortBy, p.tagId ?? undefined, p.workspaceId ?? undefined, signal)
  }, [])

  const replace = useCallback(async (limit: number) => {
    const forKey = keyRef.current
    const gen = ++genRef.current
    ctlRef.current?.abort()
    const ctl = new AbortController()
    ctlRef.current = ctl
    try {
      const data = await fetchPage(limit, 0, ctl.signal)
      if (gen !== genRef.current || forKey !== keyRef.current) return
      setState((s) => (s.key !== forKey ? s : {
        ...s, chats: dedupeChats(data), hasMore: data.length === limit, loading: false, loadingMore: false,
      }))
    } catch (e) {
      if (isAbortError(e) || gen !== genRef.current) return
      // Keep the current list; request() already retried transient failures
      setState((s) => (s.key !== forKey ? s : { ...s, loading: false }))
      reportError(e, 'Couldn’t load chats')
    }
  }, [fetchPage])

  // Key change: start over with one page
  useEffect(() => {
    keyRef.current = key
    setState({ key, chats: [], hasMore: false, loading: true, loadingMore: false })
    void replace(CHAT_PAGE)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  useEffect(() => () => ctlRef.current?.abort(), [])

  /** Refetch everything currently loaded (WS-driven; coalesce the trigger) */
  const reload = useCallback(() => {
    const loaded = stateRef.current.chats.length
    const limit = Math.min(MAX_RELOAD, Math.max(CHAT_PAGE, Math.ceil(loaded / CHAT_PAGE) * CHAT_PAGE))
    return replace(limit)
  }, [replace])

  const loadMore = useCallback(async () => {
    const s = stateRef.current
    if (s.loading || s.loadingMore || !s.hasMore) return
    const forKey = keyRef.current
    const gen = genRef.current
    setState((st) => (st.key === forKey ? { ...st, loadingMore: true } : st))
    try {
      const data = await fetchPage(CHAT_PAGE, s.chats.length)
      if (gen !== genRef.current || forKey !== keyRef.current) return
      setState((st) => (st.key !== forKey ? st : {
        ...st, chats: dedupeChats([...st.chats, ...data]), hasMore: data.length === CHAT_PAGE, loadingMore: false,
      }))
    } catch (e) {
      if (gen !== genRef.current) return
      setState((st) => (st.key === forKey ? { ...st, loadingMore: false } : st))
      reportError(e, 'Couldn’t load more chats')
    }
  }, [fetchPage])

  /** Local delta (WS new_message, optimistic flag changes) */
  const setChats = useCallback((fn: (prev: Chat[]) => Chat[]) => {
    setState((s) => {
      const chats = fn(s.chats)
      return chats === s.chats ? s : { ...s, chats }
    })
  }, [])

  return { ...state, reload, loadMore, setChats }
}
