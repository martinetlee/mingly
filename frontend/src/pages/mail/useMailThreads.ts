/**
 * The thread list of one Mail view {accountId, folder, label}: paginated and
 * latest-only (hooks/usePagedList), reloaded — as many rows as are loaded —
 * when mail arrives, a thread changes or deletes messages, and after a
 * reconnect.
 */
import { useCallback, useEffect, useRef } from 'react'
import { MAIL_PAGE, listMailThreads } from '../../api/email'
import type { MailFolder, MailThread } from '../../api/email'
import { reportError } from '../../lib/toast'
import { useWsEvent } from '../../hooks/useWebSocket'
import { usePagedList } from '../../hooks/usePagedList'

export interface MailView {
  accountId: number | null
  folder: MailFolder
  label: string | null
}

/** Coalescing window for WS-driven reloads */
export const MAIL_RELOAD_MS = 400

export function useMailThreads(view: MailView, enabled: boolean) {
  const key = JSON.stringify(view)
  const viewRef = useRef(view)
  viewRef.current = view

  const fetchPage = useCallback((limit: number, offset: number, signal: AbortSignal) => {
    const v = viewRef.current
    return listMailThreads({ accountId: v.accountId, folder: v.folder, label: v.label, limit, offset }, signal)
  }, [])

  const list = usePagedList<MailThread>(key, fetchPage, {
    pageSize: MAIL_PAGE,
    enabled,
    // The first page's failure is shown in the list (with Retry); later ones keep the rows
    onError: (e, phase) => { if (phase !== 'load') reportError(e, phase === 'more' ? 'Couldn’t load more mail' : 'Couldn’t refresh mail') },
  })
  const { reload } = list

  // New mail, a synced thread, deletions: refresh the visible rows (coalesced)
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null)
  const scheduleReload = useCallback(() => {
    if (pending.current) return
    pending.current = setTimeout(() => { pending.current = null; void reload() }, MAIL_RELOAD_MS)
  }, [reload])
  useEffect(() => () => { if (pending.current) clearTimeout(pending.current) }, [])
  const idsRef = useRef<Set<number>>(new Set())
  idsRef.current = new Set(list.items.map((t) => t.id))
  // Email messages carry participants (chat messages don't); a sync matters when it touches a listed thread
  useWsEvent('new_message', (e) => {
    if (idsRef.current.has(e.conversation_id) || (e.message?.participants?.length ?? 0) > 0) scheduleReload()
  }, enabled)
  useWsEvent('conversations_synced', (e) => {
    if (e.conversation_ids.some((id) => idsRef.current.has(id))) scheduleReload()
  }, enabled)
  useWsEvent('messages_deleted', (e) => {
    if (e.conversation_id == null || idsRef.current.has(e.conversation_id)) scheduleReload()
  }, enabled)
  // Events may have been missed while disconnected
  useWsEvent('resync', () => { void reload() }, enabled)

  /** Local patch after an action in the reading pane (null removes the row) */
  const patch = useCallback((id: number, fn: (t: MailThread) => MailThread | null) => {
    list.setItems((prev) => {
      if (!prev.some((t) => t.id === id)) return prev
      return prev.flatMap((t) => {
        if (t.id !== id) return [t]
        const next = fn(t)
        return next ? [next] : []
      })
    })
  }, [list.setItems]) // eslint-disable-line react-hooks/exhaustive-deps

  return {
    threads: list.items,
    loading: list.loading,
    loadingMore: list.loadingMore,
    hasMore: list.hasMore,
    error: list.error,
    loadMore: list.loadMore,
    reload,
    patch,
  }
}
