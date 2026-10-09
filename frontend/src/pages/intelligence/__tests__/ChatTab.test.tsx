import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { controllableStream, type ControllableStream } from './testStream'
import type { AiConversationDetail, AiConversationSummary } from '../../../api/types'

function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

const conv = (id: number, title: string | null, messages: [string, string][] = []): AiConversationDetail => ({
  id, title, created_at: '2026-10-06T00:00:00Z', updated_at: '2026-10-06T00:00:00Z',
  messages: messages.map(([role, content], i) => ({ id: i, role, content, created_at: '2026-10-06T00:00:00Z' })),
})
const summary = (c: AiConversationDetail): AiConversationSummary => ({
  id: c.id, title: c.title, created_at: c.created_at, updated_at: c.updated_at, message_count: c.messages.length,
})

const streams: { path: string; body: unknown; s: ControllableStream }[] = []
const pendingDetails: Record<number, ReturnType<typeof deferred<AiConversationDetail>>> = {}
const listAiConversations = vi.fn<() => Promise<AiConversationSummary[]>>()
const suggestAiConversationTitle = vi.fn()
const updateAiConversationTitle = vi.fn()

vi.mock('../../../api/client', () => ({
  listAiConversations: () => listAiConversations(),
  getAiConversation: (id: number) => {
    pendingDetails[id] = deferred<AiConversationDetail>()
    return pendingDetails[id].promise
  },
  createAiConversation: vi.fn(),
  deleteAiConversation: vi.fn(),
  updateAiConversationTitle: (...a: unknown[]) => updateAiConversationTitle(...a),
  suggestAiConversationTitle: (id: number) => suggestAiConversationTitle(id),
  stream: (path: string, opts: { body?: unknown; signal?: AbortSignal }) => {
    const s = controllableStream()
    streams.push({ path, body: opts.body, s })
    return s.gen(opts)
  },
  isAbortError: (e: unknown) => e instanceof DOMException && e.name === 'AbortError',
}))

import ChatTab from '../ChatTab'
import { ConfirmProvider } from '../../../components/ui'

const A = conv(1, 'Alpha', [['user', 'question A'], ['assistant', 'answer A']])
const B = conv(2, 'Beta', [['user', 'question B'], ['assistant', 'answer B']])

beforeEach(() => {
  streams.length = 0
  for (const k of Object.keys(pendingDetails)) delete pendingDetails[Number(k)]
  listAiConversations.mockReset()
  suggestAiConversationTitle.mockReset()
  updateAiConversationTitle.mockReset()
})

const renderTab = () => render(<ConfirmProvider><ChatTab /></ConfirmProvider>)

describe('ChatTab', () => {
  it('selecting conversations is latest-only: a slow earlier load never replaces the current one', async () => {
    listAiConversations.mockResolvedValue([summary(A), summary(B)])
    renderTab()
    await waitFor(() => expect(pendingDetails[1]).toBeDefined())
    await act(async () => { pendingDetails[1].resolve(A) })
    expect(await screen.findByText('answer A')).toBeTruthy()

    // A → B → A: the first B load stays pending, then A is re-selected… and B resolves late
    fireEvent.click(screen.getByText('Beta'))
    const slowB = pendingDetails[2]
    fireEvent.click(screen.getByText('Alpha'))
    await act(async () => { pendingDetails[1].resolve(A) })
    await act(async () => { slowB.resolve(B) })

    expect(screen.getByText('answer A')).toBeTruthy()
    expect(screen.queryByText('answer B')).toBeNull()
  })

  it('sends only {ai_conversation_id, message} (backend persists the turn) and drops a stream after switching away', async () => {
    listAiConversations.mockResolvedValue([summary(A), summary(B)])
    renderTab()
    await waitFor(() => expect(pendingDetails[1]).toBeDefined())
    await act(async () => { pendingDetails[1].resolve(A) })

    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'hello' } })
    fireEvent.click(screen.getByLabelText('Send'))
    await waitFor(() => expect(streams).toHaveLength(1))
    expect(streams[0].path).toBe('/api/intelligence/chat')
    expect(streams[0].body).toEqual({ ai_conversation_id: 1, message: 'hello' })

    // Switch to B mid-reply: A's stream is aborted and its late chunks never reach B
    fireEvent.click(screen.getByText('Beta'))
    expect(streams[0].s.signal?.aborted).toBe(true)
    await act(async () => { pendingDetails[2].resolve(B) })
    await act(async () => { streams[0].s.push('text', { content: 'LEAKED' }) })
    expect(await screen.findByText('answer B')).toBeTruthy()
    expect(screen.queryByText('LEAKED')).toBeNull()
    expect(screen.queryByText('hello')).toBeNull()
  })

  it('applies a suggested title only while the conversation is still untitled', async () => {
    const U = conv(3, null)
    listAiConversations.mockResolvedValue([summary(U)])
    const suggestion = deferred<{ title: string; applied: boolean }>()
    suggestAiConversationTitle.mockReturnValue(suggestion.promise)
    updateAiConversationTitle.mockResolvedValue(undefined)
    renderTab()
    await waitFor(() => expect(pendingDetails[3]).toBeDefined())
    await act(async () => { pendingDetails[3].resolve(U) })

    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByLabelText('Send'))
    await waitFor(() => expect(streams).toHaveLength(1))
    await act(async () => {
      streams[0].s.push('text', { content: 'reply' })
      streams[0].s.push('done', { ai_conversation_id: 3 })
    })
    await waitFor(() => expect(suggestAiConversationTitle).toHaveBeenCalledWith(3))

    // User renames manually before the suggestion comes back
    fireEvent.click(screen.getByTitle('Click to rename'))
    const input = screen.getByLabelText('Conversation title')
    fireEvent.change(input, { target: { value: 'My name' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(updateAiConversationTitle).toHaveBeenCalledWith(3, 'My name'))

    await act(async () => { suggestion.resolve({ title: 'Auto title', applied: false }) })
    expect(screen.getAllByText('My name').length).toBeGreaterThan(0)
    expect(screen.queryByText('Auto title')).toBeNull()
  })
})
