/**
 * In-memory session token management.
 *
 * The session token is issued by the backend when the vault is unlocked or
 * initialized. It must be sent as an X-Session-Token header on all API
 * requests and as a query parameter on WebSocket connections.
 */

let _sessionToken: string | null = null

export function setSessionToken(token: string): void {
  _sessionToken = token
}

export function getSessionToken(): string | null {
  return _sessionToken
}

export function clearSessionToken(): void {
  _sessionToken = null
}
