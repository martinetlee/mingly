import { useCallback, useEffect, useRef, useState } from 'react'
import { isAbortError } from '../lib/http'

export type QueryKey = string | number | boolean | null | undefined | readonly unknown[] | Record<string, unknown>

export interface LatestQueryOptions<T> {
  /** false → no fetch; data/error reset for the key (default true) */
  enabled?: boolean
  /** Extra values that should trigger a refetch, folded into the key */
  deps?: readonly unknown[]
  /**
   * Keep showing the previous key's data while the new key loads (default
   * false: `data` is undefined until data for the *current* key arrives).
   */
  keepPreviousData?: boolean
}

export interface LatestQueryResult<T> {
  /** Data for the current key (or the previous key's, with keepPreviousData) */
  data: T | undefined
  /** Last error for the current key */
  error: unknown
  /** True until the current key has settled (data or error) */
  loading: boolean
  /** True whenever a request is in flight (initial load or refetch) */
  fetching: boolean
  /** True when `data` belongs to a different key (keepPreviousData only) */
  isStale: boolean
  /** Re-run the fetcher for the current key; resolves to its data (undefined if superseded/failed) */
  refetch: () => Promise<T | undefined>
  /**
   * Local update (optimistic mutation). Discards any in-flight fetch so an
   * older read can't overwrite it — call refetch() after the mutation settles.
   */
  setData: (update: T | undefined | ((prev: T | undefined) => T | undefined)) => void
}

interface State<T> {
  data: T | undefined
  dataKey: string | null
  error: unknown
  errorKey: string | null
  settledKey: string | null
  fetching: boolean
}

export function serializeKey(key: QueryKey, deps?: readonly unknown[]): string {
  const base = typeof key === 'string' ? key : JSON.stringify(key ?? null)
  return deps && deps.length ? `${base}|${JSON.stringify(deps)}` : base
}

/**
 * Fetch bound to an identity. A result is applied only if its key is still
 * the current key and no newer request (or setData) superseded it; the
 * previous request is aborted via its AbortSignal on every key change,
 * refetch and unmount.
 *
 *   const { data: window, loading, refetch } =
 *     useLatestQuery(['messages', chatId, topicId], (signal) => getMessageWindow(chatId!, { topicId }, signal), { enabled: chatId != null })
 */
export function useLatestQuery<T>(
  key: QueryKey,
  fetcher: (signal: AbortSignal) => Promise<T>,
  options: LatestQueryOptions<T> = {},
): LatestQueryResult<T> {
  const { enabled = true, deps, keepPreviousData = false } = options
  const keyStr = serializeKey(key, deps)

  const [state, setState] = useState<State<T>>({
    data: undefined, dataKey: null, error: null, errorKey: null, settledKey: null, fetching: false,
  })
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher
  const currentKeyRef = useRef(keyStr)
  const enabledRef = useRef(enabled)
  const genRef = useRef(0)
  const abortRef = useRef<AbortController | null>(null)

  const run = useCallback((k: string): Promise<T | undefined> => {
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    const gen = ++genRef.current
    const isCurrent = () => gen === genRef.current && currentKeyRef.current === k && !ctrl.signal.aborted
    setState((s) => ({ ...s, fetching: true }))
    let p: Promise<T>
    try {
      p = fetcherRef.current(ctrl.signal)
    } catch (e) {
      p = Promise.reject(e)
    }
    return p.then(
      (result) => {
        if (!isCurrent()) return undefined
        abortRef.current = null
        setState({ data: result, dataKey: k, error: null, errorKey: null, settledKey: k, fetching: false })
        return result
      },
      (err) => {
        if (!isCurrent() || isAbortError(err)) return undefined
        abortRef.current = null
        setState((s) => ({ ...s, error: err, errorKey: k, settledKey: k, fetching: false }))
        return undefined
      },
    )
  }, [])

  useEffect(() => {
    currentKeyRef.current = keyStr
    enabledRef.current = enabled
    if (!enabled) {
      abortRef.current?.abort()
      abortRef.current = null
      genRef.current++
      setState((s) => (s.fetching ? { ...s, fetching: false } : s))
      return
    }
    run(keyStr)
  }, [keyStr, enabled, run])

  // Abort whatever is in flight on unmount
  useEffect(() => () => {
    genRef.current++
    abortRef.current?.abort()
  }, [])

  const refetch = useCallback(() => {
    if (!enabledRef.current) return Promise.resolve(undefined)
    return run(currentKeyRef.current)
  }, [run])

  const setData = useCallback((update: T | undefined | ((prev: T | undefined) => T | undefined)) => {
    // Supersede in-flight reads: they were issued before this local change
    genRef.current++
    abortRef.current?.abort()
    abortRef.current = null
    const k = currentKeyRef.current
    setState((s) => {
      const prev = s.dataKey === k ? s.data : undefined
      const next = typeof update === 'function' ? (update as (p: T | undefined) => T | undefined)(prev) : update
      return { data: next, dataKey: k, error: null, errorKey: null, settledKey: k, fetching: false }
    })
  }, [])

  const isCurrentData = state.dataKey === keyStr
  const showData = enabled && (isCurrentData || (keepPreviousData && state.dataKey !== null))
  return {
    data: showData ? state.data : undefined,
    error: enabled && state.errorKey === keyStr ? state.error : null,
    loading: enabled && state.settledKey !== keyStr,
    fetching: enabled && (state.fetching || state.settledKey !== keyStr),
    isStale: showData && !isCurrentData,
    refetch,
    setData,
  }
}
