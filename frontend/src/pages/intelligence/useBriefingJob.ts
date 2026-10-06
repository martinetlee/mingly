import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { ApiError, cancelBriefing, getBriefingStatus, isAbortError, stream } from '../../api/client'
import type { BriefingHistoryEntry, BriefingSummary } from '../../api/types'
import { errorMessage, reportError } from '../../lib/toast'

// ---------------------------------------------------------------------------
// Full briefing: a single-flight server job we start or attach to
// ---------------------------------------------------------------------------

export interface BriefingProgress {
  current: number
  total: number
  chatName: string
  stage?: 'syncing'
  tokens?: number
}

export interface BriefingChatError {
  chat_id: number
  chat_name: string
  message: string
}

export type BriefingOutcome =
  | { kind: 'done'; runId: number | null; status: 'done' | 'partial' | 'empty'; entries: number; failed: number }
  | { kind: 'error'; runId: number | null; message: string }
  | { kind: 'cancelled' }
  /** Refused: another briefing is running (see state.conflict) */
  | { kind: 'conflict' }
  /** The stream ended without a terminal event and no job is running any more */
  | { kind: 'lost' }

/** A Generate request was refused because a briefing with another window is running */
export interface BriefingConflict {
  runningDays: number | null
  /** The window the user asked for (re-run after "Cancel & run") */
  requestedDays: number | null
  trigger: 'manual' | 'scheduled' | null
  message: string
}

/** Recognise the 409 `briefing_running` refusal (HTTP error or SSE error payload). */
export function briefingConflictOf(e: unknown): BriefingConflict | null {
  let body: Record<string, unknown> | undefined
  if (e instanceof ApiError && (e.serverCode === 'briefing_running' || e.body?.code === 'briefing_running')) {
    body = { ...e.body, message: e.message }
  } else if (e && typeof e === 'object' && !(e instanceof Error) && (e as Record<string, unknown>).code === 'briefing_running') {
    body = e as Record<string, unknown>
  }
  if (!body) return null
  const days = Number(body.running_days)
  const requested = Number(body.requested_days)
  const trigger = body.trigger === 'manual' || body.trigger === 'scheduled' ? body.trigger : null
  return {
    runningDays: Number.isFinite(days) && days > 0 ? days : null,
    requestedDays: Number.isFinite(requested) && requested > 0 ? requested : null,
    trigger,
    message: String(body.message ?? body.detail ?? 'A briefing is already running'),
  }
}

export function describeBriefingWindow(days: number | null): string {
  if (days == null) return 'A briefing'
  if (days === 1) return 'A daily briefing'
  if (days === 7) return 'A weekly briefing'
  return `A ${days}-day briefing`
}

export interface BriefingJobState {
  running: boolean
  /** True when this view joined a job that was already running (other tab, scheduler) */
  attached: boolean
  days: number | null
  runId: number | null
  progress: BriefingProgress | null
  live: BriefingSummary[]
  failed: BriefingChatError[]
  flowOrder: string[]
  flowByChat: Record<string, string>
  error: string
  conflict: BriefingConflict | null
}

export const initialBriefingState: BriefingJobState = {
  running: false, attached: false, days: null, runId: null, progress: null,
  live: [], failed: [], flowOrder: [], flowByChat: {}, error: '', conflict: null,
}

export type BriefingAction =
  | { type: 'begin'; days: number; attached: boolean }
  | { type: 'event'; event: string; data: Record<string, unknown> }
  | { type: 'end'; error?: string }
  | { type: 'clearError' }
  | { type: 'conflict'; conflict: BriefingConflict | null }

const addFlowChat = (order: string[], name: string) => (name && !order.includes(name) ? [...order, name] : order)

