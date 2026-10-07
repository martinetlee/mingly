import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react'
import type { ArchiveStatus } from '../../../api/types'
import { ApiError } from '../../../lib/http'
import { WebSocketClient } from '../../../lib/ws'
import { WebSocketProvider } from '../../../context/WebSocketContext'
import { resetToasts, subscribeToasts, type ToastItem } from '../../../lib/toast'
import { ConfirmProvider } from '../../../components/ui'

vi.mock('../../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../api/client')>()),
  getArchiveStatus: vi.fn(),
  startArchive: vi.fn(),
  pauseArchive: vi.fn(),
  getMcpSettings: vi.fn(),
  setMcpEnabled: vi.fn(),
  setMcpWriteEnabled: vi.fn(),
  generateMcpKey: vi.fn(),
  revealMcpKey: vi.fn(),
}))

import { getArchiveStatus, startArchive, pauseArchive, getMcpSettings, setMcpWriteEnabled } from '../../../api/client'
import ArchiveCard from '../ArchiveCard'
import McpSection from '../McpSection'

const status = (over: Partial<ArchiveStatus> = {}): ArchiveStatus => ({
  state: 'idle',
  flood_wait_until: null,
  dms: { total: 200, complete: 50, pending: 145, failed: 3, bots_skipped: 2 },
  current_chat: null,
  started_at: null,
  last_topup_at: null,
  groups_queued: 0,
  ...over,
})

function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

let toasts: ToastItem[] = []
let unsub: () => void = () => {}

function setup(ui: ReactNode) {
  const client = new WebSocketClient({ url: () => null })
  render(<WebSocketProvider client={client}><ConfirmProvider>{ui}</ConfirmProvider></WebSocketProvider>)
  return client
}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  unsub = subscribeToasts((t) => { toasts = t })
})
afterEach(() => {
  unsub()
  cleanup()
})

describe('ArchiveCard', () => {
  it('renders progress, counts and limits from the initial fetch', async () => {
    vi.mocked(getArchiveStatus).mockResolvedValue(status({ last_topup_at: '2026-10-06T10:00:00' }))
    setup(<ArchiveCard />)
    expect(await screen.findByText(/of 200 DMs complete/)).toBeTruthy()
    expect(screen.getByText('25%')).toBeTruthy()
    const bar = screen.getByRole('progressbar')
    expect(bar.getAttribute('aria-valuenow')).toBe('50')
    expect(screen.getByText('145')).toBeTruthy()
    expect(screen.getByText('3')).toBeTruthy()
    expect(screen.getByText('Bots skipped')).toBeTruthy()
    expect(screen.getByText(/Last top-up/)).toBeTruthy()
    expect(screen.getByText(/Secret chats/)).toBeTruthy()
    expect(screen.getByTestId('archive-state').textContent).toBe('Idle')
  })

  it('start is single-flight and applies the returned status', async () => {
    vi.mocked(getArchiveStatus).mockResolvedValue(status())
    const d = deferred<ArchiveStatus>()
    vi.mocked(startArchive).mockReturnValue(d.promise)
    setup(<ArchiveCard />)
    const start = await screen.findByRole('button', { name: /start/i })
    fireEvent.click(start)
    fireEvent.click(start)
    expect(startArchive).toHaveBeenCalledTimes(1)
    await act(async () => { d.resolve(status({ state: 'running', current_chat: { telegram_id: 7, name: 'Alice' } })) })
    expect(screen.getByTestId('archive-state').textContent).toBe('Archiving')
    expect(screen.getByText('Alice')).toBeTruthy()
    vi.mocked(pauseArchive).mockResolvedValue(status({ state: 'paused' }))
    fireEvent.click(screen.getByRole('button', { name: /pause/i }))
    await waitFor(() => expect(screen.getByTestId('archive-state').textContent).toBe('Paused'))
    expect(screen.getByRole('button', { name: /resume/i })).toBeTruthy()
  })

  it('updates live from archive_progress and shows the FloodWait resume time', async () => {
    vi.mocked(getArchiveStatus).mockResolvedValue(status())
    const client = setup(<ArchiveCard />)
    await screen.findByText(/of 200 DMs complete/)
    act(() => {
      client.dispatch({ type: 'archive_progress', ...status({ state: 'waiting_flood', flood_wait_until: '2026-10-06T10:05:00', dms: { total: 200, complete: 120, pending: 75, failed: 3, bots_skipped: 2 } }) })
    })
    expect(screen.getByText(/of 200 DMs complete/).textContent).toContain('120')
    expect(screen.getByTestId('archive-state').textContent).toBe('Waiting on Telegram rate limit')
    expect(screen.getByText(/Resuming at/)).toBeTruthy()
  })

  it('refetches on resync', async () => {
    vi.mocked(getArchiveStatus).mockResolvedValue(status())
    const client = setup(<ArchiveCard />)
    await screen.findByText(/of 200 DMs complete/)
    vi.mocked(getArchiveStatus).mockResolvedValue(status({ dms: { total: 200, complete: 200, pending: 0, failed: 0, bots_skipped: 2 } }))
    act(() => client.dispatch({ type: 'resync' }))
    await waitFor(() => expect(screen.getByText('100%')).toBeTruthy())
  })

  it('a failed start toasts and keeps the previous state', async () => {
    vi.mocked(getArchiveStatus).mockResolvedValue(status())
    vi.mocked(startArchive).mockRejectedValue(new ApiError('Telegram not connected', { status: 409, code: 'http', path: '/api/archive/start' }))
    setup(<ArchiveCard />)
    fireEvent.click(await screen.findByRole('button', { name: /start/i }))
    await waitFor(() => expect(toasts.some((t) => t.message.includes('Telegram not connected'))).toBe(true))
    expect(screen.getByTestId('archive-state').textContent).toBe('Idle')
  })
})

describe('McpSection write toggle', () => {
  it('turns write access on, and reverts with a toast when the save fails', async () => {
    vi.mocked(getArchiveStatus).mockResolvedValue(status())
    vi.mocked(getMcpSettings).mockResolvedValue({ enabled: true, key_set: true, url: 'http://127.0.0.1:8765/mcp', vault_unlocked: true, write_enabled: false })
    vi.mocked(setMcpWriteEnabled).mockResolvedValueOnce({ write_enabled: true })
    setup(<McpSection />)
    const sw = await screen.findByRole('switch', { name: 'Allow agent to write notes and context' })
    expect(screen.queryByText(/read-only/i)).toBeNull()
    fireEvent.click(sw)
    await waitFor(() => expect(sw.getAttribute('aria-checked')).toBe('true'))
    expect(setMcpWriteEnabled).toHaveBeenCalledWith(true)

    const d = deferred<{ write_enabled: boolean }>()
    vi.mocked(setMcpWriteEnabled).mockReturnValueOnce(d.promise.then(() => { throw new ApiError('Vault locked', { status: 423, code: 'http', path: '/api/settings/prefs/mcp/write' }) }))
    fireEvent.click(sw)
    // Optimistic while saving
    expect(sw.getAttribute('aria-checked')).toBe('false')
    await act(async () => { d.resolve({ write_enabled: false }) })
    await waitFor(() => expect(sw.getAttribute('aria-checked')).toBe('true'))
    expect(toasts.some((t) => t.message.includes('Vault locked'))).toBe(true)
  })
})
