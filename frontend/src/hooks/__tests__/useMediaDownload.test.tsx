import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { ApiError } from '../../lib/http'
import { deferred } from '../../pages/chats/__tests__/fixtures'

const downloadMedia = vi.fn()
vi.mock('../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../api/client')>()),
  downloadMedia: (...a: unknown[]) => downloadMedia(...a),
}))

import { useMediaDownload, __resetMediaDownloads, MAX_ATTEMPTS, MAX_CONCURRENT, setMediaReady } from '../useMediaDownload'

const http = (status: number) => new ApiError(`HTTP ${status}`, { status, code: 'http', path: '/api/media-download' })
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

beforeEach(() => {
  __resetMediaDownloads()
  downloadMedia.mockReset()
  vi.useFakeTimers()
})
afterEach(() => { vi.useRealTimers() })

describe('useMediaDownload', () => {
  it('retries transient failures a bounded number of times, then stops (no hot loop)', async () => {
    downloadMedia.mockRejectedValue(http(502))
    const { result } = renderHook(() => useMediaDownload(-1, 10, { auto: true }))
    for (let i = 0; i < 10; i++) {
      await flush()
      await act(async () => { vi.advanceTimersByTime(5000) })
    }
    expect(downloadMedia).toHaveBeenCalledTimes(MAX_ATTEMPTS)
    expect(result.current.status).toBe('error')
  })

  it('treats 503 (Telegram disconnected) as terminal: one request, no retries', async () => {
    downloadMedia.mockRejectedValue(http(503))
    const { result } = renderHook(() => useMediaDownload(-1, 11, { auto: true }))
    await flush()
    await act(async () => { vi.advanceTimersByTime(30000) })
    expect(downloadMedia).toHaveBeenCalledTimes(1)
    expect(result.current.status).toBe('error')
    // A re-render / remount does not restart it; an explicit retry does
    const again = renderHook(() => useMediaDownload(-1, 11, { auto: true }))
    await flush()
    expect(downloadMedia).toHaveBeenCalledTimes(1)
    downloadMedia.mockResolvedValueOnce({ media_url: '/m/11.jpg', media_type: 'photo' })
    act(() => again.result.current.retry())
    await flush()
    expect(again.result.current.url).toBe('/m/11.jpg')
  })

  it('dedupes concurrent subscribers into one request', async () => {
    const d = deferred<{ media_url: string; media_type: string }>()
    downloadMedia.mockReturnValue(d.promise)
    const a = renderHook(() => useMediaDownload(-1, 12, { auto: true }))
    const b = renderHook(() => useMediaDownload(-1, 12, { auto: true }))
    await flush()
    expect(downloadMedia).toHaveBeenCalledTimes(1)
    await act(async () => { d.resolve({ media_url: '/m/12.jpg', media_type: 'photo' }) })
    expect(a.result.current.url).toBe('/m/12.jpg')
    expect(b.result.current.url).toBe('/m/12.jpg')
  })

  it('limits concurrency and drops queued items whose last viewer unmounted', async () => {
    const pending: ReturnType<typeof deferred<{ media_url: string; media_type: string }>>[] = []
    downloadMedia.mockImplementation(() => { const d = deferred<{ media_url: string; media_type: string }>(); pending.push(d); return d.promise })
    const hooks = Array.from({ length: MAX_CONCURRENT + 2 }, (_, i) => renderHook(() => useMediaDownload(-1, 100 + i, { auto: true })))
    await flush()
    expect(downloadMedia).toHaveBeenCalledTimes(MAX_CONCURRENT)
    hooks[MAX_CONCURRENT + 1].unmount() // queued, never started
    await act(async () => { pending[0].resolve({ media_url: '/m/a', media_type: 'photo' }) })
    await flush()
    expect(downloadMedia).toHaveBeenCalledTimes(MAX_CONCURRENT + 1)
    expect(downloadMedia.mock.calls.map((c) => c[1])).not.toContain(100 + MAX_CONCURRENT + 1)
  })

  it('aborts an in-flight download when its last viewer unmounts', async () => {
    downloadMedia.mockImplementation((_c: number, _m: number, signal: AbortSignal) => new Promise((_, rej) => {
      signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')))
    }))
    const { unmount } = renderHook(() => useMediaDownload(-1, 13, { auto: true }))
    await flush()
    const signal = downloadMedia.mock.calls[0][2] as AbortSignal
    unmount()
    expect(signal.aborted).toBe(true)
  })

  it('click-to-load media does not download until started; media_ready marks it ready', async () => {
    const { result } = renderHook(() => useMediaDownload(-1, 14))
    await flush()
    expect(downloadMedia).not.toHaveBeenCalled()
    act(() => setMediaReady(-1, 14, '/m/14.mp4'))
    expect(result.current.url).toBe('/m/14.mp4')
    expect(result.current.status).toBe('ready')
  })
})
