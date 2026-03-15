/**
 * In-memory session token management.
 *
 * The session token is issued by the backend when the vault is unlocked or
 * initialized. It must be sent as an X-Session-Token header on all API
 * requests and as a query parameter on WebSocket connections.
 */

let _sessionToken: string | null = null
let _onSessionExpired: (() => void) | null = null

export function setSessionToken(token: string): void {
  _sessionToken = token
}

export function getSessionToken(): string | null {
  return _sessionToken
}

export function clearSessionToken(): void {
  _sessionToken = null
}

export function onSessionExpired(cb: () => void): void {
  _onSessionExpired = cb
}

export function notifySessionExpired(): void {
  _sessionToken = null
  _onSessionExpired?.()
}

/**
 * Append the session token as a query parameter to a media URL.
 *
 * Browser <img> / <video> tags cannot send custom headers, so the backend
 * media endpoint also accepts the token via ``?token=``.  This helper keeps
 * the detail in one place.
 */
export function authenticatedMediaUrl(url: string): string {
  if (!_sessionToken) return url
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}token=${encodeURIComponent(_sessionToken)}`
}
