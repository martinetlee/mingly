import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react'
import { ApiError } from '../../lib/http'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  hasCredentials: vi.fn(),
  sendCode: vi.fn(),
  initVault: vi.fn(),
}))

import { hasCredentials, sendCode, initVault } from '../../api/client'
import Onboarding from '../Onboarding'

beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)

describe('Onboarding', () => {
  it('Enter while a code request is in flight does not send a second one', async () => {
    vi.mocked(hasCredentials).mockResolvedValue({ has_credentials: true })
    let resolve!: (v: { phone_code_hash: string }) => void
    vi.mocked(sendCode).mockImplementation(() => new Promise((r) => { resolve = r }))
    render(<Onboarding onComplete={vi.fn()} />)

    const phone = await screen.findByPlaceholderText('+1 234 567 8900')
    fireEvent.change(phone, { target: { value: '+15551234567' } })
    fireEvent.keyDown(phone, { key: 'Enter' })
    fireEvent.keyDown(phone, { key: 'Enter' })
    fireEvent.keyDown(phone, { key: 'Enter' })
    expect(sendCode).toHaveBeenCalledTimes(1)

    await act(async () => { resolve({ phone_code_hash: 'h' }) })
    expect(await screen.findByText('Enter the code')).toBeTruthy()
  })

  it('surfaces a failed step with the server message', async () => {
    vi.mocked(initVault).mockRejectedValue(new ApiError('Vault already exists', { status: 409, code: 'http', path: '/api/auth/vault/init' }))
    render(<Onboarding needsVaultSetup onComplete={vi.fn()} />)
    fireEvent.change(screen.getByPlaceholderText('At least 6 characters'), { target: { value: 'secret123' } })
    const confirm = screen.getByPlaceholderText('Repeat password')
    fireEvent.change(confirm, { target: { value: 'secret123' } })
    fireEvent.keyDown(confirm, { key: 'Enter' })
    fireEvent.keyDown(confirm, { key: 'Enter' })
    expect(await screen.findByText('Vault already exists')).toBeTruthy()
    expect(initVault).toHaveBeenCalledTimes(1)
  })

  it('shows Retry instead of guessing a step when the setup probe fails', async () => {
    vi.mocked(hasCredentials).mockRejectedValueOnce(new ApiError('Network error', { status: 0, code: 'network', path: '/api/auth/has-credentials' }))
    vi.mocked(hasCredentials).mockResolvedValueOnce({ has_credentials: false })
    render(<Onboarding onComplete={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Connect to Telegram')).toBeTruthy()
  })
})
