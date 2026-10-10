/** Vault setup, unlock/lock and session validation. */

import { ApiError, isSessionExpiredError, request } from '../lib/http'
import { clearSessionToken, notifySessionExpired, setSessionToken } from './session'

export const getVaultStatus = () =>
  request<{ status: 'unset' | 'locked' | 'unlocked' }>('/api/auth/vault-status')

export type SessionValidation = {
  valid: boolean
  vault_status: 'unset' | 'locked' | 'unlocked'
  idle_timeout_minutes?: number
}

/**
 * Ask the backend whether this tab's session token is still valid (no token
 * required; also refreshes the idle timer). Resolves to null only when the
 * endpoint doesn't exist (pre-contract backend) so callers can fall back to
 * vault status. Never fires the global session-expired handler. Network
 * errors and 5xx throw — callers must never read those as "no vault".
 */
export const validateSession = async (signal?: AbortSignal): Promise<SessionValidation | null> => {
  try {
    return await request<SessionValidation>('/api/session/validate', { signal, skipSessionExpiry: true, attempts: 1 })
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 405 || e.code === 'html_response')) return null
    throw e
  }
}

export const initVault = async (password: string) => {
  const result = await request<{ success: boolean; session_token: string }>('/api/auth/vault/init', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })
  if (result.session_token) {
    setSessionToken(result.session_token)
  }
  return result
}

export const unlockVault = async (password: string) => {
  const result = await request<{ success: boolean; session_token: string }>('/api/auth/vault/unlock', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })
  if (result.session_token) {
    setSessionToken(result.session_token)
  }
  return result
}

/**
 * Destroy the vault. The backend requires the user's typed confirmation
 * (`"RESET"`); anything else is rejected with 400 — collect it in the UI.
 */
export const resetVault = async (confirm?: string) => {
  const result = await request<{ success: boolean }>('/api/auth/vault/reset', {
    method: 'POST',
    body: JSON.stringify({ confirm: confirm ?? '' }),
  })
  clearSessionToken()
  return result
}

export const resolve1Password = (reference: string) =>
  request<{ value: string }>('/api/auth/resolve-1password', {
    method: 'POST',
    body: JSON.stringify({ reference }),
  })

/**
 * Lock the vault: the backend revokes every session, stops Telegram and
 * closes all WebSockets. Then end this tab's session through the same path
 * as expiry (token cleared once, App → Unlock, socket stopped), so in-flight
 * requests that 401 afterwards hit the stampede guard instead of a storm.
 * A 401 means the session was already gone — still lock locally.
 */
export const lockVault = async (): Promise<void> => {
  try {
    await request<{ success: boolean }>('/api/auth/lock', { method: 'POST' })
  } catch (e) {
    if (!isSessionExpiredError(e)) throw e
  }
  notifySessionExpired()
}
