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
import type { Account } from '../../api/accounts'

vi.mock('../../api/accounts', async (orig) => ({
  ...(await orig<typeof import('../../api/accounts')>()),
  getAccounts: vi.fn(),
}))
import { getAccounts } from '../../api/accounts'
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
})
