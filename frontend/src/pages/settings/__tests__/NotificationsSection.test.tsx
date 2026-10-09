import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react'

vi.mock('../../../hooks/useWebSocket', () => ({ useWsEvent: () => {} }))
vi.mock('../../../api/accounts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../api/accounts')>()),
  getAccounts: vi.fn(),
}))
vi.mock('../../../api/email', () => ({ setChannelNotifPref: vi.fn(async (_c: string, pref: string) => ({ success: true, pref })) }))
vi.mock('../../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../api/client')>()),
  setNotifPref: vi.fn(),
}))

import { getAccounts } from '../../../api/accounts'
import type { Account } from '../../../api/accounts'
import { setChannelNotifPref } from '../../../api/email'
import type { Prefs } from '../../../api/types'
import { NotificationsSection } from '../PreferenceSections'

const account = (channel: string): Account => ({
  account_key: `${channel}:1`, account_id: 1, channel, display_name: channel, capabilities: [],
  configured: true, status: { state: 'ready', error: null },
})

beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)

describe('NotificationsSection per-channel setting', () => {
  it('shows a Gmail selector (off by default) only with a Gmail account, and saves it per channel', async () => {
    vi.mocked(getAccounts).mockResolvedValue({ accounts: [account('telegram'), account('gmail')], channels: [] })
    render(<NotificationsSection prefs={{ notif_pref: 'all' } as Prefs} />)
    const select = await screen.findByLabelText('Gmail') as HTMLSelectElement
    expect(select.value).toBe('off')
    await act(async () => { fireEvent.change(select, { target: { value: 'all' } }) })
    expect(setChannelNotifPref).toHaveBeenCalledWith('gmail', 'all')
  })

  it('adopts the stored per-channel value; no selector without a Gmail account', async () => {
    vi.mocked(getAccounts).mockResolvedValue({ accounts: [account('gmail')], channels: [] })
    const prefs = { notif_pref: 'tracked', notif_pref_channels: { gmail: 'tracked' } } as unknown as Prefs
    render(<NotificationsSection prefs={prefs} />)
    expect(((await screen.findByLabelText('Gmail')) as HTMLSelectElement).value).toBe('tracked')
    cleanup()

    vi.mocked(getAccounts).mockResolvedValue({ accounts: [account('telegram')], channels: [] })
    render(<NotificationsSection prefs={{ notif_pref: 'all' } as Prefs} />)
    await act(async () => { await Promise.resolve() })
    expect(getAccounts).toHaveBeenCalledTimes(2)
    expect(screen.queryByLabelText('Gmail')).toBeNull()
  })
})
