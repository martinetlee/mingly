import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import type { ChatMessages } from '../../hooks/useChatMessages'

vi.mock('../MessageList', () => ({ default: () => <div data-testid="message-list" /> }))

import ChatMessagesView from '../ChatMessagesView'

const base = {
  messages: [{ id: 1, tg_message_id: 1 }], loading: false, error: null, hasOlder: true, hasNewer: false,
  replacing: false, jumpingTo: null, jumpNotice: null, backfill: 'idle', backfillError: null, syncing: false,
  loadOlder: vi.fn(), loadNewer: vi.fn(), jumpTo: vi.fn(async () => {}), jumpToDate: vi.fn(async () => {}),
  jumpToLatest: vi.fn(), dismissJumpNotice: vi.fn(), refresh: vi.fn(), fetchOlderFromTelegram: vi.fn(),
}
const msgs = (extra: Record<string, unknown>) => ({ ...base, ...extra }) as unknown as ChatMessages

afterEach(() => cleanup())

describe('ChatMessagesView (Contacts/Tracked pane)', () => {
  it('shows why a jump failed, with Retry and Dismiss, like the Chats pane', () => {
    const m = msgs({ jumpNotice: { reason: 'unavailable', target: { kind: 'message', tgId: 42 } } })
    render(<ChatMessagesView msgs={m} chatType="private" />)
    expect(screen.getByRole('alert').textContent).toContain('Telegram is offline')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(m.jumpTo).toHaveBeenCalledWith(42)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(m.dismissJumpNotice).toHaveBeenCalled()
  })

  it('a not-found target offers no Retry', () => {
    render(<ChatMessagesView msgs={msgs({ jumpNotice: { reason: 'not_found', target: { kind: 'message', tgId: 7 } } })} chatType="private" />)
    expect(screen.getByRole('alert').textContent).toContain('isn’t available')
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })
})
