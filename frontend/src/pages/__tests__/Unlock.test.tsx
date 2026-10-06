import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'
import { ApiError } from '../../lib/http'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  unlockVault: vi.fn(),
  resetVault: vi.fn(),
}))

import { unlockVault, resetVault } from '../../api/client'
import Unlock, { classifyUnlockError } from '../Unlock'

const mockUnlock = vi.mocked(unlockVault)
const mockReset = vi.mocked(resetVault)

const corrupt = () => new ApiError('The vault file is damaged and cannot be opened. Reset the vault to start over.', {
  status: 409, code: 'http', path: '/api/auth/vault/unlock', serverCode: 'vault_corrupt',
})
const wrongPassword = () => new ApiError('Incorrect password', { status: 400, code: 'http', path: '/api/auth/vault/unlock' })
const network = () => new ApiError('Network error', { status: 0, code: 'network', path: '/api/auth/vault/unlock' })

function typePassword(value = 'hunter22') {
  fireEvent.change(screen.getByLabelText('Master password'), { target: { value } })
}

beforeEach(() => {
  mockUnlock.mockReset()
  mockReset.mockReset()
})
afterEach(cleanup)

describe('classifyUnlockError', () => {
  it('distinguishes corrupt vault, wrong password and an unreachable backend', () => {
    expect(classifyUnlockError(corrupt()).kind).toBe('corrupt')
    expect(classifyUnlockError(wrongPassword())).toEqual({ kind: 'wrong_password', message: 'Incorrect password' })
    expect(classifyUnlockError(network()).kind).toBe('unreachable')
    expect(classifyUnlockError(new ApiError('Internal server error', { status: 500, code: 'http', path: '/x' })).kind).toBe('unreachable')
  })
})

describe('Unlock', () => {
  it('a double Enter sends a single unlock request', async () => {
    let resolve!: (v: { success: boolean; session_token: string }) => void
    mockUnlock.mockImplementation(() => new Promise((r) => { resolve = r }))
    const onUnlock = vi.fn()
    render(<Unlock onUnlock={onUnlock} />)
    typePassword()
    const input = screen.getByLabelText('Master password')
    fireEvent.submit(input.closest('form')!)
    fireEvent.submit(input.closest('form')!)
    fireEvent.click(screen.getByRole('button', { name: /unlocking/i }))
    expect(mockUnlock).toHaveBeenCalledTimes(1)
    await act(async () => { resolve({ success: true, session_token: 't' }) })
    expect(onUnlock).toHaveBeenCalledTimes(1)
  })

  it('wrong password shows "Incorrect password" and no reset prompt', async () => {
    mockUnlock.mockRejectedValue(wrongPassword())
    render(<Unlock onUnlock={vi.fn()} />)
    typePassword()
    fireEvent.click(screen.getByRole('button', { name: 'Unlock' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Incorrect password')
    expect(screen.queryByRole('button', { name: /reset vault/i })).toBeNull()
  })

  it('backend down is reported as unreachable, not as a wrong password', async () => {
    mockUnlock.mockRejectedValue(network())
    render(<Unlock onUnlock={vi.fn()} />)
    typePassword()
    fireEvent.click(screen.getByRole('button', { name: 'Unlock' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/can't reach the mingly backend/i)
    expect(alert.textContent).not.toContain('Incorrect password')
  })

  it('corrupt vault shows the server message and offers Reset, which needs RESET typed', async () => {
    mockUnlock.mockRejectedValue(corrupt())
    mockReset.mockResolvedValue({ success: true })
    const onReset = vi.fn()
    render(<Unlock onUnlock={vi.fn()} onReset={onReset} />)
    typePassword()
    fireEvent.click(screen.getByRole('button', { name: 'Unlock' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/vault file is damaged/i)
    expect(alert.textContent).not.toContain('Incorrect password')

    fireEvent.click(screen.getByRole('button', { name: /reset vault…/i }))
    const confirmBtn = screen.getByRole('button', { name: 'Reset vault' }) as HTMLButtonElement
    expect(confirmBtn.disabled).toBe(true)

    const typed = screen.getByLabelText('Type RESET to confirm')
    fireEvent.change(typed, { target: { value: 'reset' } })
    expect(confirmBtn.disabled).toBe(true)
    fireEvent.change(typed, { target: { value: 'RESET' } })
    expect(confirmBtn.disabled).toBe(false)

    fireEvent.click(confirmBtn)
    await waitFor(() => expect(onReset).toHaveBeenCalledTimes(1))
    expect(mockReset).toHaveBeenCalledTimes(1)
    expect(mockReset).toHaveBeenCalledWith('RESET')
  })

  it('a failed reset keeps the dialog open and shows the error', async () => {
    mockReset.mockRejectedValue(new ApiError('Type "RESET" to confirm', { status: 400, code: 'http', path: '/api/auth/vault/reset' }))
    const onReset = vi.fn()
    render(<Unlock onUnlock={vi.fn()} onReset={onReset} />)
    fireEvent.click(screen.getByRole('button', { name: /forgot password/i }))
    fireEvent.change(screen.getByLabelText('Type RESET to confirm'), { target: { value: 'RESET' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reset vault' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Reset failed: Type "RESET" to confirm')
    expect(onReset).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Reset vault' })).toBeTruthy()
  })

  it('does not claim that all data is encrypted at rest', () => {
    render(<Unlock onUnlock={vi.fn()} />)
    expect(screen.queryByText(/your data is encrypted at rest/i)).toBeNull()
    expect(screen.getByText(/stored unencrypted in a local database/i)).toBeTruthy()
  })
})
