import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'

vi.mock('../../api/accounts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/accounts')>()),
  beginAccountAuth: vi.fn(),
  continueAccountAuth: vi.fn(),
  cancelAccountAuth: vi.fn(async () => undefined),
}))
vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  openExternalUrl: vi.fn(),
  resolve1Password: vi.fn(),
}))

import { beginAccountAuth, cancelAccountAuth, continueAccountAuth } from '../../api/accounts'
import type { AuthStep } from '../../api/accounts'
import { openExternalUrl, resolve1Password } from '../../api/client'
import AuthFlow, { POLL_MS } from '../AuthFlow'

const step = (over: Partial<AuthStep>): AuthStep => ({
  flow_id: 'f1', kind: 'fields', title: null, message: null, fields: [],
  qr_payload: null, auth_url: null, poll: false, ...over,
})

const phoneStep = step({
  title: 'Your phone number',
  fields: [{ name: 'phone', label: 'Phone number', kind: 'phone', required: true, placeholder: '+1 555 123 4567' }],
})
const codeStep = step({
  title: 'Enter the code sent via Telegram',
  fields: [{ name: 'code', label: 'Login code', kind: 'code', required: true, placeholder: null }],
})

beforeEach(() => vi.clearAllMocks())
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('AuthFlow', () => {
  it('fields: submits the form and renders the next step; a re-asked step shows its message as an error', async () => {
    vi.mocked(beginAccountAuth).mockResolvedValue(phoneStep)
    vi.mocked(continueAccountAuth)
      .mockResolvedValueOnce(codeStep)
      .mockResolvedValueOnce({ ...codeStep, message: 'Invalid code. Please try again.' })
    render(<AuthFlow accountKey="telegram" />)

    const phone = await screen.findByLabelText('Phone number')
    expect((phone as HTMLInputElement).type).toBe('tel')
    expect(screen.getByRole('button', { name: 'Continue' })).toHaveProperty('disabled', true)
    fireEvent.change(phone, { target: { value: ' +15551234567 ' } })
    fireEvent.submit(phone.closest('form')!)
    expect(await screen.findByText('Enter the code sent via Telegram')).toBeTruthy()
    expect(continueAccountAuth).toHaveBeenCalledWith('telegram', 'f1', { phone: '+15551234567' })

    const code = screen.getByLabelText('Login code') as HTMLInputElement
    expect(code.inputMode).toBe('numeric')
    fireEvent.change(code, { target: { value: '000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Invalid code')
  })

  it('done: shows the message and fires onDone once; error step offers Try again', async () => {
    const onDone = vi.fn()
    const onClose = vi.fn()
    vi.mocked(beginAccountAuth)
      .mockResolvedValueOnce(step({ kind: 'error', message: 'signal-cli not found' }))
      .mockResolvedValueOnce(phoneStep)
    vi.mocked(continueAccountAuth).mockResolvedValue(step({ kind: 'done', message: 'Signed in as ann' }))
    render(<AuthFlow accountKey="signal" onDone={onDone} onClose={onClose} doneLabel="Done" />)

    expect((await screen.findByRole('alert')).textContent).toContain('signal-cli not found')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    fireEvent.change(await screen.findByLabelText('Phone number'), { target: { value: '+1555' } })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Signed in as ann')).toBeTruthy()
    expect(onDone).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(beginAccountAuth).toHaveBeenCalledTimes(2)
  })

  it('qr: renders the payload as a QR code image', async () => {
    vi.mocked(beginAccountAuth).mockResolvedValue(step({
      kind: 'qr', title: 'Link Signal', qr_payload: 'sgnl://linkdevice?uuid=abc&pub_key=xyz',
    }))
    render(<AuthFlow accountKey="signal" />)
    const img = await screen.findByAltText('QR code to scan') as HTMLImageElement
    expect(img.src.startsWith('data:image/svg+xml')).toBe(true)
    expect(decodeURIComponent(img.src)).toContain('<svg')
  })

  it('oauth_browser: opens the consent page once, polls with {} while poll is set, then done', async () => {
    vi.useFakeTimers()
    const onDone = vi.fn()
    const waiting = step({ kind: 'oauth_browser', title: 'Sign in with Google', auth_url: 'https://accounts.google.com/o/oauth2/auth?x', poll: true })
    vi.mocked(beginAccountAuth).mockResolvedValue(waiting)
    vi.mocked(continueAccountAuth)
      .mockResolvedValueOnce({ ...waiting })
      .mockResolvedValueOnce(step({ kind: 'done', message: 'Connected ann@gmail.com' }))
    render(<AuthFlow accountKey="gmail" onDone={onDone} />)

    await act(async () => { await Promise.resolve() })
    expect(screen.getByText('Sign in with Google')).toBeTruthy()
    expect(openExternalUrl).toHaveBeenCalledTimes(1)
    expect(openExternalUrl).toHaveBeenCalledWith('https://accounts.google.com/o/oauth2/auth?x')
    expect(continueAccountAuth).not.toHaveBeenCalled()

    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS) })
    expect(continueAccountAuth).toHaveBeenCalledTimes(1)
    expect(continueAccountAuth).toHaveBeenLastCalledWith('gmail', 'f1', {})
    expect(onDone).not.toHaveBeenCalled()

    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS) })
    expect(continueAccountAuth).toHaveBeenCalledTimes(2)
    expect(screen.getByText('Connected ann@gmail.com')).toBeTruthy()
    expect(onDone).toHaveBeenCalledTimes(1)
    // Same URL across polls: the browser was opened only once; done stops polling
    expect(openExternalUrl).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS * 3) })
    expect(continueAccountAuth).toHaveBeenCalledTimes(2)
  })

  it('setup: a fields step with its own button label, then a progress bar that polls, then the QR code', async () => {
    vi.useFakeTimers()
    vi.mocked(beginAccountAuth).mockResolvedValue(step({
      title: 'Set up Signal support', message: 'About 180 MB', submit_label: 'Set up',
    }))
    const progress = step({ kind: 'progress', title: 'Setting up Signal support', message: 'Downloading…', progress: 0.25, poll: true })
    vi.mocked(continueAccountAuth)
      .mockResolvedValueOnce(progress)
      .mockResolvedValueOnce({ ...progress, progress: 0.6 })
      .mockResolvedValueOnce(step({ kind: 'qr', title: 'Link Signal', qr_payload: 'sgnl://linkdevice?uuid=a', poll: true }))
    render(<AuthFlow accountKey="signal" onClose={() => {}} />)

    await act(async () => { await Promise.resolve() })
    expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Set up' })) })
    expect(continueAccountAuth).toHaveBeenLastCalledWith('signal', 'f1', {})
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('25')
    expect(screen.queryByText('Waiting…')).toBeNull()
    expect(screen.queryByText(/Unsupported sign-in step/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy()

    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS) })
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('60')

    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS) })
    expect(screen.queryByRole('progressbar')).toBeNull()
    expect(screen.getByText('Link Signal')).toBeTruthy()
  })

  it('resolves a 1Password reference before the form can be submitted', async () => {
    vi.mocked(beginAccountAuth).mockResolvedValue(step({
      title: 'Connect to Telegram',
      fields: [{ name: 'api_hash', label: 'API Hash', kind: 'password', required: true, placeholder: null }],
    }))
    vi.mocked(resolve1Password).mockResolvedValue({ value: 'abc123' })
    render(<AuthFlow accountKey="telegram" />)
    const input = await screen.findByLabelText('API Hash') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'op://vault/tg/hash' } })
    expect(screen.getByRole('button', { name: 'Continue' })).toHaveProperty('disabled', true)
    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }))
    expect(await screen.findByText('Resolved from 1Password')).toBeTruthy()
    expect(input.value).toBe('abc123')
    expect(screen.getByRole('button', { name: 'Continue' })).toHaveProperty('disabled', false)
  })
})

