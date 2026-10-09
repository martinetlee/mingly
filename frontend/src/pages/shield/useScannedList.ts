import { useCallback, useRef } from 'react'
import { getScannedMessages } from '../../api/client'
import type { ScannedMessageFilters } from '../../api/client'
import type { ScamMsgState, ScannedMessage } from '../../api/types'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { useAsyncAction } from '../../hooks/useAsyncAction'
import { useCoalescedRefetch } from '../../hooks/useCoalescedRefetch'
import { useWsEvent } from '../../hooks/useWebSocket'
import { useShieldInvalidation } from './invalidate'
import { messageKey } from './shared'

export const SCANNED_PAGE_SIZE = 200
const MAX_PAGE_SIZE = 500

export interface ScannedPage {
  messages: ScannedMessage[]
  total: number
  nextCursor: string | null
}

/**
 * Fetch the newest `count` rows (at least one page) by walking keyset
 * cursors. Used for refetches so a reload keeps everything the user had
 * already paged in, without offset drift from rows inserted at the top.
 */
export async function fetchScannedRows(
  filters: ScannedMessageFilters,
  count: number,
  signal?: AbortSignal,
): Promise<ScannedPage> {
  const messages: ScannedMessage[] = []
  let cursor: string | null = null
  let total = 0
  do {
    const remaining = Math.max(count - messages.length, 1)
    const r = await getScannedMessages({ cursor, pageSize: Math.min(remaining, MAX_PAGE_SIZE), filters, signal })
    messages.push(...r.messages)
    total = r.total
    cursor = r.next_cursor
  } while (cursor && messages.length < count)
  return { messages, total, nextCursor: cursor }
}

/** Overlay WS / mutation row states onto loaded rows by message_id. */
export function applyRowStates(rows: ScannedMessage[], states: ScamMsgState[]): ScannedMessage[] {
  if (!states.length) return rows
  const byKey = new Map(states.map((s) => [messageKey(s), s]))
  let changed = false
  const next = rows.map((r) => {
    const s = byKey.get(messageKey(r))
    if (!s) return r
    changed = true
    return { ...r, ...s }
  })
  return changed ? next : rows
}

/**
 * The scanned-messages list for one filter set: keyset pagination ("Load
 * more"), refetch after every mutation (invalidateShieldData) and after WS scam
 * events (coalesced, with the same filters), and immediate row patches from
 * the stored states WS events / verdict responses carry.
 */
export function useScannedList(
  filters: ScannedMessageFilters,
  { enabled = true, pageSize = SCANNED_PAGE_SIZE }: { enabled?: boolean; pageSize?: number } = {},
) {
  const keyStr = JSON.stringify(filters)
  // How many rows the user has paged in for the current filters
  const loadedRef = useRef({ key: keyStr, count: pageSize })
  if (loadedRef.current.key !== keyStr) loadedRef.current = { key: keyStr, count: pageSize }

  const query = useLatestQuery(
    ['scam-scanned', filters],
    (signal) => fetchScannedRows(filters, loadedRef.current.count, signal),
    { enabled },
  )
  const { data, setData, refetch } = query

  const refresh = useCoalescedRefetch(refetch, 1000)
  useShieldInvalidation(refresh)
  useWsEvent('resync', refresh, enabled)

  const patchRows = useCallback((states: ScamMsgState[]) => {
    if (!states.length) return
    setData((prev) => (prev ? { ...prev, messages: applyRowStates(prev.messages, states) } : prev))
  }, [setData])

  // Row patches are instant; the coalesced refetch then fixes membership
  // (rows entering/leaving the active filter) and totals
  const onScamEvent = useCallback((e: { messages?: ScamMsgState[] }) => {
    patchRows(e.messages ?? [])
    refresh()
  }, [patchRows, refresh])
  useWsEvent('scam_detected', onScamEvent, enabled)
  useWsEvent('scam_check_safe', onScamEvent, enabled)
  useWsEvent('scam_check_error', onScamEvent, enabled)
  useWsEvent('scam_verdict_changed', onScamEvent, enabled)

  const loadMore = useAsyncAction(async () => {
    const cursor = data?.nextCursor
    if (!cursor) return
    const key = keyStr
    const r = await getScannedMessages({ cursor, pageSize, filters })
    if (loadedRef.current.key !== key) return
    setData((prev) => {
      if (!prev) return prev
      const seen = new Set(prev.messages.map(messageKey))
      const messages = [...prev.messages, ...r.messages.filter((m) => !seen.has(messageKey(m)))]
      loadedRef.current = { key, count: messages.length }
      return { messages, total: r.total, nextCursor: r.next_cursor }
    })
  }, { errorContext: "Couldn't load more messages" })

  return {
    rows: data?.messages ?? [],
    total: data?.total ?? 0,
    hasMore: !!data?.nextCursor,
    loading: query.loading,
    error: query.error,
    refetch,
    refresh,
    patchRows,
    loadMore,
  }
}

export type ScannedList = ReturnType<typeof useScannedList>
