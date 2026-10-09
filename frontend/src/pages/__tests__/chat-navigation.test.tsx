/**
 * Chats page integration: every entry point goes through selectChat, and
 * per-chat state / dialog targets can't cross chats.
 *
 * Regressions guarded:
 * - ?conversationId= params consumed once (list reloads can't re-select / wipe the pane)
 * - in-page navigation (palette) switches chats; no "Select a chat" flash
 * - a conversation beyond page 1 opens by id even if the list reloads meanwhile
 * - capability gating: actions a conversation can't do aren't offered
 * - delete dialog acts on the chat/message captured when it opened
 * - "Delete for everyone" starts unticked on every open
 * - reply / edit state stays with its chat
 * - a send that resolves after a chat switch never lands in the new chat
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, act, fireEvent, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useNavigate, useLocation } from 'react-router-dom'
import { VirtuosoMockContext } from 'react-virtuoso'
import type { Conversation, Message, MessageWindow } from '../../api/types'
import { WebSocketClient } from '../../lib/ws'
import { ApiError } from '../../lib/http'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { ConfirmProvider } from '../../components/ui'
import { deferred, makeConversation, msg, page } from '../chats/__tests__/fixtures'
import { __resetChatUiStore } from '../chats/chatUiStore'

const chatAztec = makeConversation(111, 'Aztec <> ETHTaipei')
const chatPolygon = makeConversation(222, 'Polygon <> ETHTaipei 2024')
const readOnly = makeConversation(333, 'Announcements', { kind: 'channel', capabilities: [] })

const windows: Record<number, Message[]> = {
  [chatAztec.id]: [msg(1, { conversation_id: chatAztec.id, text: 'aztec hello' }), msg(2, { conversation_id: chatAztec.id, text: 'mine', is_outgoing: true })],
  [chatPolygon.id]: [msg(1, { conversation_id: chatPolygon.id, text: 'polygon hi' })],
  [readOnly.id]: [msg(5, { conversation_id: readOnly.id, text: 'news post' })],
}

const getConversations = vi.fn(async (): Promise<Conversation[]> => [chatAztec, chatPolygon])
const getMessageWindow = vi.fn(async (conversationId: number, _params?: object): Promise<MessageWindow> => ({
  ...page([]), messages: windows[conversationId] ?? [],
}))
const getConversation = vi.fn(async (_id: number): Promise<Conversation> => { throw new Error('not found') })
const deleteMessages = vi.fn(async () => ({ success: true }))
const sendMessage = vi.fn(async (..._a: unknown[]): Promise<{ success: boolean; message_id: number; message?: Message }> => ({ success: true, message_id: 0 }))
const markConversationRead = vi.fn(async () => ({ success: true }))
const searchMessagesGlobal = vi.fn()
const sendFiles = vi.fn()

vi.mock('../../api/client', async (orig) => {
  const actual = await orig<typeof import('../../api/client')>()
  const resolve = (value: unknown = null) => async () => value
  return {
    ...actual,
    getConversations: (...a: unknown[]) => getConversations(...(a as [])),
    getMessageWindow: (...a: unknown[]) => getMessageWindow(...(a as [number, object])),
    getConversation: (...a: unknown[]) => getConversation(...(a as [number])),
    deleteMessages: (...a: unknown[]) => deleteMessages(...(a as [])),
    sendMessage: (...a: unknown[]) => sendMessage(...a),
    markConversationRead: (...a: unknown[]) => markConversationRead(...(a as [])),
    searchMessagesGlobal: (...a: unknown[]) => searchMessagesGlobal(...a),
    sendFiles: (...a: unknown[]) => sendFiles(...a),
    getTelegramFolders: resolve([]),
    getScamFlags: resolve({ flags: [], flagged_identity_ids: [], flagged_conversation_ids: [] }),
    getPinnedMessage: resolve(null),
    getPeerPresence: resolve(null),
    getPerson: resolve({ is_important: false }),
    searchTelegramDirectory: resolve([]),
    sendTypingAction: resolve(null),
    backfillConversation: resolve({ added: 0, exhausted: true, status: 'unchanged' }),
    setMarkedUnread: resolve({ is_marked_unread: false }),
  }
})

vi.mock('../../context/WorkspaceContext', () => ({
  useWorkspaces: () => ({ workspaces: [], activeWorkspace: null, loaded: true, setActiveWorkspaceId: () => {}, refreshWorkspaces: async () => {} }),
}))
vi.mock('../../api/session', async (orig) => ({
  ...(await orig<typeof import('../../api/session')>()),
  authenticatedMediaUrl: (p: string) => p,
}))
vi.mock('../../components/ChatInfoPanel', () => ({ default: () => null }))
vi.mock('react-virtuoso', () => import('../chats/__tests__/virtuosoTestShim'))

import Chats from '../Chats'

const routerControls: { navigate: ReturnType<typeof useNavigate> | null; search: string } = { navigate: null, search: '' }
function RouterProbe() {
  routerControls.navigate = useNavigate()
  routerControls.search = useLocation().search
  return null
}

let ws: WebSocketClient
function renderChats(initialEntry: string) {
  ws = new WebSocketClient({ url: () => null })
  return render(
    // Real Virtuoso lists with fixed sizes (jsdom has no layout)
    <VirtuosoMockContext.Provider value={{ viewportHeight: 2000, itemHeight: 60 }}>
    <ConfirmProvider>
      <WebSocketProvider client={ws}>
        <MemoryRouter initialEntries={[initialEntry]}>
          <RouterProbe />
          <Routes>
            <Route path="/chats" element={<Chats />} />
          </Routes>
        </MemoryRouter>
      </WebSocketProvider>
    </ConfirmProvider>
    </VirtuosoMockContext.Provider>,
  )
}

const windowCallsFor = (conversationId: number) => getMessageWindow.mock.calls.filter((c) => c[0] === conversationId)
const header = () => document.querySelector('h2')?.textContent ?? undefined
const go = (url: string) => act(() => { routerControls.navigate!(url) })
const clickRow = (chat: Conversation) => act(() => {
  fireEvent.click(document.querySelector(`[data-chat-id="${chat.id}"] [data-chat-row]`)!)
})
const messagePane = () => screen.getByTestId('message-scroll')
const compose = () => document.querySelector('textarea') as HTMLTextAreaElement

beforeEach(() => {
  __resetChatUiStore()
  getConversations.mockReset().mockImplementation(async () => [chatAztec, chatPolygon])
  getMessageWindow.mockClear()
  getConversation.mockReset().mockImplementation(async () => { throw new Error('not found') })
  deleteMessages.mockClear()
  sendMessage.mockReset().mockImplementation(async () => ({ success: true, message_id: 0 }))
  markConversationRead.mockClear()
})

describe('Chats entry points', () => {
  it('selects the conversation from /chats?conversationId=… and consumes the param', async () => {
    renderChats(`/chats?conversationId=${chatPolygon.id}`)
    await waitFor(() => expect(header()).toBe(chatPolygon.title))
    expect(windowCallsFor(chatPolygon.id).length).toBeGreaterThan(0)
    expect(routerControls.search).toBe('')
  })

  it('opens centred on messageId (anchor window)', async () => {
    renderChats(`/chats?conversationId=${chatPolygon.id}&messageId=1`)
    await waitFor(() => expect(windowCallsFor(chatPolygon.id)[0]?.[1]).toMatchObject({ anchor: 1 }))
  })

  it('switches chats when navigating in-page (palette click) and again on a second nav', async () => {
    renderChats('/chats')
    await waitFor(() => expect(getConversations).toHaveBeenCalled())
    go(`/chats?conversationId=${chatAztec.id}`)
    await waitFor(() => expect(header()).toBe(chatAztec.title))
    go(`/chats?conversationId=${chatPolygon.id}`)
    await waitFor(() => expect(header()).toBe(chatPolygon.title))
  })

  it('opens a conversation beyond page 1 by id, even if the list reloads meanwhile, without flashing the empty state', async () => {
    const offPage = makeConversation(999, 'Beyond Page One')
    const ensured = deferred<Conversation>()
    getConversation.mockImplementation(() => ensured.promise)
    const { container } = renderChats(`/chats?conversationId=${offPage.id}`)
    let flashed = false
    const obs = new MutationObserver(() => { if (container.textContent?.includes('Select a chat to start')) flashed = true })
    obs.observe(container, { subtree: true, childList: true, characterData: true })

    await waitFor(() => expect(getConversation).toHaveBeenCalledWith(offPage.id))
    await act(async () => { fireEvent.click(screen.getByText('Unread')) }) // list reload mid-flight
    await act(async () => { ensured.resolve(offPage) })
    await waitFor(() => expect(header()).toBe(offPage.title))
    obs.disconnect()
    expect(flashed).toBe(false)
  })

  it('a user click while a nav is resolving wins over the late nav result', async () => {
    const ensured = deferred<Conversation>()
    getConversation.mockImplementation(() => ensured.promise)
    renderChats('/chats?conversationId=999')
    await waitFor(() => expect(getConversation).toHaveBeenCalled())
    clickRow(chatAztec)
    await act(async () => { ensured.resolve(makeConversation(999, 'Late')) })
    expect(header()).toBe(chatAztec.title)
  })

  it('j/k navigate the list; selecting marks the chat read', async () => {
    getConversations.mockImplementation(async () => [chatAztec, { ...chatPolygon, unread_count: 3 }])
    renderChats('/chats')
    await waitFor(() => expect(document.querySelector(`[data-chat-id="${chatAztec.id}"]`)).not.toBeNull())
    // Same activity time → alphabetical: Aztec, then Polygon
    act(() => { fireEvent.keyDown(window, { key: 'j' }) })
    await waitFor(() => expect(header()).toBe(chatAztec.title))
    // Keyboard selection keeps focus in the list (the compose box doesn't steal the next j)
    expect(document.activeElement?.closest('[data-chat-list]')).not.toBeNull()
    act(() => { fireEvent.keyDown(document.activeElement!, { key: 'j' }) })
    await waitFor(() => expect(header()).toBe(chatPolygon.title))
    expect(markConversationRead).toHaveBeenCalledWith(chatPolygon.id)
    act(() => { fireEvent.keyDown(document.activeElement!, { key: 'k' }) })
    await waitFor(() => expect(header()).toBe(chatAztec.title))
  })
})

describe('Chats message search', () => {
  it('a new query aborts the running one and only the latest results show', async () => {
    const first = deferred<unknown>()
    searchMessagesGlobal.mockReset()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(async () => [{ conversation_id: chatAztec.id, conversation_title: 'Aztec hit', conversation_kind: 'group', messages: [{ external_id: '1', text: 'beta match', sender_name: null, date: null }] }])
    renderChats('/chats')
    const input = await screen.findByPlaceholderText('Search chats…')
    fireEvent.change(input, { target: { value: 'alpha' } })
    fireEvent.click(await screen.findByText('Search Telegram messages'))
    await waitFor(() => expect(searchMessagesGlobal).toHaveBeenCalledTimes(1))
    fireEvent.change(input, { target: { value: 'beta' } })
    fireEvent.click(await screen.findByText('Search Telegram messages'))
    await waitFor(() => expect(screen.getByText('beta match')).toBeTruthy())
    expect((searchMessagesGlobal.mock.calls[0][1] as AbortSignal).aborted).toBe(true)
    // The stale 'alpha' response can't replace the newer results
    await act(async () => { first.resolve([{ conversation_id: 1, conversation_title: 'stale', conversation_kind: 'group', messages: [{ external_id: '2', text: 'alpha match', sender_name: null, date: null }] }]) })
    expect(screen.queryByText('alpha match')).toBeNull()
    expect(screen.getByText('beta match')).toBeTruthy()
  })

  it('a message search hit opens the chat centred on the match (by its channel id: it may not be stored)', async () => {
    searchMessagesGlobal.mockReset().mockResolvedValue([{ conversation_id: chatPolygon.id, conversation_title: 'Polygon hit', conversation_kind: 'group', messages: [{ external_id: '1', text: 'hit text', sender_name: null, date: null }] }])
    renderChats('/chats')
    const input = await screen.findByPlaceholderText('Search chats…')
    fireEvent.change(input, { target: { value: 'hit' } })
    fireEvent.click(await screen.findByText('Search Telegram messages'))
    fireEvent.click(await screen.findByText('Polygon hit'))
    await waitFor(() => expect(header()).toBe(chatPolygon.title))
    expect(windowCallsFor(chatPolygon.id)[0][1]).toMatchObject({ aroundExternalId: '1' })
  })
})

describe('Chats list virtualization', () => {
  it('mounts only visible chat rows', async () => {
    const many = Array.from({ length: 300 }, (_, i) => makeConversation(200000 + i, `Chat ${i}`))
    getConversations.mockImplementation(async () => many)
    renderChats('/chats')
    await waitFor(() => expect(document.querySelectorAll('[data-chat-id]').length).toBeGreaterThan(0))
    expect(document.querySelectorAll('[data-chat-id]').length).toBeLessThan(80)
  })
})

describe('Chats: per-chat state and captured targets', () => {
  it('delete acts on the chat/message captured when the dialog opened', async () => {
    renderChats(`/chats?conversationId=${chatAztec.id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    const bubble = within(messagePane()).getByText('aztec hello').closest('[data-message-ids]') as HTMLElement
    fireEvent.click(within(bubble).getByLabelText('More actions'))
    fireEvent.click(within(bubble).getByText('Delete'))
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())

    // Switch chats behind the open dialog
    go(`/chats?conversationId=${chatPolygon.id}`)
    await waitFor(() => expect(header()).toBe(chatPolygon.title))

    await act(async () => { fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' })) })
    await waitFor(() => expect(deleteMessages).toHaveBeenCalledWith(chatAztec.id, [1], false))
  })

  it('"Delete for everyone" starts unticked on every open', async () => {
    renderChats(`/chats?conversationId=${chatAztec.id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    const openDelete = () => {
      const bubble = within(messagePane()).getByText('aztec hello').closest('[data-message-ids]') as HTMLElement
      fireEvent.click(within(bubble).getByLabelText('More actions'))
      fireEvent.click(within(bubble).getByText('Delete'))
    }
    openDelete()
    const box = await screen.findByRole('checkbox')
    fireEvent.click(box)
    expect((box as HTMLInputElement).checked).toBe(true)
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    openDelete()
    expect(((await screen.findByRole('checkbox')) as HTMLInputElement).checked).toBe(false)
  })

  it('reply and edit state stay with their chat', async () => {
    renderChats(`/chats?conversationId=${chatAztec.id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    const bubble = within(messagePane()).getByText('aztec hello').closest('[data-message-ids]') as HTMLElement
    fireEvent.click(within(bubble).getByTitle('Reply'))
    await waitFor(() => expect(compose().placeholder).toMatch(/reply/i))

    clickRow(chatPolygon)
    await waitFor(() => expect(header()).toBe(chatPolygon.title))
    expect(compose().placeholder).toMatch(/^Message/)

    clickRow(chatAztec)
    await waitFor(() => expect(header()).toBe(chatAztec.title))
    expect(compose().placeholder).toMatch(/reply/i)

    // Edit my own message in Aztec, then switch: Polygon is not in edit mode
    const mine = within(messagePane()).getByText('mine').closest('[data-message-ids]') as HTMLElement
    fireEvent.click(within(mine).getByLabelText('More actions'))
    fireEvent.click(within(mine).getByText('Edit'))
    await waitFor(() => expect(compose().value).toBe('mine'))
    clickRow(chatPolygon)
    await waitFor(() => expect(header()).toBe(chatPolygon.title))
    expect(compose().placeholder).not.toMatch(/edit/i)
    expect(compose().value).toBe('')
  })

  it('a send that resolves after switching chats never lands in the new chat; the old draft is cleared', async () => {
    const sent = deferred<{ success: boolean; message_id: number; message?: Message }>()
    sendMessage.mockImplementation(() => sent.promise)
    renderChats(`/chats?conversationId=${chatAztec.id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())

    fireEvent.change(compose(), { target: { value: 'hello aztec' } })
    fireEvent.keyDown(compose(), { key: 'Enter' })
    await waitFor(() => expect(sendMessage).toHaveBeenCalled())
    expect(sendMessage.mock.calls[0][0]).toBe(chatAztec.id)
    const nonce = (sendMessage.mock.calls[0][3] as { clientNonce: string }).clientNonce
    expect(nonce).toBeTruthy()

    clickRow(chatPolygon)
    await waitFor(() => expect(within(messagePane()).getByText('polygon hi')).toBeTruthy())
    await act(async () => {
      sent.resolve({ success: true, message_id: 77, message: msg(77, { conversation_id: chatAztec.id, text: 'hello aztec', is_outgoing: true }) })
    })
    expect(within(messagePane()).queryByText('hello aztec')).toBeNull()
    expect(compose().value).toBe('')

    clickRow(chatAztec)
    await waitFor(() => expect(header()).toBe(chatAztec.title))
    expect(compose().value).toBe('')
  })

  it('a failed send keeps the text, shows the error inline, and a retry reuses the nonce', async () => {
    sendMessage.mockRejectedValueOnce(new Error('Telegram not connected'))
    renderChats(`/chats?conversationId=${chatAztec.id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    fireEvent.change(compose(), { target: { value: 'retry me' } })
    fireEvent.keyDown(compose(), { key: 'Enter' })
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/not connected/))
    expect(compose().value).toBe('retry me')
    await act(async () => { fireEvent.click(within(screen.getByRole('alert')).getByText('Retry')) })
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(2))
    const n1 = (sendMessage.mock.calls[0][3] as { clientNonce: string }).clientNonce
    const n2 = (sendMessage.mock.calls[1][3] as { clientNonce: string }).clientNonce
    expect(n2).toBe(n1)
  })

  it('a WS message for another chat updates only the list, not the open pane', async () => {
    renderChats(`/chats?conversationId=${chatAztec.id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    act(() => ws.dispatch({
      type: 'new_message', conversation_id: chatPolygon.id, message_id: 9, text: 'ping from polygon',
      message: msg(9, { conversation_id: chatPolygon.id, text: 'ping from polygon' }),
    }))
    expect(within(messagePane()).queryByText('ping from polygon')).toBeNull()
    const row = document.querySelector(`[data-chat-id="${chatPolygon.id}"]`) as HTMLElement
    expect(row.textContent).toContain('ping from polygon')
    expect(row.textContent).toContain('1') // unread badge
  })

  it('a partial multi-file send keeps only the failed files and retries just those with a new nonce', async () => {
    const a = new File(['a'], 'a.pdf', { type: 'application/pdf' })
    const b = new File(['bb'], 'b.zip', { type: 'application/zip' })
    sendFiles.mockReset()
      .mockReturnValueOnce({ abort: () => {}, promise: Promise.resolve({
        success: false, partial: true, message_ids: [90],
        messages: [msg(90, { conversation_id: chatAztec.id, text: null, attachments: [{ id: 900, kind: 'document', filename: 'a.pdf', mime_type: 'application/pdf', size: 1, url: null }], is_outgoing: true })],
        files: [{ filename: 'a.pdf', status: 'sent', message_id: 90 }, { filename: 'b.zip', status: 'failed', error: 'FILE_PARTS_INVALID' }],
      }) })
      .mockReturnValueOnce({ abort: () => {}, promise: Promise.resolve({ success: true, message_ids: [91], messages: [], files: [{ filename: 'b.zip', status: 'sent', message_id: 91 }] }) })
    renderChats(`/chats?conversationId=${chatAztec.id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(fileInput, { target: { files: [a, b] } })
    fireEvent.change(compose(), { target: { value: 'two files' } })
    fireEvent.keyDown(compose(), { key: 'Enter' })

    const alert = await screen.findByText('1 of 2 files weren’t sent')
    const box = alert.closest('[role="alert"]') as HTMLElement
    expect(box.textContent).toContain('b.zip')
    expect(box.textContent).toContain('FILE_PARTS_INVALID')
    // The tray keeps only the failed file (attachment chips have a "Remove" button)
    const tray = () => Array.from(document.querySelectorAll('button[title="Remove"]')).map((btn) => btn.parentElement!.textContent)
    expect(tray()).toHaveLength(1)
    expect(tray()[0]).toContain('b.zip')
    expect(compose().value).toBe('') // caption went with the sent file

    await act(async () => { fireEvent.click(within(box).getByText('Retry it')) })
    await waitFor(() => expect(sendFiles).toHaveBeenCalledTimes(2))
    expect(sendFiles.mock.calls[1][1]).toEqual([b])
    const nonce = (i: number) => (sendFiles.mock.calls[i][5] as { clientNonce: string }).clientNonce
    expect(nonce(1)).not.toBe(nonce(0))
    await waitFor(() => expect(tray()).toHaveLength(0))
    expect(screen.queryByText(/weren’t sent/)).toBeNull()
  })

  it('when no file is sent (400) every file stays attached with its error and the caption is kept', async () => {
    const a = new File(['a'], 'a.pdf', { type: 'application/pdf' })
    sendFiles.mockReset().mockImplementationOnce(() => ({ abort: () => {}, promise: Promise.reject(new ApiError('Nothing was sent', {
      status: 400, code: 'http', path: '/api/compose/send-files',
      body: { detail: 'Nothing was sent', code: 'send_failed', files: [{ filename: 'a.pdf', status: 'failed', error: 'Telegram not connected' }] },
    })) }))
    renderChats(`/chats?conversationId=${chatAztec.id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    fireEvent.change(document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [a] } })
    fireEvent.change(compose(), { target: { value: 'caption' } })
    fireEvent.keyDown(compose(), { key: 'Enter' })
    const alert = (await screen.findByText('No files were sent')).closest('[role="alert"]') as HTMLElement
    expect(alert.textContent).toContain('Telegram not connected')
    expect(compose().value).toBe('caption')
  })

  it('a conversation without capabilities offers no reply/react/edit/delete and no compose box', async () => {
    getConversations.mockImplementation(async () => [chatAztec, readOnly])
    renderChats(`/chats?conversationId=${readOnly.id}`)
    await waitFor(() => expect(within(messagePane()).getByText('news post')).toBeTruthy())
    const bubble = within(messagePane()).getByText('news post').closest('[data-message-ids]') as HTMLElement
    expect(within(bubble).queryByTitle('Reply')).toBeNull()
    expect(within(bubble).queryByTitle('React with emoji')).toBeNull()
    expect(compose()).toBeNull()
    // The same pane for a fully capable conversation has them
    clickRow(chatAztec)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    expect(within(messagePane()).getAllByTitle('Reply').length).toBeGreaterThan(0)
    expect(compose()).not.toBeNull()
  })

  it('Sync (a pull from the channel) is offered only with history_backfill, e.g. not for Signal', async () => {
    const signal = makeConversation(444, 'Signal pal', { channel: 'signal', kind: 'direct', capabilities: ['send', 'reply', 'react', 'media_send', 'read_receipts'] })
    windows[signal.id] = [msg(7, { conversation_id: signal.id, text: 'hey from signal' })]
    getConversations.mockImplementation(async () => [chatAztec, signal])
    renderChats(`/chats?conversationId=${signal.id}`)
    await waitFor(() => expect(within(messagePane()).getByText('hey from signal')).toBeTruthy())
    expect(screen.queryByTitle('Sync messages')).toBeNull()
    clickRow(chatAztec)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    expect(screen.getByTitle('Sync messages')).toBeTruthy()
  })
})
