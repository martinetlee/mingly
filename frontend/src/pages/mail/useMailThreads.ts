/**
 * The thread list of one Mail view {accountId, folder, label}: paginated,
 * latest-only (a slow page of a previous view never lands in the current
 * one), reloaded — as many rows as are loaded — when mail arrives or a
 * thread changes.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { MAIL_PAGE, listMailThreads } from '../../api/email'
import type { MailFolder, MailThread } from '../../api/email'
import { isAbortError } from '../../lib/http'
import { reportError } from '../../lib/toast'
import { useWsEvent } from '../../hooks/useWebSocket'

export interface MailView {
  accountId: number | null
  folder: MailFolder
  label: string | null
}

export function useMailThreads(view: MailView, enabled: boolean) {
  const key = JSON.stringify(view)
  const [threads, setThreads] = useState<MailThread[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const gen = useRef(0)
  const viewRef = useRef(view)
  viewRef.current = view
  const countRef = useRef(0)
  countRef.current = threads.length

  const fetchPage = useCallback((limit: number, offset: number, signal?: AbortSignal) => {
    const v = viewRef.current
    return listMailThreads({ accountId: v.accountId, folder: v.folder, label: v.label, limit, offset }, signal)
  }, [])

  const replace = useCallback(async (limit: number) => {
    const mine = ++gen.current
    try {
      const data = await fetchPage(limit, 0)
      if (mine !== gen.current) return
      setThreads(data)
      setHasMore(data.length === limit)
    } catch (e) {
      if (mine === gen.current && !isAbortError(e)) reportError(e, 'Couldn’t load mail')
    } finally {
      if (mine === gen.current) setLoading(false)
    }
  }, [fetchPage])

  useEffect(() => {
    if (!enabled) return
    setThreads([])
    setLoading(true)
    void replace(MAIL_PAGE)
  }, [key, enabled, replace])

  const reload = useCallback(() => {
    void replace(Math.min(500, Math.max(MAIL_PAGE, Math.ceil(countRef.current / MAIL_PAGE) * MAIL_PAGE)))
  }, [replace])

  const loadMore = useCallback(async () => {
    if (loadingMore || !hasMore) return
    const mine = gen.current
    setLoadingMore(true)
    try {
      const data = await fetchPage(MAIL_PAGE, countRef.current)
      if (mine !== gen.current) return
      setThreads((prev) => [...prev, ...data.filter((t) => !prev.some((p) => p.id === t.id))])
      setHasMore(data.length === MAIL_PAGE)
    } catch (e) {
      reportError(e, 'Couldn’t load more mail')
    } finally {
      setLoadingMore(false)
    }
  }, [fetchPage, hasMore, loadingMore])

  // New mail or a synced thread: refresh the visible rows (coalesced)
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null)
  const scheduleReload = useCallback(() => {
    if (pending.current) return
    pending.current = setTimeout(() => { pending.current = null; reload() }, 400)
  }, [reload])
  useEffect(() => () => { if (pending.current) clearTimeout(pending.current) }, [])
  const idsRef = useRef<Set<number>>(new Set())
  idsRef.current = new Set(threads.map((t) => t.id))
  // Email messages carry participants (chat messages don't); a sync matters when it touches a listed thread
  useWsEvent('new_message', (e) => {
    if (idsRef.current.has(e.conversation_id) || (e.message?.participants?.length ?? 0) > 0) scheduleReload()
  }, enabled)
  useWsEvent('conversations_synced', (e) => {
    if (e.conversation_ids.some((id) => idsRef.current.has(id))) scheduleReload()
  }, enabled)

  /** Local patch after an action in the reading pane */
  const patch = useCallback((id: number, fn: (t: MailThread) => MailThread | null) => {
    setThreads((prev) => prev.flatMap((t) => {
      if (t.id !== id) return [t]
      const next = fn(t)
      return next ? [next] : []
    }))
  }, [])

  return { threads, loading, loadingMore, hasMore, loadMore, reload, patch }
}
