import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import type { ChatMessages } from '../../../hooks/useChatMessages'

vi.mock('../MessageList', () => ({ default: () => <div data-testid="message-list" /> }))

import ChatMessagesView from '../ChatMessagesView'
import { makeConversation } from '../../../test/fixtures'

const base = {
  messages: [{ id: 1, conversation_id: 1 }], loading: false, error: null, hasOlder: true, hasNewer: false,
  replacing: false, jumpingTo: null, jumpNotice: null, backfill: 'idle', backfillError: null, syncing: false,
  loadOlder: vi.fn(), loadNewer: vi.fn(), jumpTo: vi.fn(async () => {}), jumpToExternal: vi.fn(async () => {}), jumpToDate: vi.fn(async () => {}),
  jumpToLatest: vi.fn(), dismissJumpNotice: vi.fn(), refresh: vi.fn(), fetchOlderFromChannel: vi.fn(),
}
const conv = makeConversation(1, 'Ann', { kind: 'direct' })
const msgs = (extra: Record<string, unknown>) => ({ ...base, ...extra }) as unknown as ChatMessages

afterEach(() => cleanup())

describe('ChatMessagesView (Contacts/Tracked pane)', () => {
  it('an empty chat explains itself: the channel note, else what can load its history', () => {
    const empty = msgs({ messages: [] })
    const signal = makeConversation(2, 'Crew', { channel: 'signal', kind: 'group', capabilities: ['send'], history_note: 'Signal only shares messages from linking.' })
    const { rerender } = render(<ChatMessagesView msgs={empty} conversation={signal} />)
    expect(screen.getByText('Signal only shares messages from linking.')).toBeTruthy()
    rerender(<ChatMessagesView msgs={empty} conversation={conv} />)  // Telegram: history_backfill
    expect(screen.getByText('Send a message or sync to load the history')).toBeTruthy()
    rerender(<ChatMessagesView msgs={empty} conversation={{ ...conv, capabilities: ['send'] }} />)
    expect(screen.getByText('Send a message to start the conversation')).toBeTruthy()
  })

  it('shows why a jump failed, with Retry and Dismiss, like the Chats pane', () => {
    const m = msgs({ jumpNotice: { reason: 'unavailable', target: { kind: 'message', messageId: 42 } } })
    render(<ChatMessagesView msgs={m} conversation={conv} />)
    expect(screen.getByRole('alert').textContent).toContain('channel is offline')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(m.jumpTo).toHaveBeenCalledWith(42)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(m.dismissJumpNotice).toHaveBeenCalled()
  })

  it('a not-found target offers no Retry', () => {
    render(<ChatMessagesView msgs={msgs({ jumpNotice: { reason: 'not_found', target: { kind: 'message', messageId: 7 } } })} conversation={conv} />)
    expect(screen.getByRole('alert').textContent).toContain('isn’t available')
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('retries an unstored (external) target through jumpToExternal', () => {
    const m = msgs({ jumpNotice: { reason: 'rate_limited', target: { kind: 'external', externalId: '777' } } })
    render(<ChatMessagesView msgs={m} conversation={conv} />)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(m.jumpToExternal).toHaveBeenCalledWith('777')
  })
})
