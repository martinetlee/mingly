/**
 * Session token management with sessionStorage persistence.
 *
 * The session token is issued by the backend when the vault is unlocked or
 * initialized. It must be sent as an X-Session-Token header on all API
 * requests and as a query parameter on WebSocket connections.
 *
 * The token is persisted in sessionStorage so that it survives page reloads
 * within the same browser tab.  This prevents the frequent 401 errors that
 * occurred when every reload lost the in-memory token.
 */

const SESSION_TOKEN_KEY = 'mingly_session_token'

type Listener = () => void

const _expiredListeners = new Set<Listener>()
const _tokenListeners = new Set<Listener>()
let _sessionExpiredFired = false

function emit(listeners: Set<Listener>): void {
  for (const cb of [...listeners]) {
    try {
      cb()
    } catch (e) {
      console.error('session listener failed', e)
    }
  }
}

export function setSessionToken(token: string): void {
  _sessionExpiredFired = false
  try {
    sessionStorage.setItem(SESSION_TOKEN_KEY, token)
  } catch {
    // sessionStorage unavailable — fall through to in-memory only
  }
  emit(_tokenListeners)
}

export function getSessionToken(): string | null {
  try {
    return sessionStorage.getItem(SESSION_TOKEN_KEY)
  } catch {
    return null
  }
}

export function clearSessionToken(): void {
  try {
    sessionStorage.removeItem(SESSION_TOKEN_KEY)
  } catch {
    // ignore
  }
  emit(_tokenListeners)
}

/**
 * Register a callback fired once when the session expires (first 401).
 * Several listeners may be registered (App state machine, WebSocket client…).
 * Returns an unsubscribe function.
 */
export function onSessionExpired(cb: Listener): () => void {
  _expiredListeners.add(cb)
  return () => { _expiredListeners.delete(cb) }
}

/** Fires whenever the token is set or cleared. Returns an unsubscribe function. */
export function onSessionTokenChanged(cb: Listener): () => void {
  _tokenListeners.add(cb)
  return () => { _tokenListeners.delete(cb) }
}

export function notifySessionExpired(): void {
  clearSessionToken()
  // Guard against stampede: if multiple concurrent requests all get 401,
  // only fire the callbacks once until a new token is set.
  if (_sessionExpiredFired) return
  _sessionExpiredFired = true
  emit(_expiredListeners)
}

/**
 * Append the session token as a query parameter to a media URL.
 *
 * Browser <img> / <video> tags cannot send custom headers, so the backend
 * media endpoint also accepts the token via ``?token=``.  This helper keeps
 * the detail in one place.
 */
export function authenticatedMediaUrl(url: string): string {
  const token = getSessionToken()
  if (!token) return url
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}token=${encodeURIComponent(token)}`
}