/** Pure reducer over the briefing SSE events (see api/llm.md §1). */
export function briefingReducer(state: BriefingJobState, action: BriefingAction): BriefingJobState {
  switch (action.type) {
    case 'begin':
      // Attaching replays every event from the start, so always reset
      return { ...initialBriefingState, running: true, attached: action.attached, days: action.days }
    case 'clearError':
      return { ...state, error: '' }
    case 'conflict':
      return { ...state, running: false, progress: null, conflict: action.conflict }
    case 'end':
      return { ...state, running: false, progress: null, error: action.error ?? state.error }
    case 'event': {
      const d = action.data
      switch (action.event) {
        case 'start':
          return {
            ...state,
            days: typeof d.days === 'number' ? d.days : state.days,
            runId: typeof d.run_id === 'number' ? d.run_id : state.runId,
            progress: { current: 0, total: Number(d.total ?? 0), chatName: '' },
          }
        case 'progress': {
          const chatName = String(d.chat_name ?? '')
          if (d.stage === 'syncing') {
            return { ...state, progress: { current: 0, total: Number(d.total ?? 0), chatName: '', stage: 'syncing' } }
          }
          return {
            ...state,
            progress: { current: Number(d.current ?? 0), total: Number(d.total ?? state.progress?.total ?? 0), chatName, tokens: 0 },
            flowOrder: addFlowChat(state.flowOrder, chatName),
          }
        }
        case 'thinking': {
          const chatName = String(d.chat_name ?? '')
          let next = state
          if (typeof d.tokens === 'number' && state.progress) {
            next = { ...next, progress: { ...state.progress, chatName, tokens: d.tokens } }
          }
          if (typeof d.reasoning === 'string' && d.reasoning) {
            next = {
              ...next,
              flowOrder: addFlowChat(next.flowOrder, chatName),
              flowByChat: { ...next.flowByChat, [chatName]: (next.flowByChat[chatName] ?? '') + d.reasoning },
            }
          }
          return next
        }
        case 'chat_done':
          return {
            ...state,
            runId: typeof d.run_id === 'number' ? d.run_id : state.runId,
            live: [...state.live, d as unknown as BriefingSummary],
          }
        case 'chat_error':
          return {
            ...state,
            failed: [...state.failed, {
              chat_id: Number(d.chat_id ?? 0),
              chat_name: String(d.chat_name ?? ''),
              message: String(d.message ?? 'Failed'),
            }],
          }
        case 'error': {
          const conflict = briefingConflictOf(d)
          if (conflict) return { ...state, conflict }
          return { ...state, error: String(d.message ?? 'Briefing failed') }
        }
        default:
          return state
      }
    }
  }
}

function outcomeOf(event: string, d: Record<string, unknown>): BriefingOutcome | null {
  const runId = typeof d.run_id === 'number' ? d.run_id : null
  if (event === 'done') {
    const status = d.status === 'partial' || d.status === 'empty' ? d.status : 'done'
    return { kind: 'done', runId, status, entries: Number(d.entries ?? 0), failed: Number(d.failed ?? 0) }
  }
  if (event === 'error') {
    if (briefingConflictOf(d)) return { kind: 'conflict' }
    return { kind: 'error', runId, message: String(d.message ?? 'Briefing failed') }
  }
  if (event === 'cancelled') return { kind: 'cancelled' }
  return null
}

const MAX_REATTACH = 3

/**
 * Drive the briefing job: start it, attach to one already running (on mount,
 * or a scheduled run), cancel it, and stop listening on unmount (the job
 * keeps running server-side and can be re-attached later).
 */
export function useBriefingJob(onFinished: (outcome: BriefingOutcome) => void) {
  const [state, dispatch] = useReducer(briefingReducer, initialBriefingState)
  const ctrlRef = useRef<AbortController | null>(null)
  const onFinishedRef = useRef(onFinished)
  onFinishedRef.current = onFinished
  const [cancelling, setCancelling] = useState(false)

  useEffect(() => () => {
    ctrlRef.current?.abort()
    ctrlRef.current = null
  }, [])

  /**
   * mode: 'start' = start or join the same window (409 on a different one),
   * 'attach' = join only (server replies `idle` when nothing runs),
   * 'replace' = atomically cancel whatever runs and start this window.
   */
  const follow = useCallback(async (days: number, mode: 'start' | 'attach' | 'replace', reattaches = 0): Promise<void> => {
    const attach = mode === 'attach'
    ctrlRef.current?.abort()
    const ctrl = new AbortController()
    ctrlRef.current = ctrl
    const isCurrent = () => ctrlRef.current === ctrl
    dispatch({ type: 'begin', days, attached: attach })

    let outcome: BriefingOutcome | null = null
    let idle = false
    try {
      const path = `/api/intelligence/briefing?days=${days}${mode === 'start' ? '' : `&${mode}=1`}`
      for await (const ev of stream<Record<string, unknown>>(path, { signal: ctrl.signal })) {
        if (!isCurrent()) return
        if (ev.event === 'idle') { idle = true; break }
        dispatch({ type: 'event', event: ev.event, data: ev.data ?? {} })
        outcome = outcomeOf(ev.event, ev.data ?? {})
        if (outcome) break
      }
    } catch (e) {
      if (isAbortError(e) || !isCurrent()) return
      ctrlRef.current = null
      setCancelling(false)
      const conflict = briefingConflictOf(e)
      if (conflict) dispatch({ type: 'conflict', conflict })
      else dispatch({ type: 'end', error: `Briefing failed: ${errorMessage(e)}` })
      return
    }
    if (!isCurrent()) return

    if (!outcome && !idle) {
      // Stream dropped mid-run: the job may still be going — re-attach (replays)
      const status = await getBriefingStatus().catch(() => null)
      if (!isCurrent()) return
      if (status?.running && reattaches < MAX_REATTACH) {
        return follow(status.days, 'attach', reattaches + 1)
      }
      outcome = { kind: 'lost' }
    }
    ctrlRef.current = null
    dispatch({ type: 'end' })
    setCancelling(false)
    if (outcome) onFinishedRef.current(outcome)
  }, [])

  /** Start a manual briefing (no-op while one is running in this view). */
  const start = useCallback((days: number) => {
    if (ctrlRef.current) return
    void follow(days, 'start')
  }, [follow])

  /** If a briefing (manual elsewhere or scheduled) is running, show it. */
  const attachIfRunning = useCallback(async () => {
    if (ctrlRef.current) return
    try {
      const status = await getBriefingStatus()
      if (status.running && !ctrlRef.current) void follow(status.days, 'attach')
    } catch (e) {
      reportError(e, 'Could not check briefing status')
    }
  }, [follow])

  const cancel = useCallback(async () => {
    setCancelling(true)
    try {
      const res = await cancelBriefing()
      // Nothing running server-side: just stop listening
      if (!res.cancelled) {
        ctrlRef.current?.abort()
        ctrlRef.current = null
        dispatch({ type: 'end' })
        setCancelling(false)
      }
    } catch (e) {
      setCancelling(false)
      reportError(e, 'Could not stop the briefing')
    }
  }, [])

  const clearError = useCallback(() => dispatch({ type: 'clearError' }), [])
  const dismissConflict = useCallback(() => dispatch({ type: 'conflict', conflict: null }), [])

  /** Conflict banner: follow the briefing that is already running */
  const joinRunning = useCallback(() => {
    if (ctrlRef.current) return
    void attachIfRunning()
  }, [attachIfRunning])

  /**
   * Conflict banner "Cancel & run": one atomic request (`replace=1`) — the
   * server cancels the running briefing and starts this window under its
   * job lock, so there is no cancel/poll/start race and no second 409.
   */
  const cancelAndRun = useCallback((requestedDays: number) => {
    if (ctrlRef.current) return
    void follow(requestedDays, 'replace')
  }, [follow])

  return { state, start, attachIfRunning, cancel, cancelling, clearError, dismissConflict, joinRunning, cancelAndRun }
}