describe('AuthFlow under StrictMode', () => {
  afterEach(() => cleanup())

  it('shows the first step after the dev double-mount (no stuck "Starting sign-in…")', async () => {
    const { StrictMode } = await import('react')
    vi.mocked(beginAccountAuth).mockResolvedValue(step({
      kind: 'error', title: 'signal-cli is required', message: 'Install it with Homebrew: brew install signal-cli',
    }))
    await act(async () => {
      render(<StrictMode><AuthFlow accountKey="signal" onClose={() => {}} /></StrictMode>)
    })
    expect(screen.queryByText(/Starting sign-in/)).toBeNull()
    expect(screen.getByText(/brew install signal-cli/)).toBeTruthy()
  })

  describe('abandoned flows are cancelled on the server', () => {
    const qr = step({ flow_id: 'q1', kind: 'qr', title: 'Link Signal', qr_payload: 'sgnl://x', poll: true })

    it('on unmount', async () => {
      vi.mocked(beginAccountAuth).mockResolvedValue(qr)
      vi.mocked(continueAccountAuth).mockResolvedValue(qr)
      const { unmount } = render(<AuthFlow accountKey="signal" />)
      await screen.findByText('Link Signal')
      unmount()
      expect(cancelAccountAuth).toHaveBeenCalledWith('signal', 'q1')
    })

    it('on Cancel', async () => {
      vi.mocked(beginAccountAuth).mockResolvedValue(qr)
      const onClose = vi.fn()
      render(<AuthFlow accountKey="signal" onClose={onClose} />)
      await screen.findByText('Link Signal')
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      expect(cancelAccountAuth).toHaveBeenCalledWith('signal', 'q1')
      expect(onClose).toHaveBeenCalled()
    })

    it('the begin StrictMode discards is cancelled; the shown one is kept', async () => {
      vi.mocked(beginAccountAuth)
        .mockResolvedValueOnce({ ...qr, flow_id: 'first' })
        .mockResolvedValueOnce({ ...qr, flow_id: 'second' })
      render(<StrictMode><AuthFlow accountKey="signal" /></StrictMode>)
      await screen.findByText('Link Signal')
      await waitFor(() => expect(cancelAccountAuth).toHaveBeenCalledWith('signal', 'first'))
      expect(cancelAccountAuth).not.toHaveBeenCalledWith('signal', 'second')
    })

    it('not after the flow finished', async () => {
      vi.mocked(beginAccountAuth).mockResolvedValue(step({ kind: 'done', title: 'Connected' }))
      const { unmount } = render(<AuthFlow accountKey="signal" />)
      await screen.findByText('Connected')
      unmount()
      expect(cancelAccountAuth).not.toHaveBeenCalled()
    })
  })
})

