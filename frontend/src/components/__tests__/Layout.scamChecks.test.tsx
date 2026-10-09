/**
 * "Check for scam" can be started on any page that shows messages (Chats,
 * Mail, Contacts, Tracked): the terminal WS event must settle it wherever
 * the user is, so the router of those events is mounted once in Layout.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import type { WSEvent } from '../../api/types'
import { WebSocketClient } from '../../lib/ws'
import { WebSocketProvider } from '../../context/WebSocketContext'

vi.mock('../Sidebar', () => ({ default: () => null }))
vi.mock('../GlobalSearch', () => ({ default: () => null }))
vi.mock('../ShortcutHelp', () => ({ default: () => null }))
vi.mock('../../context/FeaturesContext', () => ({ useNav: () => ({ nav: [], shortcuts: [] }) }))
vi.mock('../../hooks/useLockVault', () => ({ useLockVault: () => ({ lock: async () => {}, locking: false }) }))
const checkMessagesForScam = vi.fn(async () => ({ status: 'queued', message_count: 1, queued_ids: [10], already_running_ids: [], missing_ids: [] }))
vi.mock('../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../api/client')>()),
  checkMessagesForScam: () => checkMessagesForScam(),
}))

import Layout from '../Layout'
import { __resetScamChecks, getScamCheck, startScamCheck } from '../../pages/chats/scamChecks'

afterEach(() => { cleanup(); __resetScamChecks() })

describe('Layout routes scam-check results app-wide', () => {
  it('a check started outside Chats (e.g. in Mail) settles on its terminal event', async () => {
    const ws = new WebSocketClient({ url: () => null })
    render(
      <WebSocketProvider client={ws}>
        <MemoryRouter initialEntries={['/mail']}>
          <Routes><Route path="/" element={<Layout />}><Route path="mail" element={<p>mail</p>} /></Route></Routes>
        </MemoryRouter>
      </WebSocketProvider>,
    )
    await act(async () => { await startScamCheck(7, 10) })
    expect(getScamCheck(7, 10)).toEqual({ status: 'checking' })
    act(() => {
      ws.dispatch({
        type: 'scam_detected', request_id: 'r', source: 'manual', conversation_id: 7, message_ids: [10], identity_ids: [5],
        pipeline_is_scam: true, confidence: 0.9, reason: '', category: null,
        messages: [{ conversation_id: 7, message_id: 10, identity_id: 5, scam_score: 0.9, scam_reason: null, scam_category: null, scam_manual_verdict: null, scam_status: 'completed', scam_error: null, effective_is_scam: true }],
      } as WSEvent)
    })
    expect(getScamCheck(7, 10)).toEqual({ status: 'done', result: 'scam' })
  })
})
