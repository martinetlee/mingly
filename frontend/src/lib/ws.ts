/**
 * Single app-level WebSocket connection with pub/sub fan-out.
 *
 * - One socket per client instance, shared by every subscriber.
 * - stop() is an intentional close: no reconnect is ever scheduled after it.
 * - Reconnects use exponential backoff with full jitter.
 * - After every successful *re*connect a synthetic `{type:'resync'}` event is
 *   delivered so subscribers can refetch whatever they may have missed.
 * - start()/stop() are idempotent and restartable (React StrictMode runs
 *   effects twice).
 * - Close code 4001 (session ended) → onSessionExpired, no reconnect;
 *   4003 (origin rejected) → no reconnect; anything else → backoff.
 */
import type { WSEvent } from '../api/types'

export type WSEventType = WSEvent['type']

/** Server close codes (backend infra contract §5) */
export const CLOSE_SESSION_ENDED = 4001
export const CLOSE_ORIGIN_REJECTED = 4003
export type WSHandler<E extends WSEvent = WSEvent> = (event: E) => void

export type SessionCheck = () => Promise<'valid' | 'expired' | 'unknown'>

export interface WebSocketClientOptions {
  /** Returns the URL to connect to, or null when there's no session (don't connect). */
  url: () => string | null
  /** Called after repeated handshake failures to tell "server busy" from "token rejected". */
  checkSession?: SessionCheck
  /** Called when checkSession reports the session is gone. */
  onSessionExpired?: () => void
  baseDelayMs?: number
  maxDelayMs?: number
  /** Consecutive never-opened attempts before calling checkSession */
  handshakeFailuresBeforeCheck?: number
  /** Injectable for tests */
  WebSocketImpl?: typeof WebSocket
  random?: () => number
}

export class WebSocketClient {
  private readonly opts: Required<Omit<WebSocketClientOptions, 'checkSession' | 'onSessionExpired'>> &
    Pick<WebSocketClientOptions, 'checkSession' | 'onSessionExpired'>
  private ws: WebSocket | null = null
  private running = false
  private timer: ReturnType<typeof setTimeout> | null = null
  private attempt = 0
  private handshakeFailures = 0
  private everOpened = false
  private readonly handlers = new Map<string, Set<WSHandler>>()

  constructor(options: WebSocketClientOptions) {
    this.opts = {
      baseDelayMs: 1000,
      maxDelayMs: 30000,
      handshakeFailuresBeforeCheck: 3,
      WebSocketImpl: typeof WebSocket !== 'undefined' ? WebSocket : (undefined as unknown as typeof WebSocket),
      random: Math.random,
      ...options,
    }
  }

  /**
   * Subscribe to one event type, or '*' for all events. Returns an
   * unsubscribe function. Handlers don't need a live socket to register.
   */
  subscribe<T extends WSEventType>(type: T, handler: WSHandler<Extract<WSEvent, { type: T }>>): () => void
  subscribe(type: '*', handler: WSHandler): () => void
  subscribe(type: string, handler: WSHandler<never>): () => void {
    let set = this.handlers.get(type)
    if (!set) {
      set = new Set()
      this.handlers.set(type, set)
    }
    const h = handler as unknown as WSHandler
    set.add(h)
    return () => {
      const s = this.handlers.get(type)
      if (!s) return
      s.delete(h)
      if (s.size === 0) this.handlers.delete(type)
    }
  }

  /** Total registered handlers (for tests/diagnostics). */
  get handlerCount(): number {
    let n = 0
    for (const s of this.handlers.values()) n += s.size
    return n
  }

  get connected(): boolean {
    return this.ws != null && this.ws.readyState === 1 /* OPEN */
  }

  get isRunning(): boolean {
    return this.running
  }

  /** Deliver an event to subscribers (also used for synthetic events). */
  dispatch(event: WSEvent): void {
    const targets = [
      ...(this.handlers.get(event.type) ?? []),
      ...(this.handlers.get('*') ?? []),
    ]
    for (const h of targets) {
      try {
        h(event)
      } catch (e) {
        console.error('WebSocket handler failed', event.type, e)
      }
    }
  }

