/**
 * Mail under live events: a read issued before an action can't undo it,
 * deletions and reconnects refresh the reading pane and the thread list.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, renderHook, screen, cleanup, fireEvent, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { WebSocketClient } from '../../../lib/ws'
import { WebSocketProvider } from '../../../context/WebSocketContext'
import { FeaturesProvider } from '../../../context/FeaturesContext'
import { deferred, msg } from '../../../test/fixtures'
import type { Conversation, MessageWindow } from '../../../api/types'
import type { MailThread } from '../../../api/email'

vi.mock('../../../api/email', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../api/email')>()),
  listMailThreads: vi.fn(),
  markThreadRead: vi.fn(),
  archiveThread: vi.fn(),
  useRemoteImageSenders: () => new Set<string>(),
}))
vi.mock('../../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../api/client')>()),
  getConversation: vi.fn(),
  getMessageWindow: vi.fn(),
  getReportContext: vi.fn(() => new Promise(() => {})),
  getScamPipeline: vi.fn(() => new Promise(() => {})),
}))

import { archiveThread, listMailThreads, markThreadRead } from '../../../api/email'
import { getConversation, getMessageWindow } from '../../../api/client'
import ThreadView from '../ThreadView'
import { MAIL_RELOAD_MS, useMailThreads } from '../useMailThreads'

const conv: Conversation = {
  id: 42, account_id: 3, channel: 'gmail', kind: 'email_thread', title: 'Quarterly plan', username: null, avatar_url: null,
  peer: null, is_monitored: false, is_hidden: false, is_briefing: false, is_tracked: false, is_forum: false,
  slow_mode_seconds: 0, is_pinned: false, is_archived: false, is_bot: false, is_deleted_account: false,
  history_complete: false, is_muted: false, muted_until: null, is_marked_unread: false, unread_count: 0,
  last_synced_at: null, last_activity_at: '2026-10-06T10:00:00Z', capabilities: ['send', 'reply'], labels: ['Inbox'],
}
const win = (...ids: number[]): MessageWindow => ({
  messages: ids.map((n) => msg(n, { conversation_id: 42, subject: 'Quarterly plan' })),
  has_older: false, has_newer: false, anchor_found: true, syncing: false,
})
const thread = (id: number): MailThread => ({ ...conv, id, thread: { senders: [], message_count: 1, has_attachments: false } })

let ws: WebSocketClient
function Providers({ children }: { children: ReactNode }) {
  return <MemoryRouter><WebSocketProvider client={ws}>{children}</WebSocketProvider></MemoryRouter>
}

beforeEach(() => {
  ws = new WebSocketClient({ url: () => null })
  vi.mocked(getConversation).mockResolvedValue(conv)
  vi.mocked(getMessageWindow).mockResolvedValue(win(7))
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.useRealTimers() })

describe('ThreadView', () => {
  it('a read in flight when Archive lands cannot bring the old state back; it is redone', async () => {
    const onChanged = vi.fn()
    render(<ThreadView conversationId={42} messageId={null} me={() => null} onChanged={onChanged} />, { wrapper: Providers })
    await screen.findByTestId('email-message')

    // A sync event starts a read that the server answers with the pre-archive state
    const stale = deferred<Conversation>()
    vi.mocked(getConversation).mockImplementationOnce(() => stale.promise)
    act(() => ws.dispatch({ type: 'conversations_synced', conversation_ids: [42] }))
    vi.mocked(archiveThread).mockResolvedValue({ ...conv, is_archived: true })
    vi.mocked(getConversation).mockResolvedValue({ ...conv, is_archived: true })
    fireEvent.click(within(screen.getByTestId('mail-thread')).getByText('Archive'))
    await screen.findByText('Move to Inbox')
    await act(async () => { stale.resolve(conv) })
    expect(screen.getByText('Move to Inbox')).toBeTruthy()
    expect(onChanged).toHaveBeenLastCalledWith(expect.objectContaining({ is_archived: true }))
    // The dropped read was redone after the archive
    await waitFor(() => expect(getConversation).toHaveBeenCalledTimes(3))
    expect(screen.getByText('Move to Inbox')).toBeTruthy()
  })

  it('removes deleted messages and reloads after a reconnect', async () => {
    vi.mocked(getMessageWindow).mockResolvedValue(win(7, 8))
    render(<ThreadView conversationId={42} messageId={null} me={() => null} onChanged={() => {}} />, { wrapper: Providers })
    await waitFor(() => expect(screen.getAllByTestId('email-message')).toHaveLength(2))
    act(() => ws.dispatch({ type: 'messages_deleted', conversation_id: 42, message_ids: [8] }))
    expect(screen.getAllByTestId('email-message')).toHaveLength(1)
    act(() => ws.dispatch({ type: 'messages_deleted', conversation_id: 99, message_ids: [7] }))
    expect(screen.getAllByTestId('email-message')).toHaveLength(1)

    vi.mocked(getMessageWindow).mockResolvedValue(win(7, 9))
    act(() => ws.dispatch({ type: 'resync' }))
    await waitFor(() => expect(screen.getAllByTestId('email-message')).toHaveLength(2))
  })

  it('applies scam check results for its messages', async () => {
    render(<ThreadView conversationId={42} messageId={null} me={() => null} onChanged={() => {}} />, { wrapper: Providers })
    const card = await screen.findByTestId('email-message')
    expect(card.className).not.toContain('border-red-500/50')
    const entry = { conversation_id: 42, message_id: 7, identity_id: 1, effective_is_scam: true } as never
    act(() => ws.dispatch({ type: 'scam_detected', request_id: 'r', source: 'manual', conversation_id: 99, message_ids: [7], identity_ids: [1], messages: [{ ...(entry as object), conversation_id: 99 } as never], pipeline_is_scam: true, confidence: 0.9, reason: 'x', category: null } as never))
    expect(screen.getByTestId('email-message').className).not.toContain('border-red-500/50')
    act(() => ws.dispatch({ type: 'scam_detected', request_id: 'r', source: 'manual', conversation_id: 42, message_ids: [7], identity_ids: [1], messages: [entry], pipeline_is_scam: true, confidence: 0.9, reason: 'x', category: null } as never))
    expect(screen.getByTestId('email-message').className).toContain('border-red-500/50')
  })

  it('Report scam opens the report dialog for that message (Shield on)', async () => {
    render(
      <FeaturesProvider value={{ shield: true }}>
        <ThreadView conversationId={42} messageId={null} me={() => null} onChanged={() => {}} />
      </FeaturesProvider>,
      { wrapper: Providers },
    )
    await screen.findByTestId('email-message')
    fireEvent.click(screen.getByRole('button', { name: 'Report scam' }))
    expect(await screen.findByText('Report Scam')).toBeTruthy()
  })

  it('auto-read result is not undone by a read issued before it', async () => {
    vi.mocked(getConversation).mockResolvedValueOnce({ ...conv, unread_count: 2 })
    const read = deferred<Conversation>()
    vi.mocked(markThreadRead).mockImplementation(() => read.promise)
    render(<ThreadView conversationId={42} messageId={null} me={() => null} onChanged={() => {}} />, { wrapper: Providers })
    await screen.findByText('Mark read')
    const stale = deferred<Conversation>()
    vi.mocked(getConversation).mockImplementationOnce(() => stale.promise)
    act(() => ws.dispatch({ type: 'conversations_synced', conversation_ids: [42] }))
    await act(async () => { read.resolve({ ...conv, unread_count: 0 }) })
    await act(async () => { stale.resolve({ ...conv, unread_count: 2 }) })
    expect(screen.getByText('Mark unread')).toBeTruthy()
  })
})

describe('useMailThreads', () => {
  const view = { accountId: null, folder: 'inbox' as const, label: null }
  const wrapper = ({ children }: { children: ReactNode }) => <WebSocketProvider client={ws}>{children}</WebSocketProvider>

  it('reloads after a reconnect and when a listed thread loses messages', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.mocked(listMailThreads).mockResolvedValue([thread(1), thread(2)])
    const { result } = renderHook(() => useMailThreads(view, true), { wrapper })
    await waitFor(() => expect(result.current.threads).toHaveLength(2))
    expect(listMailThreads).toHaveBeenCalledTimes(1)

    act(() => ws.dispatch({ type: 'resync' }))
    await waitFor(() => expect(listMailThreads).toHaveBeenCalledTimes(2))

    act(() => ws.dispatch({ type: 'messages_deleted', conversation_id: 77, message_ids: [1] }))
    await act(async () => { vi.advanceTimersByTime(MAIL_RELOAD_MS + 10) })
    expect(listMailThreads).toHaveBeenCalledTimes(2) // not a listed thread
    act(() => ws.dispatch({ type: 'messages_deleted', conversation_id: 2, message_ids: [5] }))
    await act(async () => { vi.advanceTimersByTime(MAIL_RELOAD_MS + 10) })
    await waitFor(() => expect(listMailThreads).toHaveBeenCalledTimes(3))
  })

  it('a reload issued before an archive cannot bring the thread back', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.mocked(listMailThreads).mockResolvedValue([thread(1), thread(2)])
    const { result } = renderHook(() => useMailThreads(view, true), { wrapper })
    await waitFor(() => expect(result.current.threads).toHaveLength(2))
    const stale = deferred<MailThread[]>()
    vi.mocked(listMailThreads).mockImplementationOnce(() => stale.promise)
    act(() => ws.dispatch({ type: 'conversations_synced', conversation_ids: [2] }))
    await act(async () => { vi.advanceTimersByTime(MAIL_RELOAD_MS + 10) })
    act(() => result.current.patch(1, () => null))
    await act(async () => { stale.resolve([thread(1), thread(2)]) })
    expect(result.current.threads.map((t) => t.id)).toEqual([2])
  })
})
