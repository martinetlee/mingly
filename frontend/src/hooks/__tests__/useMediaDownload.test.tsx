import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { ApiError } from '../../lib/http'
import { deferred } from '../../test/fixtures'

const downloadAttachment = vi.fn()
vi.mock('../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../api/client')>()),
  downloadAttachment: (...a: unknown[]) => downloadAttachment(...a),
}))

import { useMediaDownload, __resetMediaDownloads, MAX_ATTEMPTS, MAX_CONCURRENT, setMediaReady } from '../useMediaDownload'

const http = (status: number) => new ApiError(`HTTP ${status}`, { status, code: 'http', path: '/api/media-download' })
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

beforeEach(() => {
  __resetMediaDownloads()
  downloadAttachment.mockReset()
  vi.useFakeTimers()
})
afterEach(() => { vi.useRealTimers() })

describe('useMediaDownload', () => {
  it('retries transient failures a bounded number of times, then stops (no hot loop)', async () => {
    downloadAttachment.mockRejectedValue(http(502))
    const { result } = renderHook(() => useMediaDownload(10, { auto: true }))
    for (let i = 0; i < 10; i++) {
      await flush()
      await act(async () => { vi.advanceTimersByTime(5000) })
    }
    expect(downloadAttachment).toHaveBeenCalledTimes(MAX_ATTEMPTS)
    expect(result.current.status).toBe('error')
  })

  it('treats 503 (channel disconnected) as terminal: one request, no retries', async () => {
    downloadAttachment.mockRejectedValue(http(503))
    const { result } = renderHook(() => useMediaDownload(11, { auto: true }))
    await flush()
    await act(async () => { vi.advanceTimersByTime(30000) })
    expect(downloadAttachment).toHaveBeenCalledTimes(1)
    expect(result.current.status).toBe('error')
    // A re-render / remount does not restart it; an explicit retry does
    const again = renderHook(() => useMediaDownload(11, { auto: true }))
    await flush()
    expect(downloadAttachment).toHaveBeenCalledTimes(1)
    downloadAttachment.mockResolvedValueOnce({ url: '/m/11.jpg', kind: 'photo' })
    act(() => again.result.current.retry())
    await flush()
    expect(again.result.current.url).toBe('/m/11.jpg')
  })

  it('dedupes concurrent subscribers into one request', async () => {
    const d = deferred<{ url: string; kind: string }>()
    downloadAttachment.mockReturnValue(d.promise)
    const a = renderHook(() => useMediaDownload(12, { auto: true }))
    const b = renderHook(() => useMediaDownload(12, { auto: true }))
    await flush()
    expect(downloadAttachment).toHaveBeenCalledTimes(1)
    await act(async () => { d.resolve({ url: '/m/12.jpg', kind: 'photo' }) })
    expect(a.result.current.url).toBe('/m/12.jpg')
    expect(b.result.current.url).toBe('/m/12.jpg')
  })

  it('limits concurrency and drops queued items whose last viewer unmounted', async () => {
    const pending: ReturnType<typeof deferred<{ url: string; kind: string }>>[] = []
    downloadAttachment.mockImplementation(() => { const d = deferred<{ url: string; kind: string }>(); pending.push(d); return d.promise })
    const hooks = Array.from({ length: MAX_CONCURRENT + 2 }, (_, i) => renderHook(() => useMediaDownload(100 + i, { auto: true })))
    await flush()
    expect(downloadAttachment).toHaveBeenCalledTimes(MAX_CONCURRENT)
    hooks[MAX_CONCURRENT + 1].unmount() // queued, never started
    await act(async () => { pending[0].resolve({ url: '/m/a', kind: 'photo' }) })
    await flush()
    expect(downloadAttachment).toHaveBeenCalledTimes(MAX_CONCURRENT + 1)
    expect(downloadAttachment.mock.calls.map((c) => c[0])).not.toContain(100 + MAX_CONCURRENT + 1)
  })

  it('aborts an in-flight download when its last viewer unmounts', async () => {
    downloadAttachment.mockImplementation((_a: number, signal: AbortSignal) => new Promise((_, rej) => {
      signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')))
    }))
    const { unmount } = renderHook(() => useMediaDownload(13, { auto: true }))
    await flush()
    const signal = downloadAttachment.mock.calls[0][1] as AbortSignal
    unmount()
    expect(signal.aborted).toBe(true)
  })

  it('click-to-load media does not download until started; media_ready marks it ready', async () => {
    const { result } = renderHook(() => useMediaDownload(14))
    await flush()
    expect(downloadAttachment).not.toHaveBeenCalled()
    act(() => setMediaReady(14, '/m/14.mp4'))
    expect(result.current.url).toBe('/m/14.mp4')
    expect(result.current.status).toBe('ready')
  })
})
