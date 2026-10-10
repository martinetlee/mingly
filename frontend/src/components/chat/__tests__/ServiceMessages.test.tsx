import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, within, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { VirtuosoMockContext } from 'react-virtuoso'
import type { Attachment, Message } from '../../../api/types'
import { msg, makeConversation } from '../../../test/fixtures'

vi.mock('react-virtuoso', () => import('../../../test/virtuosoTestShim'))
vi.mock('../../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../api/client')>()),
  archiveConversation: vi.fn(),
}))

import { archiveConversation } from '../../../api/client'
import MessageList, { buildItems } from '../MessageList'
import { structuredMeta } from '../MessageBubble'
import { ArchiveHistorySection } from '../ChatInfoPanel'

afterEach(() => cleanup())

const sender = (name: string) => ({ identity_id: 1, person_id: 11, name, avatar_url: null })

function renderList(messages: Message[], multiParty = true, extra: Partial<React.ComponentProps<typeof MessageList>> = {}) {
  return render(
    <MemoryRouter>
      <VirtuosoMockContext.Provider value={{ viewportHeight: 2000, itemHeight: 40 }}>
        <MessageList messages={messages} multiParty={multiParty} onReply={() => {}} onReact={() => {}} onDelete={() => {}} {...extra} />
      </VirtuosoMockContext.Provider>
    </MemoryRouter>,
  )
}

describe('buildItems with service messages', () => {
  it('never groups a service line into an album and restarts the sender run after it', () => {
    const items = buildItems([
      msg(1, { sender: sender('A') }),
      msg(2, { kind: 'service', text: 'A pinned a message', sender: sender('A'), grouped_id: 9 }),
      msg(3, { sender: sender('A'), grouped_id: 9 }),
    ], true)
    expect(items.map((i) => i.type)).toEqual(['single', 'service', 'single'])
    expect(items[1].showSender).toBe(false)
    expect(items[2].showSender).toBe(true)
  })
})

describe('MessageList rendering of new message kinds', () => {
  it('renders service messages as a centered system line without actions', () => {
    renderList([msg(1), msg(2, { kind: 'service', text: 'Alice joined the group' }), msg(3)])
    const line = screen.getByTestId('service-message')
    expect(line.textContent).toContain('Alice joined the group')
    const row = line.closest('[data-message-ids]') as HTMLElement
    expect(within(row).queryByTitle('Reply')).toBeNull()
    expect(within(row).queryByTitle('React with emoji')).toBeNull()
    expect(within(row).queryByLabelText('More actions')).toBeNull()
    // Regular bubbles keep their actions
    expect(screen.getAllByTitle('Reply')).toHaveLength(2)
  })

  it('a pin line jumps to the pinned message', () => {
    const onJump = vi.fn()
    render(
      <MemoryRouter>
        <VirtuosoMockContext.Provider value={{ viewportHeight: 2000, itemHeight: 40 }}>
          <MessageList
            multiParty
            onJumpToMessage={onJump}
            messages={[msg(5), msg(6, { kind: 'service', text: 'You pinned a message', reply_to_message_id: 5, meta: { service: { action: 'pin_message' } } })]}
          />
        </VirtuosoMockContext.Provider>
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('button', { name: /You pinned a message/ }))
    expect(onJump).toHaveBeenCalledWith(5)
  })

  it('shows the forward source header', () => {
    renderList([msg(1, { fwd_from_name: 'Crypto News', text: 'hello' })], false)
    expect(screen.getByTestId('forward-header').textContent).toContain('Forwarded from Crypto News')
    expect(screen.getByText('hello')).toBeTruthy()
  })

  it('renders polls, locations and contacts from meta instead of the plain fallback text', () => {
    renderList([
      // Shapes as stored by backend convert.media_meta (text is the plain fallback)
      msg(1, { text: 'Poll: Lunch?\n• Pizza (3)\n• Sushi (1)', meta: { poll: { question: 'Lunch?', options: [{ text: 'Pizza', voters: 3 }, { text: 'Sushi', voters: 1 }], total_voters: 4, closed: false, quiz: false } } }),
      msg(2, { text: 'Venue: Marina Bay, 10 Bayfront Ave', meta: { venue: { lat: 1.28, long: 103.85, title: 'Marina Bay', address: '10 Bayfront Ave' } } }),
      msg(3, { text: 'Contact: Bob Lee, 6512345678', meta: { contact: { first_name: 'Bob', last_name: 'Lee', phone: '6512345678', user_id: null } } }),
      msg(4, { text: 'Live location: 1.00000, 2.00000', meta: { location: { lat: 1, long: 2, live_period: 900 } } }),
    ], false)
    const poll = screen.getByTestId('meta-poll')
    expect(within(poll).getByText('Lunch?')).toBeTruthy()
    expect(within(poll).getByText('Pizza')).toBeTruthy()
    expect(within(poll).getByText('Sushi')).toBeTruthy()
    expect(within(poll).getByText('4 votes')).toBeTruthy()
    expect(screen.queryByText(/Poll: Lunch\?/)).toBeNull()

    const [geo, live] = screen.getAllByTestId('meta-geo')
    expect(within(live).getByText('Live location')).toBeTruthy()
    expect(within(geo).getByText('Marina Bay')).toBeTruthy()
    expect(within(geo).getByText('10 Bayfront Ave')).toBeTruthy()
    expect(within(geo).getByText('1.28000, 103.85000')).toBeTruthy()

    const contact = screen.getByTestId('meta-contact')
    expect(within(contact).getByText('Bob Lee')).toBeTruthy()
    expect(within(contact).getByText('+6512345678')).toBeTruthy()
  })

  it('falls back to text when meta is unrecognised', () => {
    expect(structuredMeta({ dice: { emoticon: '🎲', value: 4 } })).toBeNull()
    expect(structuredMeta({ forward: { from_name: 'X' } })).toBeNull()
    expect(structuredMeta({ poll: { question: 'Q?', options: [] } })?.kind).toBe('poll')
    renderList([msg(1, { text: '🎲 4', meta: { dice: { emoticon: '🎲', value: 4 } } })], false)
    expect(screen.getByText('🎲 4')).toBeTruthy()
  })
})

