/**
 * App bootstrap (review §Infra "Single global session token"):
 * - a failed vault-status request shows "backend unavailable, retrying", not "create vault"
 * - an unlocked vault without a valid token for *this* tab → Unlock, not the full app
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, cleanup } from '@testing-library/react'
import { ApiError } from '../lib/http'

const getVaultStatus = vi.fn()
const getAuthStatus = vi.fn()
const validateSession = vi.fn()

vi.mock('../api/client', async (orig) => ({
  ...(await orig<typeof import('../api/client')>()),
  getVaultStatus: () => getVaultStatus(),
  getAuthStatus: () => getAuthStatus(),
  validateSession: () => validateSession(),
  openExternalUrl: vi.fn(),
  getPrefs: vi.fn(async () => ({})),
}))

const { stub } = vi.hoisted(() => ({ stub: (name: string) => ({ default: () => name }) }))
vi.mock('../pages/Onboarding', () => ({ default: ({ needsVaultSetup }: { needsVaultSetup?: boolean }) => <div>{needsVaultSetup ? 'CreateVault' : 'Onboarding'}</div> }))
vi.mock('../pages/Unlock', () => stub('UnlockScreen'))
vi.mock('../pages/LlmSetup', () => stub('LlmSetup'))
vi.mock('../components/Layout', () => stub('MainApp'))
vi.mock('../pages/Home', () => stub('Home'))
vi.mock('../pages/Chats', () => stub('Chats'))
vi.mock('../pages/Contacts', () => stub('Contacts'))
vi.mock('../pages/Organizations', () => stub('Orgs'))
vi.mock('../pages/Settings', () => stub('Settings'))
vi.mock('../pages/Tracked', () => stub('Tracked'))
vi.mock('../pages/Intelligence', () => stub('Intelligence'))
vi.mock('../pages/Profile', () => stub('Profile'))
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
    expect(getAuthStatus).not.toHaveBeenCalled()
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

  it('valid token → app', async () => {
    setSessionToken('good')
    validateSession.mockResolvedValue({ valid: true, vault_status: 'unlocked', idle_timeout_minutes: 720 })
    getAuthStatus.mockResolvedValue({ authenticated: true })
    render(<App />)
    expect(await screen.findByText('MainApp')).toBeTruthy()
  })

  it('401 from /api/auth/status → Unlock (not backend-unavailable)', async () => {
    setSessionToken('good')
    validateSession.mockResolvedValue({ valid: true, vault_status: 'unlocked' })
    getAuthStatus.mockRejectedValue(new ApiError('Session expired', { status: 401, code: 'session_expired', path: '/api/auth/status' }))
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
