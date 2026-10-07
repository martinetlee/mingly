import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { lockVault, request, ApiError } from '../../api/client'
import { setSessionToken, getSessionToken, onSessionExpired } from '../../api/session'
import { LOCK_ACTION, navShortcuts } from '../nav'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

let fetchMock: ReturnType<typeof vi.fn>
let expired: ReturnType<typeof vi.fn<() => void>>
let off: () => void

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  setSessionToken('tok') // also re-arms the expiry stampede guard
  expired = vi.fn<() => void>()
  off = onSessionExpired(expired)
})
afterEach(() => {
  off()
  vi.unstubAllGlobals()
})

describe('lockVault', () => {
  it('POSTs /api/auth/lock with the token, then ends the session via the expiry path', async () => {
    fetchMock.mockResolvedValueOnce(json({ success: true }))
    await lockVault()
    const [path, init] = fetchMock.mock.calls[0]
    expect(path).toBe('/api/auth/lock')
    expect(init.method).toBe('POST')
    expect(init.headers['X-Session-Token']).toBe('tok')
    expect(expired).toHaveBeenCalledTimes(1)
    expect(getSessionToken()).toBeNull()
  })

  it('no 401 storm: in-flight requests failing after the lock fire expiry only once', async () => {
    fetchMock.mockResolvedValueOnce(json({ success: true }))
    await lockVault()
    fetchMock.mockResolvedValue(json({ detail: 'Invalid or expired session token' }, 401))
    const results = await Promise.allSettled([request('/api/chats'), request('/api/dashboard'), request('/api/workspaces')])
    expect(results.every((r) => r.status === 'rejected' && (r.reason as ApiError).code === 'session_expired')).toBe(true)
    expect(expired).toHaveBeenCalledTimes(1)
  })

  it('a 401 from the lock call (session already gone) still locks locally', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'No active session' }, 401))
    await expect(lockVault()).resolves.toBeUndefined()
    expect(expired).toHaveBeenCalledTimes(1)
  })

  it('other failures reject and keep the session', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'Internal server error' }, 500))
    await expect(lockVault()).rejects.toMatchObject({ status: 500 })
    expect(expired).not.toHaveBeenCalled()
    expect(getSessionToken()).toBe('tok')
  })

  it('shortcut does not collide with nav shortcuts', () => {
    for (const shield of [false, true]) {
      expect(navShortcuts({ shield }).map((s) => s.combo)).not.toContain(LOCK_ACTION.combo)
    }
  })
})
