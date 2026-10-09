import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { shouldNotify, notificationContent, setCachedNotifPref } from '../notifications'
import { WebSocketClient } from '../ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import NotificationBridge from '../../components/NotificationBridge'
import { FakeWebSocket } from '../../test/fakeWebSocket'

vi.mock('../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../api/client')>()),
  getPrefs: vi.fn(async () => ({ notif_pref: 'all' })),
}))

const msg = (over: Record<string, unknown> = {}) => ({
  type: 'new_message' as const, conversation_id: 1, message_id: 2, conversation_title: 'Acme', sender_name: 'Bob', text: 'hi', is_tracked: false, is_outgoing: false, ...over,
})

describe('notification policy', () => {
  it('matches the backend rule (all / tracked / off, never outgoing)', () => {
    expect(shouldNotify(msg(), 'all')).toBe(true)
    expect(shouldNotify(msg(), 'tracked')).toBe(false)
    expect(shouldNotify(msg({ is_tracked: true }), 'tracked')).toBe(true)
    expect(shouldNotify(msg({ is_tracked: true }), 'off')).toBe(false)
    expect(shouldNotify(msg({ is_outgoing: true }), 'all')).toBe(false)
  })

  it('formats title/body like the old Chats.tsx block', () => {
    expect(notificationContent(msg())).toEqual({ title: 'Acme', body: 'Bob: hi' })
    expect(notificationContent(msg({ conversation_title: undefined, sender_name: null }))).toEqual({ title: 'New message', body: 'hi' })
  })
})

describe('NotificationBridge', () => {
  const created: { title: string; body?: string }[] = []
  class FakeNotification {
    static permission = 'granted'
    onclick: (() => void) | null = null
    constructor(title: string, opts?: { body?: string }) { created.push({ title, body: opts?.body }) }
    close() {}
  }

  beforeEach(() => {
    created.length = 0
    FakeWebSocket.reset()
    vi.stubGlobal('Notification', FakeNotification)
    setCachedNotifPref('tracked')
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('raises exactly one notification per message no matter how many pages subscribe', async () => {
    const client = new WebSocketClient({ url: () => 'ws://t', WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket })
    render(
      <MemoryRouter>
        <WebSocketProvider client={client}>
          <NotificationBridge />
        </WebSocketProvider>
      </MemoryRouter>,
    )
    // Pref loaded from the server ('all'), not a never-written localStorage key
    await act(async () => { await Promise.resolve() })
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
    ws.open()
    ws.receive(msg())
    ws.receive(msg({ is_outgoing: true }))
    expect(created).toEqual([{ title: 'Acme', body: 'Bob: hi' }])
  })

  it('stays silent inside pywebview (backend sends native notifications)', async () => {
    vi.stubGlobal('pywebview', {})
    ;(window as unknown as { pywebview: unknown }).pywebview = {}
    const client = new WebSocketClient({ url: () => 'ws://t', WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket })
    render(<MemoryRouter><WebSocketProvider client={client}><NotificationBridge /></WebSocketProvider></MemoryRouter>)
    await act(async () => { await Promise.resolve() })
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
    ws.open()
    ws.receive(msg({ is_tracked: true }))
    expect(created).toEqual([])
    delete (window as unknown as { pywebview?: unknown }).pywebview
  })
})
