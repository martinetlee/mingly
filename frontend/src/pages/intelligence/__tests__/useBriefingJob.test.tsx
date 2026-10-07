import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { controllableStream, type ControllableStream } from './testStream'

const streams: { path: string; s: ControllableStream }[] = []
const getBriefingStatus = vi.fn()
const cancelBriefing = vi.fn()

let failNextStream: unknown = null

vi.mock('../../../api/client', async () => ({
  ApiError: (await vi.importActual<typeof import('../../../lib/http')>('../../../lib/http')).ApiError,
  stream: (path: string, opts?: { signal?: AbortSignal }) => {
    if (failNextStream) {
      const err = failNextStream
      failNextStream = null
      return (async function* () { throw err })()
    }
    const s = controllableStream()
    streams.push({ path, s })
    return s.gen(opts)
  },
  getBriefingStatus: () => getBriefingStatus(),
  cancelBriefing: () => cancelBriefing(),
  isAbortError: (e: unknown) => e instanceof DOMException && e.name === 'AbortError',
}))

import { briefingReducer, initialBriefingState, useBriefingJob, useRegenerateEntry, describeBriefingWindow } from '../useBriefingJob'
import { ApiError } from '../../../lib/http'

beforeEach(() => {
  streams.length = 0
  getBriefingStatus.mockReset()
  cancelBriefing.mockReset()
})

describe('briefingReducer', () => {
  it('resets on begin (attach replays everything) and accumulates chat_done / chat_error', () => {
    let s = briefingReducer({ ...initialBriefingState, live: [{ chat_id: 9 } as never] }, { type: 'begin', days: 3, attached: true })
    expect(s).toMatchObject({ running: true, attached: true, days: 3, live: [] })
    s = briefingReducer(s, { type: 'event', event: 'progress', data: { current: 0, total: 4, stage: 'syncing' } })
    expect(s.progress?.stage).toBe('syncing')
    s = briefingReducer(s, { type: 'event', event: 'start', data: { total: 2, days: 3, run_id: 11 } })
    expect(s.runId).toBe(11)
    s = briefingReducer(s, { type: 'event', event: 'chat_done', data: { chat_id: 1, chat_name: 'A', category: 'tracked', summary: '**x** [1]', sources: [] } })
    s = briefingReducer(s, { type: 'event', event: 'chat_error', data: { chat_id: 2, chat_name: 'B', message: 'timeout' } })
    expect(s.live).toHaveLength(1)
    expect(s.failed).toEqual([{ chat_id: 2, chat_name: 'B', message: 'timeout' }])
    s = briefingReducer(s, { type: 'end' })
    expect(s.running).toBe(false)
  })
})

describe('useBriefingJob', () => {
  it('attaches to a running job on request (attach=1) and keeps Generate single-flight', async () => {
    getBriefingStatus.mockResolvedValue({ running: true, days: 7, job_id: 'j', kind: 'briefing', started_at: 0, trigger: 'scheduled', progress: {} })
    const onFinished = vi.fn()
    const { result } = renderHook(() => useBriefingJob(onFinished))
    await act(async () => { await result.current.attachIfRunning() })
    await waitFor(() => expect(streams).toHaveLength(1))
    expect(streams[0].path).toBe('/api/intelligence/briefing?days=7&attach=1')
    expect(result.current.state.running).toBe(true)

    // A Generate click while attached must not start a second job
    act(() => result.current.start(1))
    expect(streams).toHaveLength(1)

    await act(async () => {
      streams[0].s.push('done', { run_id: 5, status: 'done', entries: 2, failed: 0 })
    })
    await waitFor(() => expect(onFinished).toHaveBeenCalledWith({ kind: 'done', runId: 5, status: 'done', entries: 2, failed: 0 }))
    expect(result.current.state.running).toBe(false)
  })

  it('does not attach when nothing is running', async () => {
    getBriefingStatus.mockResolvedValue({ running: false })
    const { result } = renderHook(() => useBriefingJob(vi.fn()))
    await act(async () => { await result.current.attachIfRunning() })
    expect(streams).toHaveLength(0)
    expect(result.current.state.running).toBe(false)
  })

  it('job finished between status check and attach: idle reply ends quietly, never starts a run', async () => {
    getBriefingStatus.mockResolvedValueOnce({ running: true, days: 3, job_id: 'j', kind: 'briefing', started_at: 0, trigger: 'manual', progress: {} })
    const onFinished = vi.fn()
    const { result } = renderHook(() => useBriefingJob(onFinished))
    await act(async () => { await result.current.attachIfRunning() })
    await waitFor(() => expect(streams).toHaveLength(1))
    expect(streams[0].path).toContain('attach=1')
    await act(async () => { streams[0].s.push('idle', {}); streams[0].s.end() })
    await waitFor(() => expect(result.current.state.running).toBe(false))
    expect(streams).toHaveLength(1) // no non-attach request was made
    expect(onFinished).not.toHaveBeenCalled()
    expect(getBriefingStatus).toHaveBeenCalledTimes(1) // idle is terminal: no re-attach loop
  })

  it('stops listening on unmount without cancelling the server job', async () => {
    const { result, unmount } = renderHook(() => useBriefingJob(vi.fn()))
    act(() => result.current.start(1))
    await waitFor(() => expect(streams[0]?.s.signal).toBeDefined())
    unmount()
    expect(streams[0].s.signal!.aborted).toBe(true)
    expect(cancelBriefing).not.toHaveBeenCalled()
  })

  it('Stop calls the cancel endpoint; the stream reports cancelled', async () => {
    cancelBriefing.mockResolvedValue({ cancelled: true })
    const onFinished = vi.fn()
    const { result } = renderHook(() => useBriefingJob(onFinished))
    act(() => result.current.start(1))
    await waitFor(() => expect(streams).toHaveLength(1))
    await act(async () => { await result.current.cancel() })
    expect(cancelBriefing).toHaveBeenCalledTimes(1)
    await act(async () => { streams[0].s.push('cancelled', {}) })
    await waitFor(() => expect(onFinished).toHaveBeenCalledWith({ kind: 'cancelled' }))
  })

  it('re-attaches when the stream drops while the job is still running', async () => {
    getBriefingStatus.mockResolvedValue({ running: true, days: 1, job_id: 'j', kind: 'briefing', started_at: 0, trigger: 'manual', progress: {} })
    const { result } = renderHook(() => useBriefingJob(vi.fn()))
    act(() => result.current.start(1))
    await waitFor(() => expect(streams).toHaveLength(1))
    await act(async () => { streams[0].s.end() })
    await waitFor(() => expect(streams).toHaveLength(2))
    expect(streams[1].path).toContain('attach=1')
    expect(result.current.state.running).toBe(true)
  })
})

