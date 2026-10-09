/**
 * An offset-paginated list bound to one key (filter/view), shared by the
 * Chats list and the Mail thread list.
 *
 * - Latest-only: a response applies only if its key is still current and no
 *   newer request (or local patch) superseded it — a slow page for view A can
 *   never render under view B, and a load-more of the previous view can't
 *   append to the new one.
 * - Reload refetches as many rows as are loaded (the pages the user scrolled
 *   through don't vanish); load-more continues from the rows actually held;
 *   merges dedupe by id.
 * - A local patch (an action's result, a WS delta) supersedes whatever is in
 *   flight — a read issued before the change can't undo it — and schedules
 *   a follow-up reload (or load-more) so nothing is lost.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { isAbortError } from '../lib/http'

export interface PagedListOptions {
  /** Rows per page (default 50) */
  pageSize?: number
  /** Upper bound for one reload request (default 500) */
  maxReload?: number
  /** false → nothing is fetched (default true) */
  enabled?: boolean
  /** A failed request: 'load' = the key's first page, 'reload', 'more' */
  onError?: (error: unknown, phase: 'load' | 'reload' | 'more') => void
}

export interface PagedListState<T> {
  key: string
  items: T[]
  hasMore: boolean
  /** First page for this key not loaded yet */
  loading: boolean
  loadingMore: boolean
  /** The last load/reload failed (cleared by the next success) */
  error: unknown
}

export interface PagedList<T> extends PagedListState<T> {
  /** Refetch every loaded row (WS-driven; coalesce the trigger) */
  reload: () => Promise<void>
  loadMore: () => Promise<void>
  /** Local change; supersedes requests in flight */
  setItems: (fn: (prev: T[]) => T[]) => void
}

/** Delay before the follow-up request of a patch (coalesces bursts of patches) */
export const FOLLOW_UP_MS = 250

export function dedupeById<T extends { id: number }>(items: T[]): T[] {
  const seen = new Set<number>()
  return items.filter((c) => (seen.has(c.id) ? false : (seen.add(c.id), true)))
}