// ---------------------------------------------------------------------------
// Regenerating one entry of a past run
// ---------------------------------------------------------------------------

export interface RegenerateState {
  /** Entry being regenerated right now (one at a time) */
  activeEntryId: number | null
  reasoning: Record<number, string>
  tokens: Record<number, number>
}

/**
 * One regeneration at a time. Each run owns an AbortController; state is only
 * cleared in `finally` when that controller is still the current one, so a
 * finished/aborted older run can never wipe the spinner of a newer one.
 * Unmount aborts (the server cancels when no client is attached).
 */
export function useRegenerateEntry(onUpdated: (runId: number, entry: BriefingHistoryEntry) => void) {
  const [state, setState] = useState<RegenerateState>({ activeEntryId: null, reasoning: {}, tokens: {} })
  const ctrlRef = useRef<AbortController | null>(null)
  const onUpdatedRef = useRef(onUpdated)
  onUpdatedRef.current = onUpdated

  useEffect(() => () => {
    ctrlRef.current?.abort()
    ctrlRef.current = null
  }, [])

  const regenerate = useCallback(async (runId: number, entryId: number) => {
    if (ctrlRef.current) return // another regeneration is running; its buttons are disabled
    const ctrl = new AbortController()
    ctrlRef.current = ctrl
    const isCurrent = () => ctrlRef.current === ctrl
    setState((s) => ({
      activeEntryId: entryId,
      reasoning: { ...s.reasoning, [entryId]: '' },
      tokens: { ...s.tokens, [entryId]: 0 },
    }))
    try {
      const path = `/api/intelligence/briefing/history/${runId}/entries/${entryId}/regenerate`
      for await (const ev of stream<Record<string, unknown>>(path, { method: 'POST', signal: ctrl.signal })) {
        if (!isCurrent()) return
        const d = ev.data ?? {}
        if (ev.event === 'thinking') {
          if (typeof d.tokens === 'number') {
            const tokens = d.tokens
            setState((s) => ({ ...s, tokens: { ...s.tokens, [entryId]: tokens } }))
          }
          if (typeof d.reasoning === 'string' && d.reasoning) {
            const chunk = d.reasoning
            setState((s) => ({ ...s, reasoning: { ...s.reasoning, [entryId]: (s.reasoning[entryId] ?? '') + chunk } }))
          }
        } else if (ev.event === 'done') {
          onUpdatedRef.current(runId, d.entry as BriefingHistoryEntry)
          break
        } else if (ev.event === 'error') {
          reportError(new Error(String(d.message ?? 'Unknown error')), 'Regeneration failed')
          break
        }
      }
    } catch (e) {
      if (isCurrent()) reportError(e, 'Regeneration failed')
    } finally {
      if (isCurrent()) {
        ctrlRef.current = null
        setState((s) => ({ ...s, activeEntryId: null }))
      }
    }
  }, [])

  const stop = useCallback(() => {
    const ctrl = ctrlRef.current
    if (!ctrl) return
    ctrlRef.current = null
    ctrl.abort()
    setState((s) => ({ ...s, activeEntryId: null }))
  }, [])

  return { ...state, regenerate, stop }
}
