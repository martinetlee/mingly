/**
 * MailComposer retries: an unchanged retry after a lost answer reuses the
 * nonce (the server may have sent it); an edited email or a retry after an
 * answered error gets a new one (never the cached result of another email).
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { ApiError } from '../../../lib/http'

vi.mock('../../../api/email', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../api/email')>()),
  sendEmail: vi.fn(),
}))
import { sendEmail } from '../../../api/email'
import MailComposer from '../MailComposer'

const lost = () => new ApiError('Network error', { status: 0, code: 'network', path: '/api/compose/send' })
const nonceOf = (call: number) => (vi.mocked(sendEmail).mock.calls[call][3] as { clientNonce: string }).clientNonce

function setup() {
  const onSent = vi.fn()
  render(
    <MailComposer
      mode={{ kind: 'reply', conversationId: 42, target: null }}
      initial={{ to: ['ann@example.com'], cc: [], subject: 'Re: Plan' }}
      onSent={onSent}
      onCancel={() => {}}
    />,
  )
  fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Hello' } })
  return onSent
}
const send = () => fireEvent.click(screen.getByText('Send'))

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('MailComposer nonce', () => {
  it('reuses the nonce for an unchanged retry after a lost answer', async () => {
    setup()
    vi.mocked(sendEmail).mockRejectedValueOnce(lost())
    send()
    await screen.findByRole('alert')
    vi.mocked(sendEmail).mockResolvedValueOnce({ success: true, message_id: 1 })
    send()
    await waitFor(() => expect(sendEmail).toHaveBeenCalledTimes(2))
    expect(nonceOf(1)).toBe(nonceOf(0))
  })

  it('uses a new nonce once the email was edited', async () => {
    setup()
    vi.mocked(sendEmail).mockRejectedValueOnce(lost())
    send()
    await screen.findByRole('alert')
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Hello, fixed' } })
    vi.mocked(sendEmail).mockResolvedValueOnce({ success: true, message_id: 1 })
    send()
    await waitFor(() => expect(sendEmail).toHaveBeenCalledTimes(2))
    expect(nonceOf(1)).not.toBe(nonceOf(0))
  })

  it('uses a new nonce after an answered error (nothing was sent)', async () => {
    setup()
    vi.mocked(sendEmail).mockRejectedValueOnce(new ApiError('Bad recipient', { status: 400, code: 'http', path: '/api/compose/send' }))
    send()
    await screen.findByRole('alert')
    vi.mocked(sendEmail).mockResolvedValueOnce({ success: true, message_id: 1 })
    send()
    await waitFor(() => expect(sendEmail).toHaveBeenCalledTimes(2))
    expect(nonceOf(1)).not.toBe(nonceOf(0))
  })
})
