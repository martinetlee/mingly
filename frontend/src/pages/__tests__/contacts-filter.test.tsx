import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import type { PeoplePage, Person } from '../../api/types'
import { makeConversation, makePerson, msg } from '../chats/__tests__/fixtures'
import { ConfirmProvider } from '../../components/ui/ConfirmDialog'

const api = vi.hoisted(() => ({
  getPeoplePage: vi.fn(),
  getMessageWindow: vi.fn(),
  startPrivateChat: vi.fn(),
  getPersonConversations: vi.fn(),
}))

vi.mock('../../api/client', () => {
  const noop = vi.fn(async () => ({}))
  return {
    getPeoplePage: (...a: unknown[]) => api.getPeoplePage(...a),
    getMessageWindow: (...a: unknown[]) => api.getMessageWindow(...a),
    startPrivateChat: (...a: unknown[]) => api.startPrivateChat(...a),
    getPersonConversations: (...a: unknown[]) => api.getPersonConversations(...a),
    backfillConversation: noop, syncPeople: noop, setPersonImportant: noop, setRelationshipStatus: noop,
    resetRelationshipStatus: noop, sendMessage: noop, reactToMessage: noop, syncConversation: noop,
    getPositions: vi.fn(async () => []), getPerson: vi.fn(async (id: number) => makePerson(id, `P${id}`)),
    isAbortError: (e: unknown) => e instanceof Error && e.name === 'AbortError',
  }
})
vi.mock('../../hooks/useWebSocket', () => ({ useWsEvent: () => {}, useWebSocket: () => {} }))
vi.mock('../../hooks/useKeyboardShortcuts', () => ({ useKeyboardShortcuts: () => {} }))
vi.mock('../../components/MessageList', () => ({
  default: ({ messages }: { messages: { id: number }[] }) => <div data-testid="message-list">{messages.length} messages</div>,
}))
vi.mock('../../components/ComposeBar', () => ({ default: () => null }))
vi.mock('../chats/ChatPane', () => ({ applyReaction: (m: unknown) => m }))

import Contacts, { parseContactFilter } from '../Contacts'

const contact = (id: number, name: string, extra: Partial<Person> = {}): Person => makePerson(id, name, extra)
const page = (items: Person[]): PeoplePage => ({
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
  api.getPeoplePage.mockImplementation(async (p: { important?: boolean }) =>
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
    expect(api.getPeoplePage).toHaveBeenCalledWith(expect.objectContaining({ important: true, status: 'all', offset: 0, contactsOnly: true }), expect.anything())
    expect(screen.queryByText('Bob')).toBeNull()
    const tab = screen.getByRole('button', { name: /^Important/ })
    expect(tab.getAttribute('aria-pressed')).toBe('true')
    // Badge is the server's important_count, not a client-side count
    expect(tab.textContent).toBe('Important7')
  })

  it('a status filter from the URL is sent to the server, and tabs write the URL', async () => {
    renderAt('/contacts?filter=stalled')
    await screen.findByText('Ann')
    expect(api.getPeoplePage).toHaveBeenCalledWith(expect.objectContaining({ status: 'stalled', important: false }), expect.anything())
    fireEvent.click(screen.getByRole('button', { name: /^All/ }))
    await waitFor(() => expect(location).toBe(''))
    fireEvent.click(screen.getByRole('button', { name: /^Important/ }))
    await waitFor(() => expect(location).toBe('?filter=important'))
  })

  it('the message pane opens the person\'s existing direct conversation on the shared useChatMessages window engine', async () => {
    api.getPersonConversations.mockResolvedValue([makeConversation(20, 'Bob', { kind: 'direct' })])
    api.getMessageWindow.mockResolvedValue({
      messages: [msg(1, { conversation_id: 20 })],
      has_older: true, has_newer: false, anchor_found: true, syncing: false,
    })
    renderAt('/contacts')
    const bob = await screen.findByText('Bob')
    await act(async () => { fireEvent.click(bob) })
    expect((await screen.findByTestId('message-list')).textContent).toBe('1 messages')
    expect(api.getPersonConversations).toHaveBeenCalledWith(2)
    expect(api.startPrivateChat).not.toHaveBeenCalled()
    expect(api.getMessageWindow).toHaveBeenCalledWith(20, expect.objectContaining({ topicId: null }), expect.anything())
  })

  it('selecting someone without a chat never creates one; "Start a Telegram chat" does', async () => {
    api.getPersonConversations.mockResolvedValue([])
    api.startPrivateChat.mockResolvedValue(makeConversation(21, 'Bob', { kind: 'direct' }))
    api.getMessageWindow.mockResolvedValue({ messages: [], has_older: false, has_newer: false, anchor_found: true, syncing: false })
    renderAt('/contacts')
    const bob = await screen.findByText('Bob')
    await act(async () => { fireEvent.click(bob) })
    expect(await screen.findByText('No chat with Bob yet')).toBeTruthy()
    expect(api.startPrivateChat).not.toHaveBeenCalled()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Start a Telegram chat' })) })
    await waitFor(() => expect(api.startPrivateChat).toHaveBeenCalledWith(2))
    await waitFor(() => expect(api.getMessageWindow).toHaveBeenCalledWith(21, expect.anything(), expect.anything()))
  })

  it('shows the person\'s messenger chat, never their email thread (email lives in Mail)', async () => {
    api.getPersonConversations.mockResolvedValue([
      makeConversation(30, 'Re: invoice', { kind: 'email_thread', channel: 'gmail' }),
      makeConversation(20, 'Bob', { kind: 'direct' }),
    ])
    api.getMessageWindow.mockResolvedValue({ messages: [msg(1, { conversation_id: 20 })], has_older: false, has_newer: false, anchor_found: true, syncing: false })
    renderAt('/contacts')
    const bob = await screen.findByText('Bob')
    await act(async () => { fireEvent.click(bob) })
    await waitFor(() => expect(api.getMessageWindow).toHaveBeenCalledWith(20, expect.anything(), expect.anything()))
    expect(api.getMessageWindow).not.toHaveBeenCalledWith(30, expect.anything(), expect.anything())
  })
})
