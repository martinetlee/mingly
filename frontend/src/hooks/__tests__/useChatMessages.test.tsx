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
const backfillConversation = vi.fn()
vi.mock('../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../api/client')>()),
  getMessageWindow: (...a: unknown[]) => getMessageWindow(...a),
  backfillConversation: (...a: unknown[]) => backfillConversation(...a),
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

const ids = (r: { current: { messages: { id: number }[] } }) => r.current.messages.map((m) => m.id)

beforeEach(() => {
  ws = new WebSocketClient({ url: () => null })
  getMessageWindow.mockReset()
  backfillConversation.mockReset()
})
afterEach(() => { vi.useRealTimers() })

describe('useChatMessages', () => {
  it('drops a slow window for the previous chat after a fast switch', async () => {
    const slowA = deferred<MessageWindow>()
    getMessageWindow.mockImplementation((conversationId: number) =>
      conversationId === 1 ? slowA.promise : Promise.resolve(page([7, 8], {}, { conversation_id: 2 })))
    const { result, rerender } = setup({ conversationId: 1, topicId: null, nonce: 1 })
    rerender({ conversationId: 2, topicId: null, nonce: 2 })
    await waitFor(() => expect(ids(result)).toEqual([7, 8]))

    await act(async () => { slowA.resolve(page([1, 2, 3], {}, { conversation_id: 1 })) })
    expect(ids(result)).toEqual([7, 8])
    expect(result.current.messages.every((m) => m.conversation_id === 2)).toBe(true)
    // The aborted request was given a signal that is now aborted
    expect((getMessageWindow.mock.calls[0][2] as AbortSignal).aborted).toBe(true)
  })

  it('drops an in-flight older page when the chat changes', async () => {
    const older = deferred<MessageWindow>()
    getMessageWindow.mockImplementation((conversationId: number, p: { before?: number }) => {
      if (conversationId === 1 && p.before) return older.promise
      return Promise.resolve(conversationId === 1 ? page([10, 11], { has_older: true }, { conversation_id: 1 }) : page([50], {}, { conversation_id: 2 }))
    })
    const { result, rerender } = setup({ conversationId: 1, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([10, 11]))
    act(() => { void result.current.loadOlder() })
    rerender({ conversationId: 2, topicId: null, nonce: 2 })
    await waitFor(() => expect(ids(result)).toEqual([50]))
    await act(async () => { older.resolve(page([8, 9], { has_older: true }, { conversation_id: 1 })) })
    expect(ids(result)).toEqual([50])
  })

  it('drops an in-flight older page when a jump replaces the window', async () => {
    const older = deferred<MessageWindow>()
    getMessageWindow.mockImplementation((_c: number, p: { before?: number; anchor?: number }) => {
      if (p.before) return older.promise
      if (p.anchor) return Promise.resolve(page([200, 201, 202], { has_older: true, has_newer: true }))
      return Promise.resolve(page([300, 301], { has_older: true }))
    })
    const { result } = setup({ conversationId: 100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([300, 301]))
    act(() => { void result.current.loadOlder() })
    await act(async () => { await result.current.jumpTo(201) })
    expect(ids(result)).toEqual([200, 201, 202])
    await act(async () => { older.resolve(page([298, 299], { has_older: true })) })
    expect(ids(result)).toEqual([200, 201, 202])
  })

  it('appends WS messages only at latest, only in scope, and dedupes', async () => {
    getMessageWindow.mockResolvedValue(page([1, 2], {}, { topic_id: 5 }))
    const { result } = setup({ conversationId: 100, topicId: 5, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([1, 2]))

    act(() => ws.dispatch({ type: 'new_message', conversation_id: 100, message_id: 3, message: msg(3, { topic_id: 5 }) }))
    act(() => ws.dispatch({ type: 'new_message', conversation_id: 100, message_id: 3, message: msg(3, { topic_id: 5 }) }))
    act(() => ws.dispatch({ type: 'new_message', conversation_id: 100, message_id: 4, message: msg(4, { topic_id: 6 }) }))
    act(() => ws.dispatch({ type: 'new_message', conversation_id: 200, message_id: 5, message: msg(5, { conversation_id: 200 }) }))
    expect(ids(result)).toEqual([1, 2, 3])
  })

  it('does not append WS messages under a jumped (not-at-latest) window', async () => {
    getMessageWindow.mockImplementation((_c: number, p: { anchor?: number }) =>
      Promise.resolve(p.anchor ? page([10, 11, 12], { has_newer: true }) : page([90, 91])))
    const { result } = setup({ conversationId: 100, topicId: null, anchorMessageId: 11, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([10, 11, 12]))
    expect(result.current.atLatest).toBe(false)
    act(() => ws.dispatch({ type: 'new_message', conversation_id: 100, message_id: 95, message: msg(95) }))
    expect(ids(result)).toEqual([10, 11, 12])
    await act(async () => { await result.current.jumpToLatest() })
    expect(ids(result)).toEqual([90, 91])
    expect(result.current.scrollIntent?.kind).toBe('bottom')
  })

  it('anchor jump: one scroll intent + a highlight that clears itself; a scope change clears it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    getMessageWindow.mockImplementation((conversationId: number, p: { anchor?: number }) =>
      Promise.resolve(p.anchor ? page([40, 41, 42], { has_newer: true }) : page([1], {}, { conversation_id: conversationId })))
    const { result, rerender } = setup({ conversationId: 100, topicId: null, anchorMessageId: 41, nonce: 1 })
    await waitFor(() => expect(result.current.highlightMessageId).toBe(41))
    const intent = result.current.scrollIntent
    expect(intent).toMatchObject({ kind: 'message', messageId: 41 })

    // Unrelated updates (WS edit) keep the same intent object: MessageList won't re-scroll
    act(() => ws.dispatch({ type: 'message_edited', conversation_id: 100, message_id: 40, message: msg(40, { text: 'x' }) }))
    expect(result.current.scrollIntent).toBe(intent)

    act(() => { vi.advanceTimersByTime(HIGHLIGHT_MS + 10) })
    expect(result.current.highlightMessageId).toBeNull()

    await act(async () => { await result.current.jumpTo(42) }) // in window: no fetch
    expect(result.current.highlightMessageId).toBe(42)
    rerender({ conversationId: 300, topicId: null, nonce: 2 })
    expect(result.current.highlightMessageId).toBeNull()
  })

  it('places the unread separator from the unread count at selection', async () => {
    getMessageWindow.mockResolvedValue(page([1, 2, 3, 4, 5]))
    const { result } = setup({ conversationId: 100, topicId: null, unreadCount: 2, nonce: 1 })
    await waitFor(() => expect(result.current.firstUnreadMessageId).toBe(4))
    expect(result.current.scrollIntent).toMatchObject({ kind: 'message', messageId: 4 })
  })

  it('backfills from the channel when local history is exhausted, then reports exhausted', async () => {
    getMessageWindow.mockImplementation((_c: number, p: { before?: number }) =>
      Promise.resolve(p.before ? page([3, 4]) : page([5, 6])))
    backfillConversation.mockResolvedValueOnce({ added: 2, exhausted: true, status: 'changed' })
    const { result } = setup({ conversationId: 100, topicId: 9, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([5, 6]))
    await act(async () => { await result.current.loadOlder() })
    expect(backfillConversation).toHaveBeenCalledWith(100, { limit: 100, topicId: 9 }, expect.any(AbortSignal))
    expect(ids(result)).toEqual([3, 4, 5, 6])
    expect(result.current.backfill).toBe('exhausted')
    // Exhausted: scrolling to the top again doesn't call the channel
    await act(async () => { await result.current.loadOlder() })
    expect(backfillConversation).toHaveBeenCalledTimes(1)
  })

  it('a failed backfill offers retry instead of looping', async () => {
    getMessageWindow.mockResolvedValue(page([5, 6]))
    backfillConversation.mockRejectedValueOnce(new Error('Telegram rate limit — retry in 30s'))
    const { result } = setup({ conversationId: 100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([5, 6]))
    await act(async () => { await result.current.loadOlder() })
    await act(async () => { await result.current.loadOlder() })
    expect(backfillConversation).toHaveBeenCalledTimes(1)
    expect(result.current.backfill).toBe('retry')
    expect(result.current.backfillError).toMatch(/rate limit/)
  })

  it('applies scam WS states by message id and sync lifecycle events', async () => {
    getMessageWindow.mockResolvedValue({ ...page([1, 2]), messages: [msg(1, { attachments: [{ id: 501, kind: 'photo', filename: null, mime_type: null, size: null, url: null }] }), msg(2)] })
    const { result } = setup({ conversationId: 100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([1, 2]))
    act(() => ws.dispatch({
      type: 'scam_check_safe', request_id: 'r', source: 'manual', conversation_id: 100, message_ids: [2], identity_ids: [1],
      messages: [{ conversation_id: 100, message_id: 2, identity_id: 1, scam_score: 0.1, scam_reason: null, scam_category: null,
        scam_manual_verdict: null, scam_status: 'completed', scam_error: null, effective_is_scam: false }],
      pipeline_is_scam: false, confidence: 0.8, reason: '', category: null,
    }))
    expect(result.current.messages[1].scam_status).toBe('completed')
    act(() => ws.dispatch({ type: 'conversation_sync_failed', conversation_id: 100, status: 'error', error: 'boom' }))
    expect(result.current.syncFailed).toEqual({ status: 'error', error: 'boom' })
    act(() => ws.dispatch({ type: 'media_ready', conversation_id: 100, message_id: 1, attachment_id: 501, kind: 'photo', url: '/m/1.jpg' }))
    expect(result.current.messages[0].attachments[0].url).toBe('/m/1.jpg')
    act(() => ws.dispatch({ type: 'messages_deleted', conversation_id: 100, message_ids: [1] }))
    expect(ids(result)).toEqual([2])
  })

  it('a jump to a message that is not available keeps the current window and explains why', async () => {
    const jump = deferred<MessageWindow>()
    getMessageWindow.mockImplementation((_c: number, p: { anchor?: number }) =>
      p.anchor ? jump.promise : Promise.resolve(page([300, 301], { has_older: true })))
    const { result } = setup({ conversationId: 100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([300, 301]))
    act(() => { void result.current.jumpTo(5) })
    // Loading state while the server fetches the target from Telegram
    await waitFor(() => expect(result.current.jumpingTo).toEqual({ kind: 'message', messageId: 5 }))
    expect(result.current.replacing).toBe(true)
    await act(async () => { jump.resolve(page([300, 301], { anchor_found: false, anchor_reason: 'not_found' })) })
    expect(ids(result)).toEqual([300, 301]) // not yanked to the bottom
    expect(result.current.jumpingTo).toBeNull()
    expect(result.current.jumpNotice).toMatchObject({ reason: 'not_found', target: { kind: 'message', messageId: 5 } })
    expect(result.current.highlightMessageId).toBeNull()
    act(() => result.current.dismissJumpNotice())
    expect(result.current.jumpNotice).toBeNull()
  })

  it('a failed jump (Telegram offline) becomes an inline notice, not a blank pane', async () => {
    getMessageWindow.mockImplementation((_c: number, p: { anchor?: number }) =>
      p.anchor ? Promise.reject(new Error('Telegram not connected')) : Promise.resolve(page([300, 301])))
    const { result } = setup({ conversationId: 100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([300, 301]))
    await act(async () => { await result.current.jumpTo(5) })
    expect(ids(result)).toEqual([300, 301])
    expect(result.current.error).toBeNull()
    expect(result.current.jumpNotice).toMatchObject({ reason: 'error', detail: 'Telegram not connected' })
  })

  it('opening a chat on a missing anchor shows the latest window plus the notice', async () => {
    getMessageWindow.mockResolvedValue(page([300, 301], { anchor_found: false, anchor_reason: 'unavailable' }))
    const { result } = setup({ conversationId: 100, topicId: null, anchorMessageId: 5, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([300, 301]))
    expect(result.current.jumpNotice).toMatchObject({ reason: 'unavailable' })
    expect(result.current.scrollIntent?.kind).toBe('bottom')
  })

  it('a successful jump clears an earlier notice', async () => {
    getMessageWindow.mockImplementation((_c: number, p: { anchor?: number }) =>
      Promise.resolve(p.anchor === 5 ? page([300], { anchor_found: false, anchor_reason: 'not_found' })
        : p.anchor === 40 ? page([39, 40, 41], { has_newer: true }) : page([300])))
    const { result } = setup({ conversationId: 100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([300]))
    await act(async () => { await result.current.jumpTo(5) })
    expect(result.current.jumpNotice).not.toBeNull()
    await act(async () => { await result.current.jumpTo(40) })
    expect(result.current.jumpNotice).toBeNull()
    expect(ids(result)).toEqual([39, 40, 41])
  })

  it('jumps to an unstored reply target by its channel id (around_external_id)', async () => {
    getMessageWindow.mockImplementation((_c: number, p: { aroundExternalId?: string }) =>
      Promise.resolve(p.aroundExternalId === '7007'
        ? { ...page([60, 62], { has_newer: true }), messages: [msg(60), msg(61, { external_id: '7007' }), msg(62)] }
        : page([300, 301])))
    const { result } = setup({ conversationId: 100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([300, 301]))
    await act(async () => { await result.current.jumpToExternal('7007') })
    expect(getMessageWindow).toHaveBeenLastCalledWith(100, expect.objectContaining({ aroundExternalId: '7007' }), expect.any(AbortSignal))
    expect(ids(result)).toEqual([60, 61, 62])
    expect(result.current.highlightMessageId).toBe(61)
    expect(result.current.scrollIntent).toMatchObject({ kind: 'message', messageId: 61 })
  })

  it('an external target already in the window needs no fetch', async () => {
    getMessageWindow.mockResolvedValue(page([10, 11]))
    const { result } = setup({ conversationId: 100, topicId: null, nonce: 1 })
    await waitFor(() => expect(ids(result)).toEqual([10, 11]))
    const calls = getMessageWindow.mock.calls.length
    await act(async () => { await result.current.jumpToExternal('11') })
    expect(getMessageWindow).toHaveBeenCalledTimes(calls)
    expect(result.current.highlightMessageId).toBe(11)
  })

  it('never backfills a conversation without the history_backfill capability', async () => {
    getMessageWindow.mockResolvedValue(page([5, 6]))
    const { result } = setup({ conversationId: 100, topicId: null, nonce: 1, canBackfill: false })
    await waitFor(() => expect(ids(result)).toEqual([5, 6]))
    await act(async () => { await result.current.loadOlder() })
    expect(backfillConversation).not.toHaveBeenCalled()
  })
})
