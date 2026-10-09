/**
 * Shield page: server-driven verdicts, refetch after every mutation, WS
 * refetches that keep the active filters, keyset "Load more", and one
 * single-flight Cloud "Sync Now".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, waitFor, act, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ScannedMessage, ScamVerdictResponse } from '../../api/types'
import { WebSocketClient } from '../../lib/ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { ConfirmProvider } from '../../components/ui'
import { __resetFlaggedSendersForTests } from '../../hooks/useFlaggedSenders'

const row = (id: number, over: Partial<ScannedMessage> = {}): ScannedMessage => ({
  message_id: id, conversation_id: 100, conversation_title: 'Group', sender_identity_id: 500 + id, sender_name: `User ${id}`,
  text: `message ${id}`, date: '2026-10-06T09:00:00Z',
  scam_score: 0.2, scam_reason: null, scam_category: null, scam_manual_verdict: null,
  scam_status: 'completed', scam_error: null, effective_is_scam: false,
  scam_attempts: 1, scam_next_retry_at: null, scam_updated_at: null,
  llm_prompt: null, llm_response: null, trace_json: null, synced_to_cloud: false,
  ...over,
})

const api = {
  getScannedMessages: vi.fn(),
  getScamFlags: vi.fn(),
  setMessageVerdict: vi.fn(),
  setSenderVerdict: vi.fn(),
  getScamReports: vi.fn(async () => ({ reports: [] })),
  getSyncHistory: vi.fn(),
  getShieldCloudSettings: vi.fn(async () => ({ cloud_url: 'https://cloud.example', api_key_set: true, last_synced_at: null, sync_dismissals: false })),
  triggerSync: vi.fn(),
  checkMessagesForScam: vi.fn(),
}

vi.mock('../../api/client', () => {
  const pass = (k: keyof typeof api) => (...a: unknown[]) => (api[k] as (...x: unknown[]) => unknown)(...a)
  return {
    getScannedMessages: pass('getScannedMessages'),
    getScamFlags: pass('getScamFlags'),
    setMessageVerdict: pass('setMessageVerdict'),
    setSenderVerdict: pass('setSenderVerdict'),
    getScamReports: pass('getScamReports'),
    getSyncHistory: pass('getSyncHistory'),
    getShieldCloudSettings: pass('getShieldCloudSettings'),
    triggerSync: pass('triggerSync'),
    checkMessagesForScam: pass('checkMessagesForScam'),
    setShieldCloudSettings: vi.fn(async () => ({ success: true })),
    getScamDetectionSettings: vi.fn(async () => ({ mode: 'local_llm', api_url: '', api_key: '', api_key_set: false, max_tokens: 200 })),
    setScamDetectionSettings: vi.fn(),
    triggerMassScan: vi.fn(),
    exportScannedMessages: vi.fn(),
    submitFeedback: vi.fn(),
    getScamPipeline: vi.fn(),
    reloadScamPipeline: vi.fn(),
  }
})

import Shield from '../Shield'
import ScannedTable from '../shield/ScannedTable'
import { useScannedList } from '../shield/useScannedList'
import { useRescan } from '../shield/useRescan'

const history = (over: Record<string, unknown> = {}) => ({
  last_synced_at: null, last_sync_status: null, last_sync_error: null, last_sync_details: null, running: false,
  flags: { total: 0, synced: 0, pending: 0 }, reports: { total: 0, synced: 0, pending: 0 },
  scans: { total: 0, synced: 0, pending: 0, total_scanned: 0, scam_detected: 0, failed: 0 },
  dismissals: { total: 0, synced: 0, pending: 0 }, analytics: { total: 0, synced: 0, pending: 0, latest: null },
  ...over,
})

function renderShield() {
  const client = new WebSocketClient({ url: () => null })
  const ui = (children: ReactNode) => (
    <ConfirmProvider>
      <WebSocketProvider client={client}>
        <MemoryRouter>{children}</MemoryRouter>
      </WebSocketProvider>
    </ConfirmProvider>
  )
  render(ui(<Shield />))
  return { client }
}

const scannedCalls = () => api.getScannedMessages.mock.calls.map((c) => c[0] as { cursor?: string; pageSize?: number; filters?: Record<string, unknown> })

async function openScanned() {
  fireEvent.click(screen.getByRole('button', { name: /Scanned Messages/ }))
  await screen.findByText('message 1')
}

beforeEach(() => {
  __resetFlaggedSendersForTests()
  Object.values(api).forEach((f) => f.mockClear())
  api.getScannedMessages.mockImplementation(async () => ({
    total: 3, page: 1, page_size: 200, next_cursor: null,
    messages: [
      row(1),
      row(2, { scam_status: 'failed', scam_score: null, scam_error: 'classify failed' }),
      row(3, { scam_score: 0.9, effective_is_scam: false, scam_manual_verdict: 'not_scam' }),
    ],
  }))
  api.getScamFlags.mockResolvedValue({ flags: [], flagged_identity_ids: [], flagged_person_ids: [], flagged_conversation_ids: [] })
  api.getSyncHistory.mockResolvedValue(history())
})

describe('Shield verdict rendering', () => {
  it('renders verdicts from server fields: failed is failed (not safe), manual not_scam beats a high score', async () => {
    renderShield()
    await openScanned()
    const failedRow = screen.getByText('message 2').closest('tr')!
    expect(within(failedRow).getByText('Scan failed')).toBeTruthy()
    expect(within(failedRow).queryByText('Safe')).toBeNull()
    expect(within(failedRow).getByTitle(/Retry scan/)).toBeTruthy()

    const overridden = screen.getByText('message 3').closest('tr')!
    expect(within(overridden).getByText('Safe')).toBeTruthy()
    expect(within(overridden).getByText('Marked safe')).toBeTruthy()
    expect(within(overridden).queryByText('Scam')).toBeNull()
  })
})

describe('Shield mutations', () => {
  it('bulk "Mark as not scam" sends exactly the selected rows, then refetches scanned + flags', async () => {
    const resp: ScamVerdictResponse = {
      status: 'updated', scope: 'message', verdict: 'not_scam', count: 2,
      messages: [], flags: [{ identity_id: 501, status: 'dismissed' }],
    }
    api.setMessageVerdict.mockResolvedValue(resp)
    renderShield()
    await openScanned()
    await waitFor(() => expect(api.getScamFlags).toHaveBeenCalled())

    fireEvent.click(screen.getByText('User 1'))
    fireEvent.click(screen.getByText('User 3'), { shiftKey: true })
    expect(screen.getByText('3 selected')).toBeTruthy()

    const scannedBefore = api.getScannedMessages.mock.calls.length
    const flagsBefore = api.getScamFlags.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Mark as not scam' }))

    await waitFor(() => expect(api.setMessageVerdict).toHaveBeenCalledTimes(1))
    expect(api.setMessageVerdict.mock.calls[0]).toEqual([
      [1, 2, 3].map((id) => ({ message_id: id, conversation_id: 100 })),
      'not_scam',
    ])
    await waitFor(() => expect(api.getScannedMessages.mock.calls.length).toBeGreaterThan(scannedBefore))
    await waitFor(() => expect(api.getScamFlags.mock.calls.length).toBeGreaterThan(flagsBefore))
    await waitFor(() => expect(screen.queryByText(/selected$/)).toBeNull())
  })

  it('WS scam events refetch with the active verdict filter (coalesced)', async () => {
    const { client } = renderShield()
    await openScanned()
    fireEvent.click(screen.getByRole('button', { name: 'Failed' }))
    await waitFor(() => expect(scannedCalls().some((c) => c.filters?.verdict_filter === 'failed')).toBe(true))
    const before = api.getScannedMessages.mock.calls.length

    const ev = { request_id: 'r', source: 'realtime' as const, conversation_id: 100, message_ids: [9], identity_ids: [], messages: [], error: 'x' }
    act(() => {
      client.dispatch({ type: 'scam_check_error', ...ev })
      client.dispatch({ type: 'scam_check_error', ...ev })
      client.dispatch({ type: 'scam_check_error', ...ev })
    })
    await waitFor(() => expect(api.getScannedMessages.mock.calls.length).toBeGreaterThan(before))
    const after = scannedCalls().slice(before)
    expect(after.length).toBeLessThanOrEqual(2) // leading + at most one trailing
    after.forEach((c) => expect(c.filters?.verdict_filter).toBe('failed'))
  })

  it('applies WS row states immediately (failed rescan flips the pill)', async () => {
    const { client } = renderShield()
    await openScanned()
    act(() => client.dispatch({
      type: 'scam_check_error', request_id: 'r', source: 'manual', conversation_id: 100, message_ids: [1], identity_ids: [501], error: 'boom',
      messages: [{ conversation_id: 100, message_id: 1, identity_id: 501, scam_score: null, scam_reason: null, scam_category: null,
        scam_manual_verdict: null, scam_status: 'failed', scam_error: 'boom', effective_is_scam: false }],
    }))
    const r1 = screen.getByText('message 1').closest('tr')!
    expect(within(r1).getByText('Scan failed')).toBeTruthy()
  })
})

describe('Shield keyset pagination', () => {
  // Drives the real list hook + table with a 2-row page size, so the test
  // exercises cursors without rendering hundreds of rows
  function PagedScanned() {
    const list = useScannedList({}, { pageSize: 2 })
    const rescan = useRescan()
    return <ScannedTable list={list} devMode={false} rescan={rescan} emptyTitle="empty" />
  }

  it('"Load more" passes next_cursor, and a later refetch keeps every loaded row', async () => {
    const all = [row(1), row(2), row(3)]
    api.getScannedMessages.mockImplementation(async (opts: { cursor?: string; pageSize?: number }) => {
      const size = opts.pageSize ?? 200
      const start = opts.cursor ? Number(opts.cursor) : 0
      const page = all.slice(start, start + size)
      const end = start + page.length
      return { total: all.length, page: 1, page_size: size, next_cursor: end < all.length ? String(end) : null, messages: page }
    })
    const client = new WebSocketClient({ url: () => null })
    render(
      <ConfirmProvider>
        <WebSocketProvider client={client}>
          <MemoryRouter><PagedScanned /></MemoryRouter>
        </WebSocketProvider>
      </ConfirmProvider>,
    )
    await screen.findByText('Showing 2 of 3')
    expect(screen.queryByText('message 3')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
    await screen.findByText('Showing 3 of 3')
    expect(screen.getByText('message 3')).toBeTruthy()
    expect(scannedCalls().some((c) => c.cursor === '2')).toBe(true)
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()

    const before = api.getScannedMessages.mock.calls.length
    act(() => client.dispatch({ type: 'resync' }))
    await waitFor(() => expect(api.getScannedMessages.mock.calls.length).toBeGreaterThan(before))
    // The refetch starts from the top (no cursor/offset) and asks for every loaded row
    const refetch = scannedCalls().slice(before)
    expect(refetch[0].cursor).toBeFalsy()
    expect(refetch[0].pageSize).toBe(3)
    await waitFor(() => expect(api.getScannedMessages).toHaveBeenCalledTimes(before + 1))
    expect(screen.getByText('Showing 3 of 3')).toBeTruthy()
    expect(screen.getByText('message 3')).toBeTruthy()
  })
})

describe('Shield cloud sync', () => {
  it('both Sync Now buttons share one in-flight sync and show the same result', async () => {
    let finish!: (v: unknown) => void
    api.triggerSync.mockImplementation(() => new Promise((r) => { finish = r }))
    renderShield()
    fireEvent.click(screen.getByTitle('Detection settings'))
    fireEvent.click(screen.getByRole('button', { name: /Sync History/ }))
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Sync Now' })).toHaveLength(2))
    const buttons = screen.getAllByRole('button', { name: 'Sync Now' })
    await waitFor(() => buttons.forEach((b) => expect((b as HTMLButtonElement).disabled).toBe(false)))

    fireEvent.click(buttons[0])
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Syncing…' })).toHaveLength(2))
    screen.getAllByRole('button', { name: 'Syncing…' }).forEach((b) => fireEvent.click(b))
    expect(api.triggerSync).toHaveBeenCalledTimes(1)

    await act(async () => { finish({ status: 'success', flags_submitted: 2, files_updated: [] }) })
    await waitFor(() => expect(screen.getAllByText('Synced — uploaded 2 flags')).toHaveLength(2))
  })

  it('a server-side sync already running disables Sync Now', async () => {
    api.getSyncHistory.mockResolvedValue(history({ running: true }))
    renderShield()
    fireEvent.click(screen.getByRole('button', { name: /Sync History/ }))
    const btn = await screen.findByRole('button', { name: 'Syncing…' })
    expect((btn as HTMLButtonElement).disabled).toBe(true)
  })
})
