/**
 * One download pipeline for every message attachment (photo, video, audio,
 * voice, album item, document, gif), keyed by attachment id.
 *
 * - In-flight dedupe: all components showing the same attachment share one
 *   request and one state.
 * - Global concurrency limit: at most MAX_CONCURRENT downloads run at once;
 *   the rest queue.
 * - Bounded retries with backoff for transient failures (network, 5xx, 429);
 *   404/400/409/503 and exhausted retries are terminal for the session — no
 *   hot loop when the channel is disconnected. `retry()` (user action) resets that.
 * - Abort: when the last subscriber unmounts, a queued item is dropped and an
 *   in-flight request is aborted.
 * - `media_ready` WS events / an attachment that already has a `url` mark the
 *   item ready without a request (`setMediaReady` / `initialUrl`).
 */
import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { ApiError, downloadAttachment, isAbortError } from '../api/client'
import { errorMessage } from '../lib/toast'

export type MediaStatus = 'idle' | 'queued' | 'loading' | 'ready' | 'error'

export interface MediaState {
  status: MediaStatus
  url: string | null
  error: string | null
  attempts: number
}

export const MAX_CONCURRENT = 3
export const MAX_ATTEMPTS = 3
const BACKOFF_MS = [1000, 4000]

const IDLE: MediaState = Object.freeze({ status: 'idle', url: null, error: null, attempts: 0 }) as MediaState

interface Entry {
  state: MediaState
  subscribers: number
  ctl: AbortController | null
  retryTimer: ReturnType<typeof setTimeout> | null
}

const entries = new Map<number, Entry>()
const listeners = new Map<number, Set<() => void>>()
const queue: number[] = []
let running = 0

function entry(id: number): Entry {
  let e = entries.get(id)
  if (!e) {
    e = { state: IDLE, subscribers: 0, ctl: null, retryTimer: null }
    entries.set(id, e)
  }
  return e
}

function set(id: number, patch: Partial<MediaState>) {
  const e = entry(id)
  e.state = { ...e.state, ...patch }
  listeners.get(id)?.forEach((l) => l())
}

function isTerminal(e: unknown): boolean {
  if (e instanceof ApiError) {
    if (e.status === 404 || e.status === 400 || e.status === 409 || e.status === 503 || e.status === 401) return true
    if (e.code === 'session_expired') return true
  }
  return false
}

function pump() {
  while (running < MAX_CONCURRENT && queue.length > 0) {
    const id = queue.shift()!
    const e = entries.get(id)
    if (!e || e.state.status !== 'queued') continue
    void run(id, e)
  }
}

async function run(id: number, e: Entry) {
  running++
  const ctl = new AbortController()
  e.ctl = ctl
  set(id, { status: 'loading', attempts: e.state.attempts + 1 })
  try {
    const res = await downloadAttachment(id, ctl.signal)
    set(id, { status: 'ready', url: res.url, error: null })
  } catch (err) {
    if (isAbortError(err)) {
      set(id, { status: 'idle' })
    } else if (!isTerminal(err) && entry(id).state.attempts < MAX_ATTEMPTS) {
      const delay = BACKOFF_MS[Math.min(entry(id).state.attempts - 1, BACKOFF_MS.length - 1)]
      set(id, { status: 'queued', error: errorMessage(err) })
      e.retryTimer = setTimeout(() => {
        e.retryTimer = null
        if (e.subscribers > 0 && e.state.status === 'queued') { queue.push(id); pump() }
        else if (e.state.status === 'queued') set(id, { status: 'idle' })
      }, delay)
    } else {
      set(id, { status: 'error', error: errorMessage(err) })
    }
  } finally {
    e.ctl = null
    running--
    pump()
  }
}

/** Request a download (no-op if ready/in flight/terminally failed). */
export function startMediaDownload(attachmentId: number, opts: { force?: boolean } = {}): void {
  const e = entry(attachmentId)
  const st = e.state.status
  if (st === 'ready' || st === 'loading' || st === 'queued') return
  if (st === 'error' && !opts.force) return
  set(attachmentId, { status: 'queued', error: null, attempts: opts.force ? 0 : e.state.attempts })
  queue.push(attachmentId)
  pump()
}

/** A WS `media_ready` or a payload whose attachment carries a url. */
export function setMediaReady(attachmentId: number, url: string): void {
  const e = entry(attachmentId)
  if (e.state.status === 'ready' && e.state.url === url) return
  e.ctl?.abort()
  set(attachmentId, { status: 'ready', url, error: null })
}

export function getMediaState(attachmentId: number): MediaState {
  return entries.get(attachmentId)?.state ?? IDLE
}

function retain(id: number): () => void {
  const e = entry(id)
  e.subscribers++
  return () => {
    e.subscribers--
    if (e.subscribers > 0) return
    if (e.state.status === 'queued') {
      const i = queue.indexOf(id)
      if (i !== -1) queue.splice(i, 1)
      if (e.retryTimer) { clearTimeout(e.retryTimer); e.retryTimer = null }
      set(id, { status: 'idle' })
    } else if (e.state.status === 'loading') {
      e.ctl?.abort()
    }
  }
}

export interface UseMediaDownload extends MediaState {
  /** Start (user click); also retries a terminal error */
  start: () => void
  retry: () => void
}

/**
 * `auto`: start downloading on mount (photos, gifs, album thumbnails).
 * Click-to-load media (video, audio, documents) pass auto=false and call start().
 */
export function useMediaDownload(
  attachmentId: number,
  opts: { auto?: boolean; initialUrl?: string | null } = {},
): UseMediaDownload {
  const { auto = false, initialUrl = null } = opts

  const subscribe = useCallback((l: () => void) => {
    let s = listeners.get(attachmentId)
    if (!s) { s = new Set(); listeners.set(attachmentId, s) }
    s.add(l)
    const release = retain(attachmentId)
    return () => { s!.delete(l); release() }
  }, [attachmentId])
  const get = useCallback(() => entries.get(attachmentId)?.state ?? IDLE, [attachmentId])
  const state = useSyncExternalStore(subscribe, get, get)

  useEffect(() => {
    if (initialUrl) setMediaReady(attachmentId, initialUrl)
  }, [attachmentId, initialUrl])

  useEffect(() => {
    if (auto && !initialUrl) startMediaDownload(attachmentId)
  }, [auto, initialUrl, attachmentId])

  const start = useCallback(() => {
    const st = getMediaState(attachmentId).status
    startMediaDownload(attachmentId, { force: st === 'error' })
  }, [attachmentId])
  const retry = useCallback(() => startMediaDownload(attachmentId, { force: true }), [attachmentId])

  const url = initialUrl ?? state.url
  return { ...state, url, status: url ? 'ready' : state.status, start, retry }
}

/** Test-only */
export function __resetMediaDownloads(): void {
  for (const e of entries.values()) {
    e.ctl?.abort()
    if (e.retryTimer) clearTimeout(e.retryTimer)
  }
  entries.clear()
  listeners.clear()
  queue.length = 0
  running = 0
}
