/**
 * Chats page integration: every entry point goes through selectChat, and
 * per-chat state / dialog targets can't cross chats.
 *
 * Regressions guarded:
 * - ?chatId= params consumed once (list reloads can't re-select / wipe the pane)
 * - in-page navigation (palette) switches chats; no "Select a chat" flash
 * - a chat beyond page 1 opens via ensureChat even if the list reloads meanwhile
 * - delete dialog acts on the chat/message captured when it opened
 * - "Delete for everyone" starts unticked on every open
 * - reply / edit state stays with its chat
 * - a send that resolves after a chat switch never lands in the new chat
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, act, fireEvent, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useNavigate, useLocation } from 'react-router-dom'
import { VirtuosoMockContext } from 'react-virtuoso'
import type { Chat, Message, MessageWindow } from '../../api/types'
import { WebSocketClient } from '../../lib/ws'
import { ApiError } from '../../lib/http'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { ConfirmProvider } from '../../components/ui'
import { deferred, makeChat, msg, page } from '../chats/__tests__/fixtures'
import { __resetChatUiStore } from '../chats/chatUiStore'

const chatAztec = makeChat(-100111, 'Aztec <> ETHTaipei')
const chatPolygon = makeChat(-100222, 'Polygon <> ETHTaipei 2024')

const windows: Record<number, Message[]> = {
  [chatAztec.telegram_id]: [msg(1, { chat_id: chatAztec.telegram_id, text: 'aztec hello' }), msg(2, { chat_id: chatAztec.telegram_id, text: 'mine', is_outgoing: true })],
  [chatPolygon.telegram_id]: [msg(1, { chat_id: chatPolygon.telegram_id, text: 'polygon hi' })],
}

const getChats = vi.fn(async (): Promise<Chat[]> => [chatAztec, chatPolygon])
const getMessageWindow = vi.fn(async (chatId: number, _params?: object): Promise<MessageWindow> => ({
  ...page([]), messages: windows[chatId] ?? [],
}))
const ensureChat = vi.fn(async (): Promise<Chat> => { throw new Error('not found') })
const deleteMessages = vi.fn(async () => ({ success: true }))
const sendMessage = vi.fn(async (..._a: unknown[]): Promise<{ success: boolean; message_id: number; message?: Message }> => ({ success: true, message_id: 0 }))
const markChatRead = vi.fn(async () => ({ success: true }))
const searchMessagesGlobal = vi.fn()
const sendFiles = vi.fn()

vi.mock('../../api/client', async (orig) => {
  const actual = await orig<typeof import('../../api/client')>()
  const resolve = (value: unknown = null) => async () => value
  return {
    ...actual,
    getChats: (...a: unknown[]) => getChats(...(a as [])),
    getMessageWindow: (...a: unknown[]) => getMessageWindow(...(a as [number, object])),
    ensureChat: (...a: unknown[]) => ensureChat(...(a as [])),
    deleteMessages: (...a: unknown[]) => deleteMessages(...(a as [])),
    sendMessage: (...a: unknown[]) => sendMessage(...a),
    markChatRead: (...a: unknown[]) => markChatRead(...(a as [])),
    searchMessagesGlobal: (...a: unknown[]) => searchMessagesGlobal(...a),
    sendFiles: (...a: unknown[]) => sendFiles(...a),
    getFolders: resolve([]),
    getScamFlags: resolve({ flags: [], flagged_sender_ids: [], flagged_chat_ids: [] }),
    getPinnedMessage: resolve(null),
    getUserPresence: resolve(null),
    getContactByTelegram: resolve({ is_important: false }),
    searchChatsTelegram: resolve([]),
    sendTypingAction: resolve(null),
    backfillChat: resolve({ added: 0, exhausted: true, status: 'unchanged' }),
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

const windowCallsFor = (chatId: number) => getMessageWindow.mock.calls.filter((c) => c[0] === chatId)
const header = () => document.querySelector('h2')?.textContent
const go = (url: string) => act(() => { routerControls.navigate!(url) })
const clickRow = (chat: Chat) => act(() => {
  fireEvent.click(document.querySelector(`[data-chat-id="${chat.telegram_id}"] [data-chat-row]`)!)
})
const messagePane = () => screen.getByTestId('message-scroll')
const compose = () => document.querySelector('textarea') as HTMLTextAreaElement

beforeEach(() => {
  __resetChatUiStore()
  getChats.mockReset().mockImplementation(async () => [chatAztec, chatPolygon])
  getMessageWindow.mockClear()
  ensureChat.mockReset().mockImplementation(async () => { throw new Error('not found') })
  deleteMessages.mockClear()
  sendMessage.mockReset().mockImplementation(async () => ({ success: true, message_id: 0 }))
  markChatRead.mockClear()
})

describe('Chats entry points', () => {
  it('selects the chat from /chats?chatId=… and consumes the param', async () => {
    renderChats(`/chats?chatId=${chatPolygon.telegram_id}`)
    await waitFor(() => expect(header()).toBe(chatPolygon.name))
    expect(windowCallsFor(chatPolygon.telegram_id).length).toBeGreaterThan(0)
    expect(routerControls.search).toBe('')
  })

  it('opens centred on msgId (anchor window)', async () => {
    renderChats(`/chats?chatId=${chatPolygon.telegram_id}&msgId=1`)
    await waitFor(() => expect(windowCallsFor(chatPolygon.telegram_id)[0]?.[1]).toMatchObject({ anchor: 1 }))
  })

  it('switches chats when navigating in-page (palette click) and again on a second nav', async () => {
    renderChats('/chats')
    await waitFor(() => expect(getChats).toHaveBeenCalled())
    go(`/chats?chatId=${chatAztec.telegram_id}`)
    await waitFor(() => expect(header()).toBe(chatAztec.name))
    go(`/chats?chatId=${chatPolygon.telegram_id}`)
    await waitFor(() => expect(header()).toBe(chatPolygon.name))
  })

  it('opens a chat beyond page 1 via ensureChat, even if the list reloads meanwhile, without flashing the empty state', async () => {
    const offPage = makeChat(-100999, 'Beyond Page One')
    const ensured = deferred<Chat>()
    ensureChat.mockImplementation(() => ensured.promise)
    const { container } = renderChats(`/chats?chatId=${offPage.telegram_id}`)
    let flashed = false
    const obs = new MutationObserver(() => { if (container.textContent?.includes('Select a chat to start')) flashed = true })
    obs.observe(container, { subtree: true, childList: true, characterData: true })

    await waitFor(() => expect(ensureChat).toHaveBeenCalled())
    await act(async () => { fireEvent.click(screen.getByText('Unread')) }) // list reload mid-flight
    await act(async () => { ensured.resolve(offPage) })
    await waitFor(() => expect(header()).toBe(offPage.name))
    obs.disconnect()
    expect(flashed).toBe(false)
  })

  it('a user click while a nav is resolving wins over the late nav result', async () => {
    const ensured = deferred<Chat>()
    ensureChat.mockImplementation(() => ensured.promise)
    renderChats('/chats?chatId=-100999')
    await waitFor(() => expect(ensureChat).toHaveBeenCalled())
    clickRow(chatAztec)
    await act(async () => { ensured.resolve(makeChat(-100999, 'Late')) })
    expect(header()).toBe(chatAztec.name)
  })

  it('j/k navigate the list; selecting marks the chat read', async () => {
    getChats.mockImplementation(async () => [chatAztec, { ...chatPolygon, unread_count: 3 }])
    renderChats('/chats')
    await waitFor(() => expect(document.querySelector(`[data-chat-id="${chatAztec.telegram_id}"]`)).not.toBeNull())
    // Same activity time → alphabetical: Aztec, then Polygon
    act(() => { fireEvent.keyDown(window, { key: 'j' }) })
    await waitFor(() => expect(header()).toBe(chatAztec.name))
    // Keyboard selection keeps focus in the list (the compose box doesn't steal the next j)
    expect(document.activeElement?.closest('[data-chat-list]')).not.toBeNull()
    act(() => { fireEvent.keyDown(document.activeElement!, { key: 'j' }) })
    await waitFor(() => expect(header()).toBe(chatPolygon.name))
    expect(markChatRead).toHaveBeenCalledWith(chatPolygon.telegram_id)
    act(() => { fireEvent.keyDown(document.activeElement!, { key: 'k' }) })
    await waitFor(() => expect(header()).toBe(chatAztec.name))
  })
})

describe('Chats message search', () => {
  it('a new query aborts the running one and only the latest results show', async () => {
    const first = deferred<unknown>()
    searchMessagesGlobal.mockReset()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(async () => [{ telegram_id: chatAztec.telegram_id, name: 'Aztec hit', type: 'group', messages: [{ tg_message_id: 1, text: 'beta match', sender_name: null, date: null }] }])
    renderChats('/chats')
    const input = await screen.findByPlaceholderText('Search chats…')
    fireEvent.change(input, { target: { value: 'alpha' } })
    fireEvent.click(await screen.findByText('Search message content'))
    await waitFor(() => expect(searchMessagesGlobal).toHaveBeenCalledTimes(1))
    fireEvent.change(input, { target: { value: 'beta' } })
    fireEvent.click(await screen.findByText('Search message content'))
    await waitFor(() => expect(screen.getByText('beta match')).toBeTruthy())
    expect((searchMessagesGlobal.mock.calls[0][1] as AbortSignal).aborted).toBe(true)
    // The stale 'alpha' response can't replace the newer results
    await act(async () => { first.resolve([{ telegram_id: 1, name: 'stale', type: 'group', messages: [{ tg_message_id: 2, text: 'alpha match', sender_name: null, date: null }] }]) })
    expect(screen.queryByText('alpha match')).toBeNull()
    expect(screen.getByText('beta match')).toBeTruthy()
  })

  it('a message search hit opens the chat centred on the match', async () => {
    searchMessagesGlobal.mockReset().mockResolvedValue([{ telegram_id: chatPolygon.telegram_id, name: 'Polygon hit', type: 'group', messages: [{ tg_message_id: 1, text: 'hit text', sender_name: null, date: null }] }])
    renderChats('/chats')
    const input = await screen.findByPlaceholderText('Search chats…')
    fireEvent.change(input, { target: { value: 'hit' } })
    fireEvent.click(await screen.findByText('Search message content'))
    fireEvent.click(await screen.findByText('Polygon hit'))
    await waitFor(() => expect(header()).toBe(chatPolygon.name))
    expect(windowCallsFor(chatPolygon.telegram_id)[0][1]).toMatchObject({ anchor: 1 })
  })
})

describe('Chats list virtualization', () => {
  it('mounts only visible chat rows', async () => {
    const many = Array.from({ length: 300 }, (_, i) => makeChat(-200000 - i, `Chat ${i}`))
    getChats.mockImplementation(async () => many)
    renderChats('/chats')
    await waitFor(() => expect(document.querySelectorAll('[data-chat-id]').length).toBeGreaterThan(0))
    expect(document.querySelectorAll('[data-chat-id]').length).toBeLessThan(80)
  })
})

describe('Chats: per-chat state and captured targets', () => {
  it('delete acts on the chat/message captured when the dialog opened', async () => {
    renderChats(`/chats?chatId=${chatAztec.telegram_id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    const bubble = within(messagePane()).getByText('aztec hello').closest('[data-tg-ids]') as HTMLElement
    fireEvent.click(within(bubble).getByLabelText('More actions'))
    fireEvent.click(within(bubble).getByText('Delete'))
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())

    // Switch chats behind the open dialog
    go(`/chats?chatId=${chatPolygon.telegram_id}`)
    await waitFor(() => expect(header()).toBe(chatPolygon.name))

    await act(async () => { fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' })) })
    await waitFor(() => expect(deleteMessages).toHaveBeenCalledWith(chatAztec.telegram_id, [1], false))
  })

  it('"Delete for everyone" starts unticked on every open', async () => {
    renderChats(`/chats?chatId=${chatAztec.telegram_id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    const openDelete = () => {
      const bubble = within(messagePane()).getByText('aztec hello').closest('[data-tg-ids]') as HTMLElement
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
    renderChats(`/chats?chatId=${chatAztec.telegram_id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    const bubble = within(messagePane()).getByText('aztec hello').closest('[data-tg-ids]') as HTMLElement
    fireEvent.click(within(bubble).getByTitle('Reply'))
    await waitFor(() => expect(compose().placeholder).toMatch(/reply/i))

    clickRow(chatPolygon)
    await waitFor(() => expect(header()).toBe(chatPolygon.name))
    expect(compose().placeholder).toMatch(/^Message/)

    clickRow(chatAztec)
    await waitFor(() => expect(header()).toBe(chatAztec.name))
    expect(compose().placeholder).toMatch(/reply/i)

    // Edit my own message in Aztec, then switch: Polygon is not in edit mode
    const mine = within(messagePane()).getByText('mine').closest('[data-tg-ids]') as HTMLElement
    fireEvent.click(within(mine).getByLabelText('More actions'))
    fireEvent.click(within(mine).getByText('Edit'))
    await waitFor(() => expect(compose().value).toBe('mine'))
    clickRow(chatPolygon)
    await waitFor(() => expect(header()).toBe(chatPolygon.name))
    expect(compose().placeholder).not.toMatch(/edit/i)
    expect(compose().value).toBe('')
  })

  it('a send that resolves after switching chats never lands in the new chat; the old draft is cleared', async () => {
    const sent = deferred<{ success: boolean; message_id: number; message?: Message }>()
    sendMessage.mockImplementation(() => sent.promise)
    renderChats(`/chats?chatId=${chatAztec.telegram_id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())

    fireEvent.change(compose(), { target: { value: 'hello aztec' } })
    fireEvent.keyDown(compose(), { key: 'Enter' })
    await waitFor(() => expect(sendMessage).toHaveBeenCalled())
    expect(sendMessage.mock.calls[0][0]).toBe(chatAztec.telegram_id)
    const nonce = (sendMessage.mock.calls[0][3] as { clientNonce: string }).clientNonce
    expect(nonce).toBeTruthy()

    clickRow(chatPolygon)
    await waitFor(() => expect(within(messagePane()).getByText('polygon hi')).toBeTruthy())
    await act(async () => {
      sent.resolve({ success: true, message_id: 77, message: msg(77, { chat_id: chatAztec.telegram_id, text: 'hello aztec', is_outgoing: true }) })
    })
    expect(within(messagePane()).queryByText('hello aztec')).toBeNull()
    expect(compose().value).toBe('')

    clickRow(chatAztec)
    await waitFor(() => expect(header()).toBe(chatAztec.name))
    expect(compose().value).toBe('')
  })

  it('a failed send keeps the text, shows the error inline, and a retry reuses the nonce', async () => {
    sendMessage.mockRejectedValueOnce(new Error('Telegram not connected'))
    renderChats(`/chats?chatId=${chatAztec.telegram_id}`)
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
    renderChats(`/chats?chatId=${chatAztec.telegram_id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    act(() => ws.dispatch({
      type: 'new_message', chat_id: chatPolygon.telegram_id, message_id: 9, text: 'ping from polygon',
      message: msg(9, { chat_id: chatPolygon.telegram_id, text: 'ping from polygon' }),
    }))
    expect(within(messagePane()).queryByText('ping from polygon')).toBeNull()
    const row = document.querySelector(`[data-chat-id="${chatPolygon.telegram_id}"]`) as HTMLElement
    expect(row.textContent).toContain('ping from polygon')
    expect(row.textContent).toContain('1') // unread badge
  })

  it('a partial multi-file send keeps only the failed files and retries just those with a new nonce', async () => {
    const a = new File(['a'], 'a.pdf', { type: 'application/pdf' })
    const b = new File(['bb'], 'b.zip', { type: 'application/zip' })
    sendFiles.mockReset()
      .mockReturnValueOnce({ abort: () => {}, promise: Promise.resolve({
        success: false, partial: true, message_ids: [90],
        messages: [msg(90, { chat_id: chatAztec.telegram_id, text: null, media_type: 'document', is_outgoing: true })],
        files: [{ filename: 'a.pdf', status: 'sent', message_id: 90 }, { filename: 'b.zip', status: 'failed', error: 'FILE_PARTS_INVALID' }],
      }) })
      .mockReturnValueOnce({ abort: () => {}, promise: Promise.resolve({ success: true, message_ids: [91], messages: [], files: [{ filename: 'b.zip', status: 'sent', message_id: 91 }] }) })
    renderChats(`/chats?chatId=${chatAztec.telegram_id}`)
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
    renderChats(`/chats?chatId=${chatAztec.telegram_id}`)
    await waitFor(() => expect(within(messagePane()).getByText('aztec hello')).toBeTruthy())
    fireEvent.change(document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [a] } })
    fireEvent.change(compose(), { target: { value: 'caption' } })
    fireEvent.keyDown(compose(), { key: 'Enter' })
    const alert = (await screen.findByText('No files were sent')).closest('[role="alert"]') as HTMLElement
    expect(alert.textContent).toContain('Telegram not connected')
    expect(compose().value).toBe('caption')
  })
})
