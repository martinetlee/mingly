import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { msg } from '../../test/fixtures'
import type { Conversation } from '../../api/types'
import type { MailThread } from '../../api/email'

const accountsState = { accounts: [] as unknown[], channels: [], loaded: true, error: null, refresh: async () => {} }
vi.mock('../../hooks/useAccounts', () => ({ useAccounts: () => accountsState }))
vi.mock('../../hooks/useWebSocket', () => ({ useWsEvent: () => {}, useWebSocket: () => {} }))
vi.mock('../../api/email', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/email')>()),
  listMailThreads: vi.fn(),
  getMailLabels: vi.fn(() => Promise.resolve([{ label: 'Inbox', count: 1 }, { label: 'Clients', count: 2 }])),
  markThreadRead: vi.fn(),
  archiveThread: vi.fn(),
  useRemoteImageSenders: () => new Set<string>(),
}))
vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getConversation: vi.fn(),
  getMessageWindow: vi.fn(),
}))

import { archiveThread, listMailThreads, markThreadRead } from '../../api/email'
import { getConversation, getMessageWindow } from '../../api/client'
import Mail from '../Mail'
import OpenConversation from '../OpenConversation'
import { conversationUrl, mailUrl, openConversationUrl } from '../../lib/routes'

const conv: Conversation = {
  id: 42, account_id: 3, channel: 'gmail', kind: 'email_thread', title: 'Quarterly plan', username: null, avatar_url: null,
  peer: null, is_monitored: false, is_hidden: false, is_briefing: false, is_tracked: false, is_forum: false,
  slow_mode_seconds: 0, is_pinned: false, is_archived: false, is_bot: false, is_deleted_account: false,
  history_complete: false, is_muted: false, muted_until: null, is_marked_unread: false, unread_count: 1,
  last_synced_at: null, last_activity_at: '2026-10-06T10:00:00Z',
  last_message: { text: 'Hello team', sender_name: 'Ann', date: '2026-10-06T10:00:00Z' },
  capabilities: ['send', 'reply'], labels: ['Inbox', 'Clients'],
}
const thread: MailThread = { ...conv, thread: { senders: [{ name: 'Ann', person_id: 1, is_me: false }, { name: 'me', person_id: 2, is_me: true }], message_count: 2, has_attachments: true } }
const gmail = { account_key: 'gmail:3', account_id: 3, channel: 'gmail', display_name: 'Gmail', label: 'me@example.com', capabilities: [], configured: true, status: { state: 'ready', error: null } }

