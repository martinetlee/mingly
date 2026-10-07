/**
 * Races guarded by useChatMessages: results applied only for the scope and
 * window generation they were requested for; WS appends only at latest and in
 * scope; merges dedupe; highlight/scroll intent are one-shot.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import type { MessageWindow } from '../../api/types'
import { WebSocketClient } from '../../lib/ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { deferred, msg, page } from '../../pages/chats/__tests__/fixtures'

const getMessageWindow = vi.fn()
const backfillChat = vi.fn()
vi.mock('../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../api/client')>()),
  getMessageWindow: (...a: unknown[]) => getMessageWindow(...a),
  backfillChat: (...a: unknown[]) => backfillChat(...a),
}))

import { useChatMessages, HIGHLIGHT_MS } from '../useChatMessages'
import type { ChatMessagesScope } from '../useChatMessages'

let ws: WebSocketClient
function wrapper({ children }: { children: ReactNode }) {
  return <WebSocketProvider client={ws}>{children}</WebSocketProvider>
}

function setup(initial: ChatMessagesScope) {
  return renderHook((scope: ChatMessagesScope) => useChatMessages(scope), { initialProps: initial, wrapper })
}

const ids = (r: { current: { messages: { tg_message_id: number }[] } }) => r.current.messages.map((m) => m.tg_message_id)

beforeEach(() => {
  ws = new WebSocketClient({ url: () => null })
  getMessageWindow.mockReset()
  backfillChat.mockReset()
})
afterEach(() => { vi.useRealTimers() })

describe('useChatMessages', () => {
  it('drops a slow window for the previous chat after a fast switch', async () => {
    const slowA = deferred<MessageWindow>()
    getMessageWindow.mockImplementation((chatId: number) =>
      chatId === -1 ? slowA.promise : Promise.resolve(page([7, 8], {}, { chat_id: -2 })))
    const { result, rerender } = setup({ chatId: -1, topicId: null, nonce: 1 })
    rerender({ chatId: -2, topicId: null, nonce: 2 })
    await waitFor(() => expect(ids(result)).toEqual([7, 8]))

    await act(async () => { slowA.resolve(page([1, 2, 3], {}, { chat_id: -1 })) })
    expect(ids(result)).toEqual([7, 8])
    expect(result.current.messages.every((m) => m.chat_id === -2)).toBe(true)
    // The aborted request was given a signal that is now aborted
    expect((getMessageWindow.mock.calls[0][2] as AbortSignal).aborted).toBe(true)
  })

  it('drops an in-flight older page when the chat changes', async () => {
    const older = deferred<MessageWindow>()
    getMessageWindow.mockImplementation((chatId: number, p: { before?: string }) => {
      if (chatId === -1 && p.before) return older.promise
      return Promise.resolve(chatId === -1 ? page([10, 11], { has_older: true }, { chat_id: -1 }) : page([50], {}, { chat_id: -2 }))
    })
    const { result, rerender } = setup({ chatId: -1, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([10, 11]))
    act(() => { void result.current.loadOlder() })
    rerender({ chatId: -2, topicId: null, nonce: 2 })
    await waitFor(() => expect(ids(result)).toEqual([50]))
    await act(async () => { older.resolve(page([8, 9], { has_older: true }, { chat_id: -1 })) })
    expect(ids(result)).toEqual([50])
  })

  it('drops an in-flight older page when a jump replaces the window', async () => {
    const older = deferred<MessageWindow>()
    getMessageWindow.mockImplementation((_c: number, p: { before?: string; anchor?: number }) => {
      if (p.before) return older.promise
      if (p.anchor) return Promise.resolve(page([200, 201, 202], { has_older: true, has_newer: true }))
      return Promise.resolve(page([300, 301], { has_older: true }))
    })
    const { result } = setup({ chatId: -100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([300, 301]))
    act(() => { void result.current.loadOlder() })
    await act(async () => { await result.current.jumpTo(201) })
    expect(ids(result)).toEqual([200, 201, 202])
    await act(async () => { older.resolve(page([298, 299], { has_older: true })) })
    expect(ids(result)).toEqual([200, 201, 202])
  })

  it('appends WS messages only at latest, only in scope, and dedupes', async () => {
    getMessageWindow.mockResolvedValue(page([1, 2], {}, { topic_id: 5 }))
    const { result } = setup({ chatId: -100, topicId: 5, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([1, 2]))

    act(() => ws.dispatch({ type: 'new_message', chat_id: -100, message_id: 3, message: msg(3, { topic_id: 5 }) }))
    act(() => ws.dispatch({ type: 'new_message', chat_id: -100, message_id: 3, message: msg(3, { topic_id: 5 }) }))
    act(() => ws.dispatch({ type: 'new_message', chat_id: -100, message_id: 4, message: msg(4, { topic_id: 6 }) }))
    act(() => ws.dispatch({ type: 'new_message', chat_id: -200, message_id: 5, message: msg(5, { chat_id: -200 }) }))
    expect(ids(result)).toEqual([1, 2, 3])
  })

  it('does not append WS messages under a jumped (not-at-latest) window', async () => {
    getMessageWindow.mockImplementation((_c: number, p: { anchor?: number }) =>
      Promise.resolve(p.anchor ? page([10, 11, 12], { has_newer: true }) : page([90, 91])))
    const { result } = setup({ chatId: -100, topicId: null, anchorTgId: 11, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([10, 11, 12]))
    expect(result.current.atLatest).toBe(false)
    act(() => ws.dispatch({ type: 'new_message', chat_id: -100, message_id: 95, message: msg(95) }))
    expect(ids(result)).toEqual([10, 11, 12])
    await act(async () => { await result.current.jumpToLatest() })
    expect(ids(result)).toEqual([90, 91])
    expect(result.current.scrollIntent?.kind).toBe('bottom')
  })

  it('anchor jump: one scroll intent + a highlight that clears itself; a scope change clears it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    getMessageWindow.mockImplementation((chatId: number, p: { anchor?: number }) =>
      Promise.resolve(p.anchor ? page([40, 41, 42], { has_newer: true }) : page([1], {}, { chat_id: chatId })))
    const { result, rerender } = setup({ chatId: -100, topicId: null, anchorTgId: 41, nonce: 1 })
    await waitFor(() => expect(result.current.highlightTgId).toBe(41))
    const intent = result.current.scrollIntent
    expect(intent).toMatchObject({ kind: 'message', tgId: 41 })

    // Unrelated updates (WS edit) keep the same intent object: MessageList won't re-scroll
    act(() => ws.dispatch({ type: 'message_edited', chat_id: -100, tg_message_id: 40, message: msg(40, { text: 'x' }) }))
    expect(result.current.scrollIntent).toBe(intent)

    act(() => { vi.advanceTimersByTime(HIGHLIGHT_MS + 10) })
    expect(result.current.highlightTgId).toBeNull()

    await act(async () => { await result.current.jumpTo(42) }) // in window: no fetch
    expect(result.current.highlightTgId).toBe(42)
    rerender({ chatId: -300, topicId: null, nonce: 2 })
    expect(result.current.highlightTgId).toBeNull()
  })

  it('places the unread separator from the unread count at selection', async () => {
    getMessageWindow.mockResolvedValue(page([1, 2, 3, 4, 5]))
    const { result } = setup({ chatId: -100, topicId: null, unreadCount: 2, nonce: 1 })
    await waitFor(() => expect(result.current.firstUnreadTgId).toBe(4))
    expect(result.current.scrollIntent).toMatchObject({ kind: 'message', tgId: 4 })
  })

  it('backfills from Telegram when local history is exhausted, then reports exhausted', async () => {
    getMessageWindow.mockImplementation((_c: number, p: { before?: string }) =>
      Promise.resolve(p.before ? page([3, 4]) : page([5, 6])))
    backfillChat.mockResolvedValueOnce({ added: 2, exhausted: true, status: 'changed' })
    const { result } = setup({ chatId: -100, topicId: 9, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([5, 6]))
    await act(async () => { await result.current.loadOlder() })
    expect(backfillChat).toHaveBeenCalledWith(-100, { limit: 100, topicId: 9 }, expect.any(AbortSignal))
    expect(ids(result)).toEqual([3, 4, 5, 6])
    expect(result.current.backfill).toBe('exhausted')
    // Exhausted: scrolling to the top again doesn't call Telegram
    await act(async () => { await result.current.loadOlder() })
    expect(backfillChat).toHaveBeenCalledTimes(1)
  })

  it('a failed backfill offers retry instead of looping', async () => {
    getMessageWindow.mockResolvedValue(page([5, 6]))
    backfillChat.mockRejectedValueOnce(new Error('Telegram rate limit — retry in 30s'))
    const { result } = setup({ chatId: -100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([5, 6]))
    await act(async () => { await result.current.loadOlder() })
    await act(async () => { await result.current.loadOlder() })
    expect(backfillChat).toHaveBeenCalledTimes(1)
    expect(result.current.backfill).toBe('retry')
    expect(result.current.backfillError).toMatch(/rate limit/)
  })

  it('applies scam WS states by (chat, tg id) and sync lifecycle events', async () => {
    getMessageWindow.mockResolvedValue(page([1, 2]))
    const { result } = setup({ chatId: -100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([1, 2]))
    act(() => ws.dispatch({
      type: 'scam_check_safe', request_id: 'r', source: 'manual', chat_id: -100, message_ids: [2], sender_ids: [1],
      messages: [{ chat_id: -100, tg_message_id: 2, sender_id: 1, scam_score: 0.1, scam_reason: null, scam_category: null,
        scam_manual_verdict: null, scam_status: 'completed', scam_error: null, effective_is_scam: false }],
      pipeline_is_scam: false, confidence: 0.8, reason: '', category: null,
    }))
    expect(result.current.messages[1].scam_status).toBe('completed')
    act(() => ws.dispatch({ type: 'chat_sync_failed', chat_id: -100, status: 'error', error: 'boom' }))
    expect(result.current.syncFailed).toEqual({ status: 'error', error: 'boom' })
    act(() => ws.dispatch({ type: 'media_ready', chat_id: -100, tg_message_id: 1, media_type: 'photo', media_url: '/m/1.jpg' }))
    expect(result.current.messages[0].media_url).toBe('/m/1.jpg')
    act(() => ws.dispatch({ type: 'messages_deleted', chat_id: -100, message_ids: [1] }))
    expect(ids(result)).toEqual([2])
  })

  it('a jump to a message that is not available keeps the current window and explains why', async () => {
    const jump = deferred<MessageWindow>()
    getMessageWindow.mockImplementation((_c: number, p: { anchor?: number }) =>
      p.anchor ? jump.promise : Promise.resolve(page([300, 301], { has_older: true })))
    const { result } = setup({ chatId: -100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([300, 301]))
    act(() => { void result.current.jumpTo(5) })
    // Loading state while the server fetches the target from Telegram
    await waitFor(() => expect(result.current.jumpingTo).toEqual({ kind: 'message', tgId: 5 }))
    expect(result.current.replacing).toBe(true)
    await act(async () => { jump.resolve(page([300, 301], { anchor_found: false, anchor_reason: 'not_found' })) })
    expect(ids(result)).toEqual([300, 301]) // not yanked to the bottom
    expect(result.current.jumpingTo).toBeNull()
    expect(result.current.jumpNotice).toMatchObject({ reason: 'not_found', target: { kind: 'message', tgId: 5 } })
    expect(result.current.highlightTgId).toBeNull()
    act(() => result.current.dismissJumpNotice())
    expect(result.current.jumpNotice).toBeNull()
  })

  it('a failed jump (Telegram offline) becomes an inline notice, not a blank pane', async () => {
    getMessageWindow.mockImplementation((_c: number, p: { anchor?: number }) =>
      p.anchor ? Promise.reject(new Error('Telegram not connected')) : Promise.resolve(page([300, 301])))
    const { result } = setup({ chatId: -100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([300, 301]))
    await act(async () => { await result.current.jumpTo(5) })
    expect(ids(result)).toEqual([300, 301])
    expect(result.current.error).toBeNull()
    expect(result.current.jumpNotice).toMatchObject({ reason: 'error', detail: 'Telegram not connected' })
  })

  it('opening a chat on a missing anchor shows the latest window plus the notice', async () => {
    getMessageWindow.mockResolvedValue(page([300, 301], { anchor_found: false, anchor_reason: 'unavailable' }))
    const { result } = setup({ chatId: -100, topicId: null, anchorTgId: 5, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([300, 301]))
    expect(result.current.jumpNotice).toMatchObject({ reason: 'unavailable' })
    expect(result.current.scrollIntent?.kind).toBe('bottom')
  })

  it('a successful jump clears an earlier notice', async () => {
    getMessageWindow.mockImplementation((_c: number, p: { anchor?: number }) =>
      Promise.resolve(p.anchor === 5 ? page([300], { anchor_found: false, anchor_reason: 'not_found' })
        : p.anchor === 40 ? page([39, 40, 41], { has_newer: true }) : page([300])))
    const { result } = setup({ chatId: -100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([300]))
    await act(async () => { await result.current.jumpTo(5) })
    expect(result.current.jumpNotice).not.toBeNull()
    await act(async () => { await result.current.jumpTo(40) })
    expect(result.current.jumpNotice).toBeNull()
    expect(ids(result)).toEqual([39, 40, 41])
  })
})
