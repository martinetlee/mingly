import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { msg } from '../../test/fixtures'
import type { Message } from '../../api/types'

vi.mock('../../api/email', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/email')>()),
  useRemoteImageSenders: vi.fn(() => new Set<string>()),
  setRemoteImagesAllowed: vi.fn(() => Promise.resolve()),
}))

import { setRemoteImagesAllowed, useRemoteImageSenders, parseAddressList } from '../../api/email'
import MessageBubble from '../MessageBubble'
import { FeaturesProvider } from '../../context/FeaturesContext'
import { emailSrcDoc, hasBlockedImages } from '../EmailMessageView'
import { replyAllRecipients, replyRecipients, replySubject } from '../../features/mail/recipients'

afterEach(() => {
  cleanup()
  vi.mocked(setRemoteImagesAllowed).mockClear()
  vi.mocked(useRemoteImageSenders).mockImplementation(() => new Set<string>())
})

function email(overrides: Partial<Message> = {}): Message {
  return msg(7, {
    subject: 'Quarterly plan',
    text: 'Hello team',
    text_html: '<p>Hello <b>team</b></p><img data-src="https://track.example/p.gif">',
    sender: { identity_id: 11, person_id: 21, name: 'Ann Lee', avatar_url: null },
    participants: [
      { identity_id: 11, person_id: 21, role: 'from', display_name: 'Ann Lee', external_id: 'ann@example.com' },
      { identity_id: 12, person_id: 22, role: 'to', display_name: null, external_id: 'me@example.com' },
      { identity_id: 13, person_id: 23, role: 'cc', display_name: 'Bob', external_id: 'bob@example.com' },
    ],
    attachments: [{ id: 5, kind: 'document', filename: 'plan.pdf', mime_type: 'application/pdf', size: 2048, url: null }],
    ...overrides,
  })
}

function renderEmail(message: Message, kind: 'email_thread' | 'direct' = 'email_thread') {
  return render(
    <MemoryRouter>
      <MessageBubble message={message} showSender conversationKind={kind} onReply={() => {}} />
    </MemoryRouter>,
  )
}

describe('EmailMessageView', () => {
  it('offers "Report scam" (Shield on, handler given) like a chat bubble, and only then', () => {
    const onReportScam = vi.fn()
    const m = email()
    const { unmount } = render(
      <FeaturesProvider value={{ shield: true }}>
        <MemoryRouter>
          <MessageBubble message={m} showSender conversationKind="email_thread" onReportScam={onReportScam} />
        </MemoryRouter>
      </FeaturesProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Report scam' }))
    expect(onReportScam).toHaveBeenCalledWith(m)
    unmount()
    // Shield off: no report action even with a handler
    render(<MemoryRouter><MessageBubble message={m} showSender conversationKind="email_thread" onReportScam={onReportScam} /></MemoryRouter>)
    expect(screen.queryByRole('button', { name: 'Report scam' })).toBeNull()
  })

  it('its hover actions are named buttons that also show on keyboard focus (not hover-only)', () => {
    renderEmail(email())
    const reply = screen.getByRole('button', { name: 'Reply' })
    expect(screen.getByRole('button', { name: 'Copy text' })).toBeTruthy()
    expect(reply.parentElement!.className).toContain('group-focus-within/email:opacity-100')
  })

  it('renders an email-thread message as an email: header, recipients, sandboxed body, attachments', () => {
    const { container } = renderEmail(email())
    expect(screen.getByTestId('email-message')).toBeTruthy()
    expect(screen.getByText('Ann Lee')).toBeTruthy()
    expect(screen.getByText('ann@example.com')).toBeTruthy()
    expect(screen.getByText('Quarterly plan')).toBeTruthy()
    expect(screen.getByText('Bob')).toBeTruthy() // cc
    const frame = container.querySelector('iframe')!
    expect(frame.getAttribute('sandbox')).toBe('allow-same-origin') // never allow-scripts
    const doc = frame.getAttribute('srcdoc')!
    expect(doc).toContain("default-src 'none'")
    expect(doc).toContain('img-src data:;')
    expect(doc).toContain('data-src="https://track.example/p.gif"')
    expect(screen.getByText('plan.pdf')).toBeTruthy()
    expect(screen.getByTestId('remote-images-banner')).toBeTruthy()
  })

  it('a message with a subject renders as an email even without the conversation kind', () => {
    renderEmail(email(), 'direct')
    expect(screen.getByTestId('email-message')).toBeTruthy()
  })

  it('a chat message stays a bubble', () => {
    renderEmail(msg(8, { text: 'hi' }), 'direct')
    expect(screen.queryByTestId('email-message')).toBeNull()
  })

  it('"Load images" loads them once; "Always load" saves the sender', async () => {
    const { container } = renderEmail(email())
    fireEvent.click(screen.getByText('Load images'))
    const doc = container.querySelector('iframe')!.getAttribute('srcdoc')!
    expect(doc).toContain('src="https://track.example/p.gif"')
    expect(doc).toContain('img-src https: http: data:')
    cleanup()
    renderEmail(email())
    fireEvent.click(screen.getByText('Always load from ann@example.com'))
    await waitFor(() => expect(setRemoteImagesAllowed).toHaveBeenCalledWith('ann@example.com', true))
  })

  it('images load automatically for an allowed sender', () => {
    vi.mocked(useRemoteImageSenders).mockImplementation(() => new Set(['ann@example.com']))
    const { container } = renderEmail(email())
    expect(screen.queryByTestId('remote-images-banner')).toBeNull()
    expect(container.querySelector('iframe')!.getAttribute('srcdoc')).toContain('src="https://track.example/p.gif"')
  })

  it('plain-text email falls back to linkified text', () => {
    const { container } = renderEmail(email({ text_html: null, text: 'See https://example.com' }))
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.querySelector('a[href="https://example.com"]')).toBeTruthy()
  })
})

describe('email helpers', () => {
  it('srcdoc keeps blocked images blocked and CSP strict', () => {
    expect(hasBlockedImages('<img data-src="https://x/y">')).toBe(true)
    expect(hasBlockedImages('<img src="data:image/png;base64,AA">')).toBe(false)
    expect(emailSrcDoc('<img data-src="https://x/y">', false)).not.toMatch(/<img src="https/)
  })

  it('parses address lists', () => {
    expect(parseAddressList('A@x.com, b@y.org; <c@z.io>  a@x.com')).toEqual(['a@x.com', 'b@y.org', 'c@z.io'])
  })

  it('reply recipients mirror the backend: sender, Reply-To, or your own recipients', () => {
    const m = email()
    expect(replyRecipients(m)).toEqual(['ann@example.com'])
    expect(replyRecipients({ ...m, meta: { reply_to: 'Team <team@example.com>' } })).toEqual(['team@example.com'])
    expect(replyRecipients({ ...m, is_outgoing: true })).toEqual(['me@example.com'])
  })

  it('reply all adds everyone else, never yourself', () => {
    expect(replyAllRecipients(email(), 'me@example.com')).toEqual({ to: ['ann@example.com'], cc: ['bob@example.com'] })
    expect(replySubject('Plans')).toBe('Re: Plans')
    expect(replySubject('RE: Plans')).toBe('RE: Plans')
  })
})
