import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import type { Contact, ContactsPage } from '../../api/types'
import { ConfirmProvider } from '../../components/ui/ConfirmDialog'

const api = vi.hoisted(() => ({
  getContactsPage: vi.fn(),
  getMessageWindow: vi.fn(),
}))

vi.mock('../../api/client', () => {
  const noop = vi.fn(async () => ({}))
  return {
    getContactsPage: (...a: unknown[]) => api.getContactsPage(...a),
    getMessageWindow: (...a: unknown[]) => api.getMessageWindow(...a),
    backfillChat: noop, syncContacts: noop, setContactImportantByTelegram: noop, setRelationshipStatus: noop,
    resetRelationshipStatus: noop, sendMessage: noop, reactToMessage: noop, syncChat: noop,
    getPositions: vi.fn(async () => []), resolvePeople: vi.fn(async () => ({})),
    isAbortError: (e: unknown) => e instanceof Error && e.name === 'AbortError',
  }
})
vi.mock('../../hooks/useWebSocket', () => ({ useWsEvent: () => {}, useWebSocket: () => {} }))
vi.mock('../../hooks/useKeyboardShortcuts', () => ({ useKeyboardShortcuts: () => {} }))
vi.mock('../../components/MessageList', () => ({
  default: ({ messages }: { messages: { tg_message_id: number }[] }) => <div data-testid="message-list">{messages.length} messages</div>,
}))
vi.mock('../../components/ComposeBar', () => ({ default: () => null }))
vi.mock('../chats/ChatPane', () => ({ applyReaction: (m: unknown) => m }))

import Contacts, { parseContactFilter } from '../Contacts'

const contact = (id: number, name: string, extra: Partial<Contact> = {}): Contact => ({
  id, telegram_id: id * 10, name, first_name: name, last_name: null, username: null, phone: null,
  twitter_handle: null, linkedin_url: null, notes: null, is_important: false, relationship_status: null,
  relationship_status_manual: false, last_interaction_at: null, last_outgoing_at: null, last_incoming_at: null,
  synced_at: null, positions: [], ...extra,
})
const page = (items: Contact[]): ContactsPage => ({
  items, total: items.length, limit: 100, offset: 0, status_counts: { stalled: 1 }, important_count: 7,
})

let location = ''
function LocationProbe() {
  location = useLocation().search
  return null
}
function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <ConfirmProvider>
        <LocationProbe />
        <Routes><Route path="/contacts" element={<Contacts />} /></Routes>
      </ConfirmProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getContactsPage.mockImplementation(async (p: { important?: boolean }) =>
    page(p.important ? [contact(1, 'Ann', { is_important: true })] : [contact(1, 'Ann', { is_important: true }), contact(2, 'Bob')]))
})
afterEach(() => cleanup())

describe('Contacts URL filter', () => {
  it('parses ?filter= (and legacy ?status=), defaulting to all', () => {
    expect(parseContactFilter(new URLSearchParams('filter=important'))).toBe('important')
    expect(parseContactFilter(new URLSearchParams('status=stalled'))).toBe('stalled')
    expect(parseContactFilter(new URLSearchParams('filter=bogus'))).toBe('all')
  })

  it('Home\'s /contacts?filter=important opens the Important view, filtered on the server', async () => {
    renderAt('/contacts?filter=important')
    await screen.findByText('Ann')
    expect(api.getContactsPage).toHaveBeenCalledWith(expect.objectContaining({ important: true, status: 'all', offset: 0 }), expect.anything())
    expect(screen.queryByText('Bob')).toBeNull()
    const tab = screen.getByRole('button', { name: /^Important/ })
    expect(tab.getAttribute('aria-pressed')).toBe('true')
    // Badge is the server's important_count, not a client-side count
    expect(tab.textContent).toBe('Important7')
  })

  it('a status filter from the URL is sent to the server, and tabs write the URL', async () => {
    renderAt('/contacts?filter=stalled')
    await screen.findByText('Ann')
    expect(api.getContactsPage).toHaveBeenCalledWith(expect.objectContaining({ status: 'stalled', important: false }), expect.anything())
    fireEvent.click(screen.getByRole('button', { name: /^All/ }))
    await waitFor(() => expect(location).toBe(''))
    fireEvent.click(screen.getByRole('button', { name: /^Important/ }))
    await waitFor(() => expect(location).toBe('?filter=important'))
  })

  it('the message pane uses the shared useChatMessages window engine', async () => {
    api.getMessageWindow.mockResolvedValue({
      messages: [{ id: 1, tg_message_id: 1, chat_id: 20, date: '2026-10-06T00:00:00Z', text: 'hi' }],
      has_older: true, has_newer: false, older_cursor: null, newer_cursor: null, anchor_found: true, syncing: false,
    })
    renderAt('/contacts')
    const bob = await screen.findByText('Bob')
    await act(async () => { fireEvent.click(bob) })
    expect((await screen.findByTestId('message-list')).textContent).toBe('1 messages')
    expect(api.getMessageWindow).toHaveBeenCalledWith(20, expect.objectContaining({ topicId: null }), expect.anything())
  })
})