describe('MessageList v2 message shape', () => {
  const att = (id: number, kind: Attachment['kind'], filename: string | null = null): Attachment => ({
    id, kind, filename, mime_type: null, size: 1200, url: null,
  })

  it('shows an email subject above the text', () => {
    renderList([msg(1, { subject: 'Quarterly numbers', text: 'See attached' })], true, { channel: 'gmail' })
    expect(screen.getByTestId('message-subject').textContent).toBe('Quarterly numbers')
    expect(screen.getByText('See attached')).toBeTruthy()
  })

  it('renders every attachment of one message (an email can carry several)', () => {
    renderList([msg(1, { text: 'files', attachments: [att(1, 'document', 'a.pdf'), att(2, 'document', 'b.xlsx')] })], false)
    expect(screen.getByText('a.pdf')).toBeTruthy()
    expect(screen.getByText('b.xlsx')).toBeTruthy()
  })

  it('a reply quote whose target isn\'t stored jumps by the channel id', () => {
    const onJumpToExternal = vi.fn()
    renderList([msg(2, {
      text: 'answer', reply_to_message_id: null, reply_to_external_id: '900',
      reply_to_preview: { sender_name: 'Bob', text: 'question', media_type: null },
    })], false, { onJumpToExternal })
    fireEvent.click(screen.getByTitle('Go to the original message'))
    expect(onJumpToExternal).toHaveBeenCalledWith('900')
  })

  it('links Telegram mentions through /mention/telegram/… (only on Telegram)', () => {
    renderList([msg(1, { text: 'ping @alice_w here', text_html: null }), msg(2, { text: 'Bob', text_html: '<a href="tg://user?id=42">Bob</a>' })], false, { channel: 'telegram' })
    expect(document.querySelector('a[href="/mention/telegram/%40alice_w"]')).not.toBeNull()
    expect(document.querySelector('a[href="/mention/telegram/42"]')).not.toBeNull()
    cleanup()
    renderList([msg(1, { text: 'ping @alice_w here' })], false, { channel: 'gmail' })
    expect(document.querySelector('a[data-mention]')).toBeNull()
  })

  it('hides actions the conversation can\'t do (no callback, no button)', () => {
    render(
      <MemoryRouter>
        <VirtuosoMockContext.Provider value={{ viewportHeight: 2000, itemHeight: 40 }}>
          <MessageList messages={[msg(1)]} multiParty={false} />
        </VirtuosoMockContext.Provider>
      </MemoryRouter>,
    )
    expect(screen.queryByTitle('Reply')).toBeNull()
    expect(screen.queryByTitle('React with emoji')).toBeNull()
  })
})

describe('ChatInfoPanel archive action', () => {
  it('queues a full-history archive once and shows the archiving state', async () => {
    let resolve!: (v: Awaited<ReturnType<typeof archiveConversation>>) => void
    vi.mocked(archiveConversation).mockReturnValue(new Promise((r) => { resolve = r }))
    render(<ArchiveHistorySection chat={makeConversation(100, 'Group', { history_complete: false })} />)
    const btn = screen.getByRole('button', { name: /archive full history/i })
    fireEvent.click(btn)
    fireEvent.click(btn)
    expect(archiveConversation).toHaveBeenCalledTimes(1)
    expect(archiveConversation).toHaveBeenCalledWith(100)
    await act(async () => {
      resolve({ queued: true, coverage: { history_complete: false, message_count: 120, oldest_seq: 500, newest_contiguous_seq: null } })
    })
    expect(screen.getByText(/Archiving…/)).toBeTruthy()
  })

  it('shows complete when the chat history is complete', () => {
    render(<ArchiveHistorySection chat={makeConversation(100, 'Group', { history_complete: true })} />)
    expect(screen.getByText(/Full history archived/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /archive full history/i })).toBeNull()
  })
})
