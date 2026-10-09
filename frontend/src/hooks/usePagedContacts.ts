import { useCallback, useEffect, useRef, useState } from 'react'
import type { Person } from '../api/types'
import { getPeoplePage, isAbortError } from '../api/client'
import { serializeKey } from './useLatestQuery'

interface PageState {
  key: string
  items: Person[]
  total: number
  statusCounts: Record<string, number>
  importantCount: number | null
  loading: boolean
  loadingMore: boolean
  error: unknown
}

const initial = (key: string): PageState => ({
  key, items: [], total: 0, statusCounts: {}, importantCount: null, loading: true, loadingMore: false, error: null,
})

/**
 * Contacts (people in any channel's address book) filtered and paged on the server. Results are bound to
 * (search, status): a page for an older filter is dropped, and `loadMore`
 * only appends pages fetched for the filter that's still current.
 */
export function usePagedContacts(search: string, status: string, pageSize = 100, important = false) {
  const key = serializeKey(['contacts', search, status, important])
  const [state, setState] = useState<PageState>(() => initial(key))
  const keyRef = useRef(key)
  const stateRef = useRef(state)
  stateRef.current = state
  const ctrlRef = useRef<AbortController | null>(null)

  const loadFirst = useCallback(async (k: string, keepItems: boolean) => {
    ctrlRef.current?.abort()
    const ctrl = new AbortController()
    ctrlRef.current = ctrl
    if (!keepItems) setState(initial(k))
    try {
      const page = await getPeoplePage({ search, status, important, contactsOnly: true, limit: pageSize, offset: 0 }, ctrl.signal)
      if (keyRef.current !== k || ctrl.signal.aborted) return
      setState({
        key: k, items: page.items, total: page.total, statusCounts: page.status_counts,
        importantCount: page.important_count ?? null,
        loading: false, loadingMore: false, error: null,
      })
    } catch (e) {
      if (keyRef.current !== k || isAbortError(e)) return
      setState((s) => ({ ...s, key: k, loading: false, error: e }))
    }
  }, [search, status, important, pageSize])

  useEffect(() => {
    keyRef.current = key
    loadFirst(key, false)
    return () => { ctrlRef.current?.abort() }
  }, [key, loadFirst])

  const hasMore = state.key === key && !state.loading && state.items.length < state.total

  const loadMore = useCallback(async () => {
    const k = keyRef.current
    const s = stateRef.current
    if (s.key !== k || s.loading || s.loadingMore || s.items.length >= s.total) return
    setState((prev) => ({ ...prev, loadingMore: true }))
    try {
      const page = await getPeoplePage({ search, status, important, contactsOnly: true, limit: pageSize, offset: s.items.length })
      if (keyRef.current !== k) return
      setState((prev) => {
        const seen = new Set(prev.items.map((c) => c.id))
        return {
          ...prev,
          items: [...prev.items, ...page.items.filter((c) => !seen.has(c.id))],
          total: page.total,
          statusCounts: page.status_counts,
          loadingMore: false,
        }
      })
    } catch (e) {
      if (keyRef.current !== k) return
      setState((prev) => ({ ...prev, loadingMore: false, error: e }))
    }
  }, [search, status, important, pageSize])

  /** Re-read the first page (after a sync); keeps the list on screen meanwhile. */
  const reload = useCallback(() => loadFirst(keyRef.current, true), [loadFirst])

  /** Replace one person in place (after an edit). */
  const updateItem = useCallback((updated: Person) => {
    setState((prev) => ({ ...prev, items: prev.items.map((c) => (c.id === updated.id ? updated : c)) }))
  }, [])

  const current = state.key === key ? state : initial(key)
  return {
    items: current.items,
    total: current.total,
    statusCounts: current.statusCounts,
    importantCount: current.importantCount,
    loading: current.loading,
    loadingMore: current.loadingMore,
    error: current.error,
    hasMore,
    loadMore,
    reload,
    updateItem,
  }
}
