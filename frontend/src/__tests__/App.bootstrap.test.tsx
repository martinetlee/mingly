/**
 * App bootstrap (review §Infra "Single global session token"):
 * - a failed vault-status request shows "backend unavailable, retrying", not "create vault"
 * - an unlocked vault without a valid token for *this* tab → Unlock, not the full app
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, cleanup, fireEvent } from '@testing-library/react'
import { ApiError } from '../lib/http'

const getVaultStatus = vi.fn()
const validateSession = vi.fn()

vi.mock('../api/client', async (orig) => ({
  ...(await orig<typeof import('../api/client')>()),
  getVaultStatus: () => getVaultStatus(),
  validateSession: () => validateSession(),
  openExternalUrl: vi.fn(),
  getPrefs: vi.fn(async () => ({})),
}))

const { stub } = vi.hoisted(() => ({ stub: (name: string) => ({ default: () => name }) }))
vi.mock('../pages/Onboarding', () => ({ default: ({ onComplete }: { onComplete: () => void }) => <button onClick={onComplete}>CreateVault</button> }))
vi.mock('../pages/Unlock', () => ({ default: ({ onUnlock }: { onUnlock: () => void }) => <button onClick={onUnlock}>UnlockScreen</button> }))
vi.mock('../pages/LlmSetup', () => ({ default: ({ onComplete }: { onComplete: () => void }) => <button onClick={onComplete}>LlmSetup</button> }))
vi.mock('../components/layout/Layout', () => stub('MainApp'))
vi.mock('../pages/Home', () => stub('Home'))
vi.mock('../pages/Chats', () => stub('Chats'))
vi.mock('../pages/Contacts', () => stub('Contacts'))
vi.mock('../pages/Organizations', () => stub('Orgs'))
vi.mock('../pages/Settings', () => stub('Settings'))
vi.mock('../pages/Tracked', () => stub('Tracked'))
vi.mock('../pages/Intelligence', () => stub('Intelligence'))
vi.mock('../pages/Shield', () => stub('Shield'))
vi.mock('../pages/Board', () => stub('Board'))
vi.mock('../context/WorkspaceContext', () => ({ WorkspaceProvider: ({ children }: { children: React.ReactNode }) => <>{children}</> }))
vi.mock('../context/WebSocketContext', () => ({
  WebSocketProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useWebSocketClient: () => null,
}))

import App from '../App'
import { setSessionToken, clearSessionToken, getSessionToken } from '../api/session'

beforeEach(() => {
  vi.clearAllMocks()
  clearSessionToken()
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('App bootstrap', () => {
  it('shows "backend unavailable" (not create-vault) when validate fails, then recovers', async () => {
    vi.useFakeTimers()
    validateSession.mockRejectedValueOnce(new ApiError('Internal server error', { status: 500, code: 'http', path: '/api/session/validate' }))
    validateSession.mockResolvedValueOnce({ valid: false, vault_status: 'locked' })
    render(<App />)
    await act(async () => { await Promise.resolve() })
    expect(screen.getByText("Can't reach the Mingly backend")).toBeTruthy()
    expect(screen.queryByText('CreateVault')).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(screen.getByText('UnlockScreen')).toBeTruthy()
  })

  it('unlocked vault but no valid token in this tab → Unlock', async () => {
    validateSession.mockResolvedValue({ valid: false, vault_status: 'unlocked' })
    render(<App />)
    expect(await screen.findByText('UnlockScreen')).toBeTruthy()
  })

  it('stale token rejected by /api/session/validate → cleared, Unlock', async () => {
    setSessionToken('old')
    validateSession.mockResolvedValue({ valid: false, vault_status: 'unlocked' })
    render(<App />)
    expect(await screen.findByText('UnlockScreen')).toBeTruthy()
    expect(getSessionToken()).toBeNull()
  })

  it('no vault → Create vault', async () => {
    validateSession.mockResolvedValue({ valid: false, vault_status: 'unset' })
    render(<App />)
    expect(await screen.findByText('CreateVault')).toBeTruthy()
  })

  it('valid token → app, whatever accounts are connected (no channel gate)', async () => {
    setSessionToken('good')
    validateSession.mockResolvedValue({ valid: true, vault_status: 'unlocked', idle_timeout_minutes: 720 })
    render(<App />)
    expect(await screen.findByText('MainApp')).toBeTruthy()
  })

  it('unlock → app directly', async () => {
    validateSession.mockResolvedValue({ valid: false, vault_status: 'locked' })
    render(<App />)
    fireEvent.click(await screen.findByText('UnlockScreen'))
    expect(await screen.findByText('MainApp')).toBeTruthy()
  })

  it('new vault → LLM setup → app', async () => {
    validateSession.mockResolvedValue({ valid: false, vault_status: 'unset' })
    render(<App />)
    fireEvent.click(await screen.findByText('CreateVault'))
    fireEvent.click(await screen.findByText('LlmSetup'))
    expect(await screen.findByText('MainApp')).toBeTruthy()
  })

  it('401 while validating → Unlock (not backend-unavailable)', async () => {
    validateSession.mockRejectedValue(new ApiError('Session expired', { status: 401, code: 'session_expired', path: '/api/session/validate' }))
    render(<App />)
    expect(await screen.findByText('UnlockScreen')).toBeTruthy()
    expect(screen.queryByText("Can't reach the Mingly backend")).toBeNull()
  })

  describe('pre-contract backend (validate endpoint missing → null)', () => {
    beforeEach(() => validateSession.mockResolvedValue(null))

    it('vault-status failure → backend unavailable, not create-vault', async () => {
      getVaultStatus.mockRejectedValue(new ApiError('Network error', { status: 0, code: 'network', path: '/api/auth/vault-status' }))
      render(<App />)
      expect(await screen.findByText("Can't reach the Mingly backend")).toBeTruthy()
    })

    it('unlocked without a token → Unlock', async () => {
      getVaultStatus.mockResolvedValue({ status: 'unlocked' })
      render(<App />)
      expect(await screen.findByText('UnlockScreen')).toBeTruthy()
    })
  })
})
