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
}))

import { initVault } from '../../api/client'
import { beginAccountAuth, continueAccountAuth, getAccounts } from '../../api/accounts'
import type { AuthStep } from '../../api/accounts'
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
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(onComplete).toHaveBeenCalledTimes(1)
  })
})