export function usePagedList<T extends { id: number }>(
  key: string,
  fetchPage: (limit: number, offset: number, signal: AbortSignal) => Promise<T[]>,
  options: PagedListOptions = {},
): PagedList<T> {
  const { pageSize = 50, maxReload = 500, enabled = true } = options
  const [state, setState] = useState<PagedListState<T>>({
    key, items: [], hasMore: false, loading: true, loadingMore: false, error: null,
  })
  const stateRef = useRef(state)
  stateRef.current = state
  // Source of truth for the rows (state mirrors it), so patches and request
  // sizes always see the latest rows
  const itemsRef = useRef<T[]>([])
  const keyRef = useRef(key)
  const fetchRef = useRef(fetchPage)
  fetchRef.current = fetchPage
  const optsRef = useRef(options)
  optsRef.current = options
  /** Bumped by every replace (key change / reload) and every local patch */
  const genRef = useRef(0)
  const replaceCtl = useRef<AbortController | null>(null)
  const moreCtl = useRef<AbortController | null>(null)
  const loadedOnce = useRef(false)
  const followUpTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const followUp = useRef<'reload' | 'more' | null>(null)

  const commit = useCallback((forKey: string, patch: Partial<PagedListState<T>>) => {
    if (patch.items) itemsRef.current = patch.items
    setState((s) => (s.key !== forKey ? s : { ...s, ...patch }))
  }, [])

  const replace = useCallback(async (limit: number) => {
    const forKey = keyRef.current
    const gen = ++genRef.current
    replaceCtl.current?.abort()
    moreCtl.current?.abort() // a replace supersedes a load-more in flight
    moreCtl.current = null
    const ctl = new AbortController()
    replaceCtl.current = ctl
    const first = !loadedOnce.current
    setState((s) => (s.key === forKey && s.loadingMore ? { ...s, loadingMore: false } : s))
    try {
      const data = await fetchRef.current(limit, 0, ctl.signal)
      if (gen !== genRef.current || forKey !== keyRef.current) return
      loadedOnce.current = true
      commit(forKey, { items: dedupeById(data), hasMore: data.length === limit, loading: false, loadingMore: false, error: null })
    } catch (e) {
      if (isAbortError(e) || gen !== genRef.current || forKey !== keyRef.current) return
      // Keep the current rows
      commit(forKey, { loading: false, loadingMore: false, error: e })
      optsRef.current.onError?.(e, first ? 'load' : 'reload')
    } finally {
      if (replaceCtl.current === ctl) replaceCtl.current = null
    }
  }, [commit])

  const reloadLimit = useCallback(() => {
    const n = itemsRef.current.length
    return Math.min(maxReload, Math.max(pageSize, Math.ceil(n / pageSize) * pageSize))
  }, [maxReload, pageSize])

  // Key change (or enabling): start over with one page
  useEffect(() => {
    keyRef.current = key
    itemsRef.current = []
    loadedOnce.current = false
    if (followUpTimer.current) clearTimeout(followUpTimer.current)
    followUpTimer.current = null
    followUp.current = null
    setState({ key, items: [], hasMore: false, loading: true, loadingMore: false, error: null })
    if (!enabled) {
      genRef.current++
      replaceCtl.current?.abort()
      moreCtl.current?.abort()
      return
    }
    void replace(pageSize)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled])

  useEffect(() => () => {
    genRef.current++
    replaceCtl.current?.abort()
    moreCtl.current?.abort()
    if (followUpTimer.current) clearTimeout(followUpTimer.current)
  }, [])

  const reload = useCallback(() => {
    if (optsRef.current.enabled === false) return Promise.resolve()
    return replace(reloadLimit())
  }, [replace, reloadLimit])

  const loadMore = useCallback(async () => {
    const s = stateRef.current
    if (s.loading || s.loadingMore || !s.hasMore || replaceCtl.current) return
    const forKey = keyRef.current
    const gen = genRef.current
    const ctl = new AbortController()
    moreCtl.current = ctl
    setState((st) => (st.key === forKey ? { ...st, loadingMore: true } : st))
    try {
      const data = await fetchRef.current(pageSize, itemsRef.current.length, ctl.signal)
      if (gen !== genRef.current || forKey !== keyRef.current) return
      commit(forKey, { items: dedupeById([...itemsRef.current, ...data]), hasMore: data.length === pageSize, loadingMore: false })
    } catch (e) {
      if (isAbortError(e) || gen !== genRef.current || forKey !== keyRef.current) return
      commit(forKey, { loadingMore: false })
      optsRef.current.onError?.(e, 'more')
    } finally {
      if (moreCtl.current === ctl) moreCtl.current = null
    }
  }, [commit, pageSize])

  const setItems = useCallback((fn: (prev: T[]) => T[]) => {
    const prev = itemsRef.current
    const next = fn(prev)
    if (next === prev) return
    // Whatever is in flight was read before this change: drop it and redo it
    const pending = replaceCtl.current ? 'reload' : moreCtl.current ? 'more' : null
    if (pending) {
      genRef.current++
      replaceCtl.current?.abort()
      replaceCtl.current = null
      moreCtl.current?.abort()
      moreCtl.current = null
      if (followUp.current !== 'reload') followUp.current = pending
    }
    const forKey = keyRef.current
    commit(forKey, { items: next, ...(pending ? { loadingMore: false } : {}) })
    if (followUp.current) {
      if (followUpTimer.current) clearTimeout(followUpTimer.current)
      followUpTimer.current = setTimeout(() => {
        followUpTimer.current = null
        const what = followUp.current
        followUp.current = null
        if (forKey !== keyRef.current) return
        if (what === 'reload') void replace(reloadLimit())
        else if (what === 'more') void loadMore()
      }, FOLLOW_UP_MS)
    }
  }, [commit, replace, reloadLimit, loadMore])

  return { ...state, reload, loadMore, setItems }
}