function Where() {
  const loc = useLocation()
  return <div data-testid="where">{loc.pathname + loc.search}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/mail" element={<><Mail /><Where /></>} />
        <Route path="/open/:conversationId" element={<OpenConversation />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  accountsState.accounts = [gmail]
  vi.mocked(listMailThreads).mockResolvedValue([thread])
  vi.mocked(getConversation).mockResolvedValue(conv)
  vi.mocked(getMessageWindow).mockResolvedValue({
    messages: [msg(7, { conversation_id: 42, subject: 'Quarterly plan', text: 'Hello team' })],
    has_older: false, has_newer: false, anchor_found: true, syncing: false,
  })
  vi.mocked(markThreadRead).mockResolvedValue({ ...conv, unread_count: 0 })
  vi.mocked(archiveThread).mockResolvedValue({ ...conv, is_archived: true, labels: ['Clients'] })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('Mail space', () => {
  it('shows the connect notice without a Gmail account', () => {
    accountsState.accounts = []
    renderAt('/mail')
    expect(screen.getByTestId('no-accounts').textContent).toContain('No email account connected')
    expect(screen.getByText('Open Settings → Connections').getAttribute('href')).toBe('/settings?tab=connections')
  })

  it('a failed accounts request is an error with Retry, never "not connected"', () => {
    accountsState.accounts = []
    ;(accountsState as { error: unknown }).error = new Error('backend down')
    try {
      renderAt('/mail')
      expect(screen.queryByTestId('no-accounts')).toBeNull()
      expect(screen.getByTestId('load-error').textContent).toContain('backend down')
    } finally {
      (accountsState as { error: unknown }).error = null
    }
  })

  it('a failed first page shows the error with Retry', async () => {
    vi.mocked(listMailThreads).mockRejectedValueOnce(new Error('offline'))
    renderAt('/mail')
    const err = await screen.findByTestId('load-error')
    expect(err.textContent).toContain('Couldn’t load mail: offline')
    fireEvent.click(screen.getByText('Retry'))
    await screen.findByText('Ann, me')
  })

  it('warns about an account that is signed out', async () => {
    accountsState.accounts = [{ ...gmail, status: { state: 'unauthorized', error: null } }]
    renderAt('/mail')
    const warn = await screen.findByTestId('mail-account-problem')
    expect(warn.textContent).toContain('Signed out')
    expect(warn.querySelector('a')?.getAttribute('href')).toBe('/settings?tab=connections')
  })

  it('lists inbox threads with senders, and labels beyond the folders', async () => {
    renderAt('/mail')
    await screen.findByText('Ann, me')
    expect(screen.getByText('Quarterly plan')).toBeTruthy()
    expect(listMailThreads).toHaveBeenCalledWith(expect.objectContaining({ folder: 'inbox', accountId: null, label: null }), expect.any(AbortSignal))
    await screen.findByText('Clients')
    fireEvent.click(screen.getByText('Clients'))
    await waitFor(() => expect(listMailThreads).toHaveBeenLastCalledWith(expect.objectContaining({ label: 'Clients' }), expect.any(AbortSignal)))
    fireEvent.click(screen.getByText('Sent'))
    await waitFor(() => expect(listMailThreads).toHaveBeenLastCalledWith(expect.objectContaining({ folder: 'sent', label: null }), expect.any(AbortSignal)))
  })

  it('opens a thread: renders it as email, marks it read, archives it out of the inbox', async () => {
    renderAt('/mail')
    fireEvent.click(await screen.findByText('Ann, me'))
    expect(screen.getByTestId('where').textContent).toBe('/mail?conversationId=42')
    await screen.findByTestId('email-message')
    await waitFor(() => expect(markThreadRead).toHaveBeenCalledWith(42, true))
    fireEvent.click(within(screen.getByTestId('mail-thread')).getByText('Archive'))
    await waitFor(() => expect(archiveThread).toHaveBeenCalledWith(42, true))
    await waitFor(() => expect(screen.queryByTestId('mail-thread-row')).toBeNull()) // gone from the inbox
  })

  it('reply all prefills everyone but me', async () => {
    vi.mocked(getMessageWindow).mockResolvedValue({
      messages: [msg(7, {
        conversation_id: 42, subject: 'Quarterly plan',
        participants: [
          { identity_id: 1, person_id: 1, role: 'from', display_name: 'Ann', external_id: 'ann@example.com' },
          { identity_id: 2, person_id: 2, role: 'to', display_name: null, external_id: 'me@example.com' },
          { identity_id: 3, person_id: 3, role: 'cc', display_name: null, external_id: 'bob@example.com' },
        ],
      })],
      has_older: false, has_newer: false, anchor_found: true, syncing: false,
    })
    renderAt('/mail?conversationId=42')
    await screen.findByTestId('email-message')
    fireEvent.click(screen.getAllByText('Reply all')[0])
    expect((screen.getByLabelText('To') as HTMLInputElement).value).toBe('ann@example.com')
    expect((screen.getByLabelText('Cc') as HTMLInputElement).value).toBe('bob@example.com')
    expect((screen.getByLabelText('Subject') as HTMLInputElement).value).toBe('Re: Quarterly plan')
  })
})

describe('Mail keyboard and filter', () => {
  const key = (k: string, over: KeyboardEventInit = {}) => fireEvent.keyDown(window, { key: k, ...over })
  const other: MailThread = { ...thread, id: 43, title: 'Invoice', thread: { ...thread.thread, senders: [{ name: 'Bob', person_id: 3, is_me: false }] } }

  it('j / k open the next / previous thread; c composes; / focuses the filter', async () => {
    vi.mocked(listMailThreads).mockResolvedValue([thread, other])
    renderAt('/mail')
    await screen.findByText('Invoice')
    key('j')
    expect(screen.getByTestId('where').textContent).toBe('/mail?conversationId=42')
    key('j')
    expect(screen.getByTestId('where').textContent).toBe('/mail?conversationId=43')
    key('k')
    expect(screen.getByTestId('where').textContent).toBe('/mail?conversationId=42')
    key('/')
    expect(document.activeElement).toBe(screen.getByLabelText('Filter threads'))
    ;(document.activeElement as HTMLElement).blur()
    key('c')
    expect(await screen.findByTestId('mail-composer')).toBeTruthy()
  })

  it('the filter narrows the loaded threads', async () => {
    vi.mocked(listMailThreads).mockResolvedValue([thread, other])
    renderAt('/mail')
    await screen.findByText('Invoice')
    fireEvent.change(screen.getByLabelText('Filter threads'), { target: { value: 'bob' } })
    expect(screen.getAllByTestId('mail-thread-row')).toHaveLength(1)
    expect(screen.getByText('Invoice')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Filter threads'), { target: { value: 'zzz' } })
    expect(screen.getByText('No loaded thread matches')).toBeTruthy()
  })

  it('r / shift+r reply in the open thread; e archives it', async () => {
    renderAt('/mail?conversationId=42')
    await screen.findByTestId('email-message')
    key('r')
    expect(await screen.findByTestId('mail-composer')).toBeTruthy()
    ;(document.activeElement as HTMLElement)?.blur()
    key('e')
    await waitFor(() => expect(archiveThread).toHaveBeenCalledWith(42, true))
  })
})

describe('conversation links', () => {
  it('email threads open in Mail, chats in Chats', () => {
    expect(mailUrl(42, 7)).toBe('/mail?conversationId=42&messageId=7')
    expect(conversationUrl({ id: 42, kind: 'email_thread' })).toBe('/mail?conversationId=42')
    expect(conversationUrl({ id: 5, kind: 'direct' }, 9)).toBe('/chats?conversationId=5&messageId=9')
    expect(openConversationUrl(42, 7)).toBe('/open/42?messageId=7')
  })

  it('/open resolves an id-only link to the right space', async () => {
    renderAt(openConversationUrl(42, 7))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/mail?conversationId=42&messageId=7'))
    cleanup()
    vi.mocked(getConversation).mockResolvedValue({ ...conv, id: 5, kind: 'direct', channel: 'telegram' })
    renderAt(openConversationUrl(5))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/chats?conversationId=5'))
  })
})
