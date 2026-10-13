import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { ApiError } from '../../lib/http'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  initVault: vi.fn(),
}))
vi.mock('../../api/accounts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/accounts')>()),
  getAccounts: vi.fn(),
  beginAccountAuth: vi.fn(),
  continueAccountAuth: vi.fn(),
  cancelAccountAuth: vi.fn(async () => undefined),
}))

import { initVault } from '../../api/client'
import { beginAccountAuth, cancelAccountAuth, continueAccountAuth, getAccounts } from '../../api/accounts'
import type { Account, AuthStep, ConnectableChannel } from '../../api/accounts'
import Onboarding from '../Onboarding'

const step = (over: Partial<AuthStep>): AuthStep => ({
  flow_id: 'f1', kind: 'fields', title: null, message: null, fields: [],
  qr_payload: null, auth_url: null, poll: false, ...over,
})

beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)

function setPassword() {
  fireEvent.change(screen.getByPlaceholderText('At least 6 characters'), { target: { value: 'secret123' } })
  const confirm = screen.getByPlaceholderText('Repeat password')
  fireEvent.change(confirm, { target: { value: 'secret123' } })
  fireEvent.keyDown(confirm, { key: 'Enter' })
  return confirm
}

describe('Onboarding', () => {
  it('surfaces a failed step with the server message (single request)', async () => {
    vi.mocked(initVault).mockRejectedValue(new ApiError('Vault already exists', { status: 409, code: 'http', path: '/api/auth/vault/init' }))
    render(<Onboarding onComplete={vi.fn()} />)
    const confirm = setPassword()
    fireEvent.keyDown(confirm, { key: 'Enter' })
    expect(await screen.findByText('Vault already exists')).toBeTruthy()
    expect(initVault).toHaveBeenCalledTimes(1)
  })

  it('after the password, offers the channels and can be skipped', async () => {
    vi.mocked(initVault).mockResolvedValue({ success: true, session_token: 't' })
    vi.mocked(getAccounts).mockResolvedValue({
      accounts: [],
      channels: [{ channel: 'telegram', display_name: 'Telegram', multi_account: false }],
    })
    const onComplete = vi.fn()
    render(<Onboarding onComplete={onComplete} />)
    setPassword()
    expect(await screen.findByRole('button', { name: 'Telegram' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Skip for now' }))
    expect(onComplete).toHaveBeenCalledTimes(1)
  })

  it('connects the chosen channel through the generic sign-in, then continues', async () => {
    vi.mocked(initVault).mockResolvedValue({ success: true, session_token: 't' })
    vi.mocked(getAccounts).mockResolvedValue({
      accounts: [],
      channels: [{ channel: 'telegram', display_name: 'Telegram', multi_account: false }],
    })
    vi.mocked(beginAccountAuth).mockResolvedValue(step({
      title: 'Your phone number',
      fields: [{ name: 'phone', label: 'Phone number', kind: 'phone', required: true, placeholder: '+1 555 123 4567' }],
    }))
    vi.mocked(continueAccountAuth).mockResolvedValue(step({ kind: 'done', message: 'Signed in as ann' }))
    const onComplete = vi.fn()
    render(<Onboarding onComplete={onComplete} />)
    setPassword()
    fireEvent.click(await screen.findByRole('button', { name: 'Telegram' }))
    fireEvent.change(await screen.findByLabelText('Phone number'), { target: { value: '+15551234567' } })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Signed in as ann')).toBeTruthy()
    expect(beginAccountAuth).toHaveBeenCalledWith('telegram', {})
    expect(continueAccountAuth).toHaveBeenCalledWith('telegram', 'f1', { phone: '+15551234567' })

    // Done goes back to the list (to connect more); the connected channel is marked, then Continue moves on
    vi.mocked(getAccounts).mockResolvedValue({
      accounts: [connectedAccount('telegram', 'ann')],
      channels: [{ channel: 'telegram', display_name: 'Telegram', multi_account: false }],
    })
    expect(screen.queryByRole('button', { name: 'Skip for now' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(onComplete).not.toHaveBeenCalled()
    const telegram = await screen.findByRole('button', { name: /Telegram.*ann/ })
    expect(telegram).toHaveProperty('disabled', true) // single-account channel: already connected
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(onComplete).toHaveBeenCalledTimes(1)
  })

  it('a setup running in the background shows on its channel; another one can be connected meanwhile', async () => {
    vi.mocked(initVault).mockResolvedValue({ success: true, session_token: 't' })
    const channels = (progress: number): ConnectableChannel[] => [
      { channel: 'signal', display_name: 'Signal', multi_account: true,
        setup: { state: 'running', progress, message: 'Downloading…' } },
      { channel: 'telegram', display_name: 'Telegram', multi_account: false, setup: null },
    ]
    vi.mocked(getAccounts).mockResolvedValue({ accounts: [], channels: channels(0.2) })
    vi.mocked(beginAccountAuth).mockResolvedValue(step({
      kind: 'progress', title: 'Setting up Signal support', progress: 0.2, poll: true, stop_label: 'Cancel setup',
    }))
    render(<Onboarding onComplete={vi.fn()} />)
    setPassword()
    expect(await screen.findByText('Setting up… 20%')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /Signal/ }))
    vi.mocked(getAccounts).mockResolvedValue({ accounts: [], channels: channels(0.5) })
    fireEvent.click(await screen.findByRole('button', { name: 'Connect another account' }))
    expect(cancelAccountAuth).toHaveBeenCalledWith('signal', 'f1') // left, not stopped
    expect(await screen.findByText('Setting up… 50%')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Telegram/ })).toHaveProperty('disabled', false)
  })
})

function connectedAccount(channel: string, label: string): Account {
  return {
    account_key: channel, account_id: null, channel, display_name: channel, label,
    capabilities: [], configured: true, status: { state: 'ready', error: null },
  }
}