  start(): void {
    if (this.running) return
    this.running = true
    this.attempt = 0
    this.handshakeFailures = 0
    this.connect()
  }

  /** Intentional close: tears down the socket and cancels any pending reconnect. */
  stop(): void {
    this.running = false
    this.clearTimer()
    const ws = this.ws
    this.ws = null
    if (ws) {
      ws.onopen = null
      ws.onmessage = null
      ws.onclose = null
      ws.onerror = null
      try { ws.close() } catch { /* already closed */ }
    }
    // A later start() is a fresh session: its first open isn't a "reconnect"
    this.everOpened = false
  }

  /** Reconnect now if idle (e.g. browser came back online, token refreshed). */
  reconnectNow(): void {
    if (!this.running) return
    if (this.ws && (this.ws.readyState === 0 || this.ws.readyState === 1)) return
    this.clearTimer()
    this.attempt = 0
    this.connect()
  }

  private clearTimer(): void {
    if (this.timer != null) {
      clearTimeout(this.timer)
      this.timer = null
    }
  }

  /** Exponential backoff with full jitter in [delay/2, delay]. */
  nextDelay(): number {
    const exp = Math.min(this.opts.maxDelayMs, this.opts.baseDelayMs * 2 ** this.attempt)
    this.attempt++
    return Math.round(exp / 2 + this.opts.random() * (exp / 2))
  }

  private scheduleReconnect(delay = this.nextDelay()): void {
    if (!this.running) return
    this.clearTimer()
    this.timer = setTimeout(() => {
      this.timer = null
      this.connect()
    }, delay)
  }

  private connect(): void {
    if (!this.running) return
    const url = this.opts.url()
    // No token → nothing to connect with. Session-token listeners call
    // reconnectNow() once one exists; avoids a reconnect loop after expiry.
    if (!url) return

    const ws = new this.opts.WebSocketImpl(url)
    this.ws = ws
    let didOpen = false

    ws.onopen = () => {
      if (this.ws !== ws) return
      didOpen = true
      this.attempt = 0
      this.handshakeFailures = 0
      const isReconnect = this.everOpened
      this.everOpened = true
      if (isReconnect) this.dispatch({ type: 'resync' })
    }

    ws.onmessage = (e: MessageEvent) => {
      if (this.ws !== ws) return
      let data: WSEvent
      try {
        data = JSON.parse(e.data) as WSEvent
      } catch {
        return // ignore malformed messages
      }
      if (data && typeof data.type === 'string') this.dispatch(data)
    }

    ws.onclose = (e: CloseEvent) => {
      // Stale socket (replaced or intentionally stopped) — never reconnect from it
      if (this.ws !== ws) return
      this.ws = null
      if (!this.running) return
      if (e?.code === CLOSE_SESSION_ENDED) {
        // Token revoked/expired or vault locked: the server won't accept us again
        this.stop()
        this.opts.onSessionExpired?.()
        return
      }
      if (e?.code === CLOSE_ORIGIN_REJECTED) {
        console.error('WebSocket rejected: origin/host not allowed')
        this.stop()
        return
      }
      if (!didOpen) {
        // Handshake rejected: invalid token (403) or the server being busy.
        // Verify before assuming the session expired.
        this.handshakeFailures++
        if (this.handshakeFailures >= this.opts.handshakeFailuresBeforeCheck && this.opts.checkSession) {
          this.handshakeFailures = 0
          this.opts.checkSession().then((status) => {
            if (!this.running) return
            if (status === 'expired') {
              this.stop()
              this.opts.onSessionExpired?.()
              return
            }
            this.scheduleReconnect()
          }, () => this.scheduleReconnect())
          return
        }
      }
      this.scheduleReconnect()
    }
  }
}

export function defaultWebSocketUrl(token: string | null): string | null {
  if (!token) return null
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${protocol}://${window.location.host}/api/ws?token=${encodeURIComponent(token)}`
}
