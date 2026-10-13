/**
 * useAccounts under connector_status: a plain state change patches in place;
 * signing out (unauthorized / stopped) re-reads the list, so `configured`
 * follows the server (Connections, Mail, Calendar, Chats gate on it).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { WebSocketClient } from '../../lib/ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import type { Account, ConnectableChannel } from '../../api/accounts'

vi.mock('../../api/accounts', async (orig) => ({
  ...(await orig<typeof import('../../api/accounts')>()),
  getAccounts: vi.fn(),
}))
import { getAccounts, SIGN_IN_POLL_MS } from '../../api/accounts'
import { useAccounts } from '../useAccounts'

const signal = (over: Partial<Account> = {}): Account => ({
  account_key: 'signal:1', account_id: 1, channel: 'signal', display_name: 'Signal', label: '+1555',
  capabilities: [], configured: true, status: { state: 'ready', error: null }, ...over,
})

let ws: WebSocketClient
const wrapper = ({ children }: { children: ReactNode }) => <WebSocketProvider client={ws}>{children}</WebSocketProvider>
const status = (state: Account['status']['state']) =>
  ws.dispatch({ type: 'connector_status', channel: 'signal', account_key: 'signal:1', state, error: null })

beforeEach(() => {
  ws = new WebSocketClient({ url: () => null })
  vi.mocked(getAccounts).mockReset()
  vi.mocked(getAccounts).mockResolvedValue({ accounts: [signal()], channels: [] })
})

describe('useAccounts', () => {
  it('patches the status of a configured account in place', async () => {
    const { result } = renderHook(() => useAccounts(), { wrapper })
    await waitFor(() => expect(result.current.loaded).toBe(true))
    act(() => status('syncing'))
    expect(result.current.accounts[0].status.state).toBe('syncing')
    expect(getAccounts).toHaveBeenCalledTimes(1)
  })

  it('signing out re-reads the list: configured follows the server', async () => {
    const { result } = renderHook(() => useAccounts(), { wrapper })
    await waitFor(() => expect(result.current.loaded).toBe(true))
    vi.mocked(getAccounts).mockResolvedValue({ accounts: [signal({ configured: false, status: { state: 'unauthorized', error: null } })], channels: [] })
    act(() => status('unauthorized'))
    expect(result.current.accounts[0].status.state).toBe('unauthorized') // at once
    await waitFor(() => expect(result.current.accounts[0].configured).toBe(false))
    expect(getAccounts).toHaveBeenCalledTimes(2)
    // Unconfigured now: signing back in re-reads it again
    vi.mocked(getAccounts).mockResolvedValue({ accounts: [signal()], channels: [] })
    act(() => status('ready'))
    await waitFor(() => expect(result.current.accounts[0].configured).toBe(true))
  })

  it('a repeated unauthorized event does not re-read again', async () => {
    vi.mocked(getAccounts).mockResolvedValue({ accounts: [signal({ status: { state: 'unauthorized', error: 'revoked' } })], channels: [] })
    const { result } = renderHook(() => useAccounts(), { wrapper })
    await waitFor(() => expect(result.current.loaded).toBe(true))
    act(() => status('unauthorized'))
    expect(getAccounts).toHaveBeenCalledTimes(1)
  })

  it('re-reads while a channel setup runs (no event reports its progress), and works without a WS provider', async () => {
    vi.useFakeTimers()
    try {
      const setup = (state: string): ConnectableChannel[] => [
        { channel: 'signal', display_name: 'Signal', multi_account: true, setup: { state, progress: 0.5, message: null } },
      ]
      vi.mocked(getAccounts).mockResolvedValue({ accounts: [], channels: setup('running') })
      const { result } = renderHook(() => useAccounts()) // onboarding: outside <WebSocketProvider>
      await act(async () => { await Promise.resolve() })
      expect(result.current.loaded).toBe(true)
      await act(async () => { await vi.advanceTimersByTimeAsync(SIGN_IN_POLL_MS) })
      expect(getAccounts).toHaveBeenCalledTimes(2)

      vi.mocked(getAccounts).mockResolvedValue({ accounts: [], channels: setup('ready') })
      await act(async () => { await vi.advanceTimersByTimeAsync(SIGN_IN_POLL_MS) })
      expect(result.current.channels[0].setup?.state).toBe('ready')
      await act(async () => { await vi.advanceTimersByTimeAsync(SIGN_IN_POLL_MS * 3) })
      expect(getAccounts).toHaveBeenCalledTimes(3) // stopped once the setup ended
    } finally {
      vi.useRealTimers()
    }
  })
})
