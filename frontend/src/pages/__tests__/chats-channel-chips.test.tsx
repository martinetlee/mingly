/**
 * Chats = messengers only: the list asks for exclude_kind=email_thread, and
 * channel chips (All + one per connected messenger, never Gmail) narrow it by
 * channel. The chosen chip is remembered; a channel that isn't connected any
 * more falls back to All. The forward picker leaves email threads out too.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, cleanup, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { VirtuosoMockContext } from 'react-virtuoso'
import type { Conversation } from '../../api/types'
import type { Account } from '../../api/accounts'
import type { ConversationListFilter } from '../../api/client'
import { WebSocketClient } from '../../lib/ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { ConfirmProvider } from '../../components/ui'
import { makeConversation } from '../chats/__tests__/fixtures'
import { __resetChatUiStore } from '../chats/chatUiStore'
import { messengerChannels } from '../chats/channelFilter'

const tgChat = makeConversation(111, 'Telegram group')
const sigChat = makeConversation(222, 'Signal friend', { channel: 'signal', kind: 'direct' })
const emailThread = makeConversation(333, 'Quarterly plan', { channel: 'gmail', kind: 'email_thread' })

type GetConversationsArgs = [number, number, string, number?, number?, AbortSignal?, ConversationListFilter?]
const getConversations = vi.fn(async (..._a: GetConversationsArgs): Promise<Conversation[]> => [tgChat, sigChat])
const searchConversationsLocal = vi.fn(async (_q: string) => ({ local: [tgChat, emailThread] }))

vi.mock('../../api/client', async (orig) => {
  const actual = await orig<typeof import('../../api/client')>()
  const resolve = (value: unknown = null) => async () => value
  return {
    ...actual,
    getConversations: (...a: unknown[]) => getConversations(...(a as GetConversationsArgs)),
    searchConversationsLocal: (q: string) => searchConversationsLocal(q),
    getTelegramFolders: resolve([]),
    getScamFlags: resolve({ flags: [], flagged_identity_ids: [], flagged_conversation_ids: [] }),
    searchTelegramDirectory: resolve([]),
    getTags: resolve([]),
  }
})

const account = (channel: string, over: Partial<Account> = {}): Account => ({
  account_key: channel, account_id: 1, channel, display_name: channel, capabilities: [], configured: true,
  status: { state: 'ready', error: null }, ...over,
})
let accounts: Account[] = []
vi.mock('../../api/accounts', async (orig) => ({
  ...(await orig<typeof import('../../api/accounts')>()),
  getAccounts: async () => ({ accounts, channels: [] }),
}))
vi.mock('../../context/WorkspaceContext', () => ({
  useWorkspaces: () => ({ workspaces: [], activeWorkspace: null, loaded: true, setActiveWorkspaceId: () => {}, refreshWorkspaces: async () => {} }),
}))
vi.mock('../../components/ChatInfoPanel', () => ({ default: () => null }))
vi.mock('react-virtuoso', () => import('../chats/__tests__/virtuosoTestShim'))

import Chats from '../Chats'
import ChatPickerDialog from '../../components/ChatPickerDialog'

function renderChats() {
  const ws = new WebSocketClient({ url: () => null })
  return render(
    <VirtuosoMockContext.Provider value={{ viewportHeight: 2000, itemHeight: 60 }}>
      <ConfirmProvider>
        <WebSocketProvider client={ws}>
          <MemoryRouter initialEntries={['/chats']}>
            <Routes><Route path="/chats" element={<Chats />} /></Routes>
          </MemoryRouter>
        </WebSocketProvider>
      </ConfirmProvider>
    </VirtuosoMockContext.Provider>,
  )
}

const lastFilter = () => getConversations.mock.calls[getConversations.mock.calls.length - 1]?.[6]
const chips = () => screen.queryByRole('group', { name: 'Filter by channel' })
const chip = (name: string) => screen.getByRole('button', { name })

beforeEach(() => {
  __resetChatUiStore()
  localStorage.clear()
  getConversations.mockClear()
  accounts = [account('telegram'), account('gmail'), account('signal')]
})
afterEach(() => cleanup())

describe('Chats channel chips', () => {
  it('lists messengers only: every request excludes email threads', async () => {
    renderChats()
    await waitFor(() => expect(getConversations).toHaveBeenCalled())
    for (const call of getConversations.mock.calls) expect(call[6]).toMatchObject({ excludeKind: 'email_thread' })
  })

  it('shows All + one chip per connected messenger (no Gmail) and filters by channel', async () => {
    renderChats()
    await waitFor(() => expect(chips()).not.toBeNull())
    const labels = Array.from(chips()!.querySelectorAll('button')).map((b) => b.textContent)
    expect(labels).toEqual(['All', 'Telegram', 'Signal'])
    expect(chip('All').getAttribute('aria-pressed')).toBe('true')
    expect(lastFilter()).toMatchObject({ excludeKind: 'email_thread', channel: null })

    fireEvent.click(chip('Signal'))
    await waitFor(() => expect(lastFilter()).toMatchObject({ excludeKind: 'email_thread', channel: 'signal' }))
    expect(chip('Signal').getAttribute('aria-pressed')).toBe('true')
    expect(localStorage.getItem('mingly_chats_channel')).toBe('signal')

    fireEvent.click(chip('All'))
    await waitFor(() => expect(lastFilter()).toMatchObject({ channel: null }))
    expect(localStorage.getItem('mingly_chats_channel')).toBeNull()
  })

  it('restores the remembered chip, and drops it when that channel is no longer connected', async () => {
    localStorage.setItem('mingly_chats_channel', 'signal')
    renderChats()
    await waitFor(() => expect(chips()).not.toBeNull())
    expect(chip('Signal').getAttribute('aria-pressed')).toBe('true')
    expect(getConversations.mock.calls.every((c) => c[6]?.channel === 'signal')).toBe(true)
    cleanup()

    accounts = [account('telegram'), account('signal', { configured: false }), account('whatsapp')]
    getConversations.mockClear()
    renderChats()
    await waitFor(() => expect(chips()).not.toBeNull())
    expect(chip('All').getAttribute('aria-pressed')).toBe('true')
    await waitFor(() => expect(lastFilter()).toMatchObject({ channel: null }))
  })

  it('hides the chips with a single messenger', async () => {
    accounts = [account('telegram'), account('gmail')]
    renderChats()
    await waitFor(() => expect(getConversations).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    expect(chips()).toBeNull()
  })

  it('works when storage is unavailable', async () => {
    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    try {
      renderChats()
      await waitFor(() => expect(chips()).not.toBeNull())
      fireEvent.click(chip('Telegram'))
      await waitFor(() => expect(lastFilter()).toMatchObject({ channel: 'telegram' }))
    } finally {
      get.mockRestore()
      set.mockRestore()
    }
  })

  it('messengerChannels: configured, non-email, once each', () => {
    expect(messengerChannels([
      { channel: 'telegram', configured: true }, { channel: 'gmail', configured: true },
      { channel: 'signal', configured: false }, { channel: 'telegram', configured: true }, { channel: 'sms', configured: true },
    ])).toEqual(['telegram', 'sms'])
  })
})

describe('forward picker', () => {
  it('requests messengers only and drops email threads from search results', async () => {
    getConversations.mockImplementationOnce(async () => [tgChat, emailThread])
    render(<ChatPickerDialog open onClose={() => {}} onSelect={() => {}} />)
    await waitFor(() => expect(screen.getByText('Telegram group')).toBeTruthy())
    expect(lastFilter()).toMatchObject({ excludeKind: 'email_thread' })
    expect(screen.queryByText('Quarterly plan')).toBeNull()

    fireEvent.change(screen.getByPlaceholderText('Search chats…'), { target: { value: 'q' } })
    await waitFor(() => expect(searchConversationsLocal).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByText('Telegram group')).toBeTruthy())
    expect(screen.queryByText('Quarterly plan')).toBeNull()
  })
})
