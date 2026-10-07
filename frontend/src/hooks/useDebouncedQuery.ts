import { useEffect, useState } from 'react'
import { useLatestQuery } from './useLatestQuery'
import type { LatestQueryResult } from './useLatestQuery'

/** `value`, updated only after it has been stable for `delayMs`. */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    if (Object.is(value, debounced)) return
    const t = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(t)
  }, [value, delayMs, debounced])
  return debounced
}

export interface DebouncedQueryOptions {
  /** Debounce in ms (default 250) */
  delayMs?: number
  /** Queries shorter than this (after trim) don't fetch (default 1) */
  minLength?: number
  enabled?: boolean
  /** Extra scope that should refetch immediately (e.g. chat id for in-chat search) */
  deps?: readonly unknown[]
  /** Keep the previous results visible while the next query loads (default true) */
  keepPreviousData?: boolean
}

export interface DebouncedQueryResult<T> extends LatestQueryResult<T> {
  /** The (trimmed) query the current data/loading refer to */
  debouncedQuery: string
  /** True while typing (query not yet debounced) or fetching */
  pending: boolean
}

/**
 * Typeahead search: debounce + latest-only + abort. Results always belong to
 * `debouncedQuery`; an older response can never overwrite a newer one.
 *
 *   const { data: hits, pending } = useDebouncedQuery(text, (q, signal) => searchContacts(q, signal))
 */
export function useDebouncedQuery<T>(
  query: string,
  fetcher: (query: string, signal: AbortSignal) => Promise<T>,
  options: DebouncedQueryOptions = {},
): DebouncedQueryResult<T> {
  const { delayMs = 250, minLength = 1, enabled = true, deps, keepPreviousData = true } = options
  const trimmed = query.trim()
  const debouncedQuery = useDebouncedValue(trimmed, delayMs)
  // Clearing the box hides results immediately; only growing queries wait
  const active = enabled && trimmed.length >= minLength && debouncedQuery.length >= minLength
  const result = useLatestQuery<T>(
    ['q', debouncedQuery],
    (signal) => fetcher(debouncedQuery, signal),
    { enabled: active, deps, keepPreviousData },
  )
  const typing = trimmed !== debouncedQuery && enabled && trimmed.length >= minLength
  return {
    ...result,
    debouncedQuery,
    pending: typing || (active && result.fetching),
  }
}