describe('briefing_running conflict (409)', () => {
  it('a 409 briefing_running shows a conflict (not an error); Cancel & run is one atomic replace=1 request', async () => {
    failNextStream = new ApiError('A 1-day briefing is already running.', {
      status: 409, code: 'http', path: '/api/intelligence/briefing?days=7', serverCode: 'briefing_running',
      body: { detail: 'A 1-day briefing is already running.', code: 'briefing_running', running_days: 1, requested_days: 7, trigger: 'scheduled' },
    })
    const onFinished = vi.fn()
    const { result } = renderHook(() => useBriefingJob(onFinished))
    act(() => result.current.start(7))
    await waitFor(() => expect(result.current.state.conflict).not.toBeNull())
    expect(result.current.state).toMatchObject({
      running: false, error: '', conflict: { runningDays: 1, requestedDays: 7, trigger: 'scheduled' },
    })
    expect(onFinished).not.toHaveBeenCalled()
    expect(streams).toHaveLength(0)

    // Synchronous: the replace stream is opened in the same tick, no timers involved
    act(() => result.current.cancelAndRun(7))
    expect(streams).toHaveLength(1)
    expect(streams[0].path).toBe('/api/intelligence/briefing?days=7&replace=1')
    expect(result.current.state).toMatchObject({ running: true, conflict: null })
    expect(cancelBriefing).not.toHaveBeenCalled()
    expect(getBriefingStatus).not.toHaveBeenCalled()

    // A second click while the replacement runs does nothing
    act(() => result.current.cancelAndRun(7))
    expect(streams).toHaveLength(1)

    await act(async () => {
      streams[0].s.push('start', { total: 2, days: 7, run_id: 9, replaced_job_id: 'old', attached: false })
      streams[0].s.push('done', { run_id: 9, status: 'done', entries: 2, failed: 0 })
    })
    await waitFor(() => expect(onFinished).toHaveBeenCalledWith({ kind: 'done', runId: 9, status: 'done', entries: 2, failed: 0 }))
  })

  it('an SSE error event with code briefing_running is a conflict too', async () => {
    const onFinished = vi.fn()
    const { result } = renderHook(() => useBriefingJob(onFinished))
    act(() => result.current.start(7))
    await waitFor(() => expect(streams).toHaveLength(1))
    await act(async () => { streams[0].s.push('error', { message: 'busy', code: 'briefing_running', running_days: 7 }) })
    await waitFor(() => expect(onFinished).toHaveBeenCalledWith({ kind: 'conflict' }))
    expect(result.current.state).toMatchObject({ running: false, error: '', conflict: { runningDays: 7 } })
  })

  it('describes the running window', () => {
    expect(describeBriefingWindow(1)).toBe('A daily briefing')
    expect(describeBriefingWindow(7)).toBe('A weekly briefing')
    expect(describeBriefingWindow(3)).toBe('A 3-day briefing')
    expect(describeBriefingWindow(null)).toBe('A briefing')
  })
})

describe('useRegenerateEntry', () => {
  it('an older aborted run never clears the state of the newer one', async () => {
    const onUpdated = vi.fn()
    const { result } = renderHook(() => useRegenerateEntry(onUpdated))

    let first!: Promise<void>
    act(() => { first = result.current.regenerate(1, 10) })
    await waitFor(() => expect(result.current.activeEntryId).toBe(10))

    // Single-flight: a second regenerate while one runs is ignored
    act(() => { void result.current.regenerate(1, 20) })
    expect(streams).toHaveLength(1)

    act(() => result.current.stop())
    expect(result.current.activeEntryId).toBeNull()

    let second!: Promise<void>
    act(() => { second = result.current.regenerate(1, 20) })
    await waitFor(() => expect(result.current.activeEntryId).toBe(20))

    // The first run's generator settles (aborted) after the second started
    await act(async () => { await first })
    expect(result.current.activeEntryId).toBe(20)

    await act(async () => {
      streams[1].s.push('done', { entry: { id: 20, chat_id: 1, chat_name: 'X', category: 'tracked', summary: 'new', sources: [] } })
      await second
    })
    expect(onUpdated).toHaveBeenCalledWith(1, expect.objectContaining({ id: 20, summary: 'new' }))
    expect(result.current.activeEntryId).toBeNull()
  })

  it('aborts the regeneration on unmount', async () => {
    const { result, unmount } = renderHook(() => useRegenerateEntry(vi.fn()))
    act(() => { void result.current.regenerate(1, 10) })
    await waitFor(() => expect(streams[0]?.s.signal).toBeDefined())
    unmount()
    expect(streams[0].s.signal!.aborted).toBe(true)
  })
})
