import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { WSEvent } from '../../../api/types'

const wsHandlers = vi.hoisted(() => new Map<string, (e: WSEvent) => void>())
vi.mock('../../../hooks/useWebSocket', () => ({
  useWsEvent: (type: string, handler: (e: WSEvent) => void) => { wsHandlers.set(type, handler) },
}))
vi.mock('../../../api/accounts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../api/accounts')>()),
  getAccounts: vi.fn(),
  beginAccountAuth: vi.fn(),
  continueAccountAuth: vi.fn(),
  logoutAccount: vi.fn(),
  removeAccount: vi.fn(),
}))

import { beginAccountAuth, getAccounts, removeAccount } from '../../../api/accounts'
import type { Account, AccountsResponse } from '../../../api/accounts'
import { ConfirmProvider } from '../../../components/ui'
import NoAccountsNotice from '../../../components/channels/NoAccountsNotice'
import Connections from '../Connections'

const account = (over: Partial<Account>): Account => ({
  account_key: 'telegram:default', account_id: 1, channel: 'telegram', display_name: 'Telegram',
  capabilities: ['send'], configured: true, status: { state: 'ready', error: null }, ...over,
})

const channels: AccountsResponse['channels'] = [
  { channel: 'gmail', display_name: 'Gmail', multi_account: true },
  { channel: 'telegram', display_name: 'Telegram', multi_account: false },
]

beforeEach(() => {
  vi.clearAllMocks()
  wsHandlers.clear()
})
afterEach(cleanup)

const renderConnections = () => render(<ConfirmProvider><Connections /></ConfirmProvider>)

describe('Connections', () => {
  it('lists accounts with live state; remove only for multi-account channels, behind a confirm', async () => {
    vi.mocked(getAccounts).mockResolvedValue({
      accounts: [account({}), account({ account_key: 'gmail:2', account_id: 2, channel: 'gmail', display_name: 'Gmail', label: 'ann@gmail.com', status: { state: 'syncing', error: null } })],
      channels,
    })
    vi.mocked(removeAccount).mockResolvedValue({ success: true })
    renderConnections()

    expect(await screen.findByText('ann@gmail.com ·')).toBeTruthy()
    const states = screen.getAllByTestId('account-state').map((el) => el.textContent)
    expect(states).toEqual(['Connected', 'Syncing…'])
    // Capabilities are not shown
    expect(screen.queryByText('send')).toBeNull()
    // Only the Gmail row can be removed
    expect(screen.getAllByRole('button', { name: 'Remove' })).toHaveLength(1)
    // Telegram is connected → only "Add Gmail account" is offered
    expect(screen.getByRole('button', { name: 'Add Gmail account' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Connect Telegram' })).toBeNull()

    act(() => wsHandlers.get('connector_status')!({ type: 'connector_status', channel: 'telegram', account_key: 'telegram:default', state: 'unauthorized', error: 'Session revoked' }))
    expect(screen.getAllByTestId('account-state')[0].textContent).toBe('Signed out')
    expect(screen.getByText('Session revoked')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Remove Gmail (ann@gmail.com)?')).toBeTruthy()
    expect(removeAccount).not.toHaveBeenCalled()
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' })) })
    expect(removeAccount).toHaveBeenCalledWith('gmail:2')
    expect(getAccounts).toHaveBeenCalledTimes(3) // load, re-read after the sign-out (configured follows the server), after the removal
  })

  it('an unconfigured channel is offered for connecting and opens the generic sign-in', async () => {
    vi.mocked(getAccounts).mockResolvedValue({
      accounts: [account({ configured: false, status: { state: 'stopped', error: null } })],
      channels,
    })
    vi.mocked(beginAccountAuth).mockReturnValue(new Promise(() => {}))
    renderConnections()
    expect(await screen.findByText('No accounts connected yet. Add one below.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Connect Telegram' }))
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(beginAccountAuth).toHaveBeenCalledWith('telegram', {})
  })

  it("a channel's background setup shows on its add button", async () => {
    vi.mocked(getAccounts).mockResolvedValue({
      accounts: [],
      channels: [{ channel: 'signal', display_name: 'Signal', multi_account: true,
        setup: { state: 'running', progress: 0.42, message: 'Downloading…' } }],
    })
    renderConnections()
    const add = await screen.findByRole('button', { name: /Add Signal account/ })
    expect(within(add).getByText('Setting up… 42%')).toBeTruthy()
  })
})

describe('Connections: Telegram credentials', () => {
  it('"Change API credentials" runs the sign-in with reset_credentials; other channels have no such action', async () => {
    vi.mocked(getAccounts).mockResolvedValue({
      accounts: [
        account({ label: '@ann' }),
        account({ account_key: 'gmail:2', account_id: 2, channel: 'gmail', display_name: 'Gmail', label: 'ann@gmail.com' }),
      ],
      channels,
    })
    vi.mocked(beginAccountAuth).mockReturnValue(new Promise(() => {}))
    renderConnections()
    expect(await screen.findByText('@ann ·')).toBeTruthy()
    const buttons = screen.getAllByRole('button', { name: 'Change API credentials' })
    expect(buttons).toHaveLength(1)
    fireEvent.click(buttons[0])
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(beginAccountAuth).toHaveBeenCalledWith('telegram:default', { reset_credentials: true })
  })
})

describe('NoAccountsNotice', () => {
  it('points to Settings → Connections only while no account is configured', async () => {
    vi.mocked(getAccounts).mockResolvedValue({ accounts: [account({ configured: false })], channels })
    render(<MemoryRouter><NoAccountsNotice fallback={<p>Select a chat</p>} /></MemoryRouter>)
    const link = await screen.findByRole('link', { name: 'Open Settings → Connections' })
    expect(link.getAttribute('href')).toBe('/settings?tab=connections')
  })

  it('renders the fallback once an account exists', async () => {
    vi.mocked(getAccounts).mockResolvedValue({ accounts: [account({})], channels })
    render(<MemoryRouter><NoAccountsNotice fallback={<p>Select a chat</p>} /></MemoryRouter>)
    await act(async () => { await Promise.resolve() })
    expect(getAccounts).toHaveBeenCalled()
    expect(screen.getByText('Select a chat')).toBeTruthy()
    expect(screen.queryByText('No accounts connected')).toBeNull()
  })
})
