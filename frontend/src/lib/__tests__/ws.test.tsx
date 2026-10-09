/**
 * WebSocket client/provider tests.
 *
 * Regression for review §2-H.1: the old per-component useWebSocket hook left a
 * live, reconnecting socket behind on every unmount (its onclose always
 * scheduled a reconnect). N mount/unmount cycles produced N orphaned sockets,
 * each still invoking the dead component's handler.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { StrictMode, useState } from 'react'
import { render, act, cleanup } from '@testing-library/react'
import { WebSocketClient } from '../ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { useWebSocket, useWsEvent } from '../../hooks/useWebSocket'
import { FakeWebSocket } from '../../test/fakeWebSocket'
import type { WSEvent } from '../../api/types'

function makeClient(overrides: Partial<ConstructorParameters<typeof WebSocketClient>[0]> = {}) {
  return new WebSocketClient({
    url: () => 'ws://test/api/ws?token=t',
    WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
    random: () => 1,
    baseDelayMs: 1000,
    maxDelayMs: 8000,
    ...overrides,
  })
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.useFakeTimers()
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('WebSocketClient', () => {
  it('fans events out by type and to "*" subscribers', () => {
    const c = makeClient()
    const typed = vi.fn()
    const all = vi.fn()
    c.subscribe('conversation_synced', typed)
    c.subscribe('*', all)
    c.start()
    FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].receive({ type: 'conversation_synced', conversation_id: 5 })
    FakeWebSocket.instances[0].receive({ type: 'bulk_sync_complete', synced: 1 })
    expect(typed).toHaveBeenCalledTimes(1)
    expect(typed).toHaveBeenCalledWith({ type: 'conversation_synced', conversation_id: 5 })
    expect(all).toHaveBeenCalledTimes(2)
  })

  it('ignores malformed frames and isolates throwing handlers', () => {
    const c = makeClient()
    const good = vi.fn()
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    c.subscribe('*', () => { throw new Error('boom') })
    c.subscribe('*', good)
    c.start()
    const ws = FakeWebSocket.instances[0]
    ws.open()
    ws.onmessage?.(new MessageEvent('message', { data: '{not json' }))
    ws.receive({ type: 'conversation_synced', conversation_id: 1 })
    expect(good).toHaveBeenCalledTimes(1)
    spy.mockRestore()
  })

  it('reconnects with exponential backoff and emits resync only after a reconnect', () => {
    const c = makeClient()
    const events: WSEvent[] = []
    c.subscribe('*', (e) => events.push(e))
    c.start()
    FakeWebSocket.instances[0].open()
    expect(events).toEqual([]) // first connect is not a resync

    FakeWebSocket.instances[0].drop()
    expect(FakeWebSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(999)
    expect(FakeWebSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances).toHaveLength(2) // 1000ms (attempt 0)

    // Fails before opening → next delay doubles
    FakeWebSocket.instances[1].drop()
    vi.advanceTimersByTime(1999)
    expect(FakeWebSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances).toHaveLength(3)

    FakeWebSocket.instances[2].open()
    expect(events).toEqual([{ type: 'resync' }])
  })

  it('caps the backoff and applies jitter in [delay/2, delay]', () => {
    const low = makeClient({ random: () => 0 })
    const delays = Array.from({ length: 6 }, () => low.nextDelay())
    expect(delays).toEqual([500, 1000, 2000, 4000, 4000, 4000])
    const high = makeClient({ random: () => 1 })
    expect(Array.from({ length: 5 }, () => high.nextDelay())).toEqual([1000, 2000, 4000, 8000, 8000])
  })

  it('never reconnects after stop(), even if the old socket closes later', async () => {
    const c = makeClient()
    c.start()
    FakeWebSocket.instances[0].open()
    c.stop()
    await Promise.resolve() // let the fake's async onclose run
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.live()).toHaveLength(0)
  })

  it('cancels a pending reconnect on stop()', () => {
    const c = makeClient()
    c.start()
    FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].drop() // reconnect scheduled
    c.stop()
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('does not connect without a token', () => {
    const c = makeClient({ url: () => null })
    c.start()
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(0)
  })

  it('checks the session after repeated handshake failures and stops when expired', async () => {
    const checkSession = vi.fn(async () => 'expired' as const)
    const onSessionExpired = vi.fn()
    const c = makeClient({ checkSession, onSessionExpired, handshakeFailuresBeforeCheck: 2 })
    c.start()
    FakeWebSocket.instances[0].drop()
    vi.advanceTimersByTime(1000)
    FakeWebSocket.instances[1].drop()
    expect(checkSession).toHaveBeenCalledTimes(1)
    await vi.runAllTimersAsync()
    expect(onSessionExpired).toHaveBeenCalledTimes(1)
    expect(c.isRunning).toBe(false)
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('keeps retrying when the session check says the server was just busy', async () => {
    const checkSession = vi.fn(async () => 'valid' as const)
    const c = makeClient({ checkSession, handshakeFailuresBeforeCheck: 1 })
    c.start()
    FakeWebSocket.instances[0].drop()
    await vi.advanceTimersByTimeAsync(1000)
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('close code 4001 (session ended) → session expired, never reconnects', () => {
    const onSessionExpired = vi.fn()
    const c = makeClient({ onSessionExpired })
    c.start()
    FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].drop(4001)
    vi.advanceTimersByTime(60_000)
    expect(onSessionExpired).toHaveBeenCalledTimes(1)
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(c.isRunning).toBe(false)
  })

  it('close code 4003 (origin rejected) stops without reconnecting; 1001 reconnects', () => {
    const onSessionExpired = vi.fn()
    const c = makeClient({ onSessionExpired })
    c.start()
    FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].drop(1001)
    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    FakeWebSocket.instances[1].drop(4003)
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect(onSessionExpired).not.toHaveBeenCalled()
    spy.mockRestore()
  })

  it('removes handlers on unsubscribe', () => {
    const c = makeClient()
    const off1 = c.subscribe('*', () => {})
    const off2 = c.subscribe('conversation_synced', () => {})
    expect(c.handlerCount).toBe(2)
    off1()
    off2()
    off2()
    expect(c.handlerCount).toBe(0)
  })
})

describe('WebSocketProvider + useWebSocket (leak regression)', () => {
  function Consumer({ onEvent }: { onEvent: (e: WSEvent) => void }) {
    useWebSocket(onEvent)
    return null
  }

  it('N mount/unmount cycles of a subscriber leave exactly 1 live socket and 0 handlers', async () => {
    const client = makeClient()
    let setShown: (v: boolean) => void = () => {}
    const handler = vi.fn()
    function Harness() {
      const [shown, s] = useState(true)
      setShown = s
      return shown ? <Consumer onEvent={handler} /> : null
    }
    render(
      <StrictMode>
        <WebSocketProvider client={client}>
          <Harness />
        </WebSocketProvider>
      </StrictMode>,
    )
    await act(async () => { await Promise.resolve() })

    for (let i = 0; i < 5; i++) {
      act(() => setShown(false))
      act(() => setShown(true))
    }
    act(() => setShown(false))
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })

    expect(FakeWebSocket.live()).toHaveLength(1)
    expect(client.handlerCount).toBe(0)

    // Events after unmount reach no dead handler
    FakeWebSocket.live()[0].open()
    FakeWebSocket.live()[0].receive({ type: 'conversation_synced', conversation_id: 1 })
    expect(handler).not.toHaveBeenCalled()
  })

  it('delivers to the latest handler without resubscribing, and closes for good on provider unmount', async () => {
    const client = makeClient()
    const first = vi.fn()
    const second = vi.fn()
    const { rerender, unmount } = render(
      <WebSocketProvider client={client}>
        <Consumer onEvent={first} />
      </WebSocketProvider>,
    )
    rerender(
      <WebSocketProvider client={client}>
        <Consumer onEvent={second} />
      </WebSocketProvider>,
    )
    expect(client.handlerCount).toBe(1)
    FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].receive({ type: 'conversation_synced', conversation_id: 2 })
    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledTimes(1)

    unmount()
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(FakeWebSocket.live()).toHaveLength(0)
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('useWsEvent narrows to one type and receives resync after reconnect', () => {
    const client = makeClient()
    const resync = vi.fn()
    const synced = vi.fn()
    function C() {
      useWsEvent('resync', resync)
      useWsEvent('conversation_synced', (e) => synced(e.conversation_id))
      return null
    }
    render(<WebSocketProvider client={client}><C /></WebSocketProvider>)
    FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].receive({ type: 'conversation_synced', conversation_id: 9 })
    FakeWebSocket.instances[0].drop()
    act(() => { vi.advanceTimersByTime(1000) })
    FakeWebSocket.instances[1].open()
    expect(synced).toHaveBeenCalledWith(9)
    expect(resync).toHaveBeenCalledTimes(1)
  })
})
