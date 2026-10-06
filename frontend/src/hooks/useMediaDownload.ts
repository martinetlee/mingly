/**
 * One download pipeline for every message media item (photo, video, audio,
 * voice, album item, document, gif).
 *
 * - In-flight dedupe: all components showing the same (chat, message) share
 *   one request and one state.
 * - Global concurrency limit: at most MAX_CONCURRENT downloads run at once;
 *   the rest queue.
 * - Bounded retries with backoff for transient failures (network, 5xx, 429);
 *   404/400/503 and exhausted retries are terminal for the session — no hot
 *   loop when Telegram is disconnected. `retry()` (user action) resets that.
 * - Abort: when the last subscriber unmounts, a queued item is dropped and an
 *   in-flight request is aborted.
 * - `media_ready` WS events / a message that already has `media_url` mark the
 *   item ready without a request (`setMediaReady` / `initialUrl`).
 */
import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { ApiError, downloadMedia, isAbortError } from '../api/client'
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

const entries = new Map<string, Entry>()
const listeners = new Map<string, Set<() => void>>()
const queue: string[] = []
let running = 0

export const mediaKey = (chatId: number, msgId: number) => `${chatId}:${msgId}`

function entry(key: string): Entry {
  let e = entries.get(key)
  if (!e) {
    e = { state: IDLE, subscribers: 0, ctl: null, retryTimer: null }
    entries.set(key, e)
  }
  return e
}

function set(key: string, patch: Partial<MediaState>) {
  const e = entry(key)
  e.state = { ...e.state, ...patch }
  listeners.get(key)?.forEach((l) => l())
}

function isTerminal(e: unknown): boolean {
  if (e instanceof ApiError) {
    if (e.status === 404 || e.status === 400 || e.status === 503 || e.status === 401) return true
    if (e.code === 'session_expired') return true
  }
  return false
}

function parseKey(key: string): [number, number] {
  const [c, m] = key.split(':')
  return [Number(c), Number(m)]
}

function pump() {
  while (running < MAX_CONCURRENT && queue.length > 0) {
    const key = queue.shift()!
    const e = entries.get(key)
    if (!e || e.state.status !== 'queued') continue
    void run(key, e)
  }
}

async function run(key: string, e: Entry) {
  running++
  const ctl = new AbortController()
  e.ctl = ctl
  set(key, { status: 'loading', attempts: e.state.attempts + 1 })
  const [chatId, msgId] = parseKey(key)
  try {
    const res = await downloadMedia(chatId, msgId, ctl.signal)
    set(key, { status: 'ready', url: res.media_url, error: null })
  } catch (err) {
    if (isAbortError(err)) {
      set(key, { status: 'idle' })
    } else if (!isTerminal(err) && entry(key).state.attempts < MAX_ATTEMPTS) {
      const delay = BACKOFF_MS[Math.min(entry(key).state.attempts - 1, BACKOFF_MS.length - 1)]
      set(key, { status: 'queued', error: errorMessage(err) })
      e.retryTimer = setTimeout(() => {
        e.retryTimer = null
        if (e.subscribers > 0 && e.state.status === 'queued') { queue.push(key); pump() }
        else if (e.state.status === 'queued') set(key, { status: 'idle' })
      }, delay)
    } else {
      set(key, { status: 'error', error: errorMessage(err) })
    }
  } finally {
    e.ctl = null
    running--
    pump()
  }
}

/** Request a download (no-op if ready/in flight/terminally failed). */
export function startMediaDownload(chatId: number, msgId: number, opts: { force?: boolean } = {}): void {
  const key = mediaKey(chatId, msgId)
  const e = entry(key)
  const st = e.state.status
  if (st === 'ready' || st === 'loading' || st === 'queued') return
  if (st === 'error' && !opts.force) return
  set(key, { status: 'queued', error: null, attempts: opts.force ? 0 : e.state.attempts })
  queue.push(key)
  pump()
}

/** A WS `media_ready` or a payload that carries media_url. */
export function setMediaReady(chatId: number, msgId: number, url: string): void {
  const key = mediaKey(chatId, msgId)
  const e = entry(key)
  if (e.state.status === 'ready' && e.state.url === url) return
  e.ctl?.abort()
  set(key, { status: 'ready', url, error: null })
}

export function getMediaState(chatId: number, msgId: number): MediaState {
  return entries.get(mediaKey(chatId, msgId))?.state ?? IDLE
}

function retain(key: string): () => void {
  const e = entry(key)
  e.subscribers++
  return () => {
    e.subscribers--
    if (e.subscribers > 0) return
    if (e.state.status === 'queued') {
      const i = queue.indexOf(key)
      if (i !== -1) queue.splice(i, 1)
      if (e.retryTimer) { clearTimeout(e.retryTimer); e.retryTimer = null }
      set(key, { status: 'idle' })
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
  chatId: number,
  msgId: number,
  opts: { auto?: boolean; initialUrl?: string | null } = {},
): UseMediaDownload {
  const key = mediaKey(chatId, msgId)
  const { auto = false, initialUrl = null } = opts

  const subscribe = useCallback((l: () => void) => {
    let s = listeners.get(key)
    if (!s) { s = new Set(); listeners.set(key, s) }
    s.add(l)
    const release = retain(key)
    return () => { s!.delete(l); release() }
  }, [key])
  const get = useCallback(() => entries.get(key)?.state ?? IDLE, [key])
  const state = useSyncExternalStore(subscribe, get, get)

  useEffect(() => {
    if (initialUrl) setMediaReady(chatId, msgId, initialUrl)
  }, [chatId, msgId, initialUrl])

  useEffect(() => {
    if (auto && !initialUrl) startMediaDownload(chatId, msgId)
  }, [auto, initialUrl, chatId, msgId])

  const start = useCallback(() => {
    const st = getMediaState(chatId, msgId).status
    startMediaDownload(chatId, msgId, { force: st === 'error' })
  }, [chatId, msgId])
  const retry = useCallback(() => startMediaDownload(chatId, msgId, { force: true }), [chatId, msgId])

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
