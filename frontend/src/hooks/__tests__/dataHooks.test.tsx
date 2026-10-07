import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useLatestQuery } from '../useLatestQuery'
import { useDebouncedQuery } from '../useDebouncedQuery'
import { useAsyncAction } from '../useAsyncAction'
import { useSettingField } from '../useSettingField'
import { useCoalescedRefetch } from '../useCoalescedRefetch'
import { resetToasts, subscribeToasts } from '../../lib/toast'
import type { ToastItem } from '../../lib/toast'

/** A promise you resolve/reject by hand, recording the abort signal. */
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function currentToasts(): ToastItem[] {
  let items: ToastItem[] = []
  subscribeToasts((t) => { items = t })()
  return items
}

beforeEach(() => resetToasts())
afterEach(() => vi.useRealTimers())

describe('useLatestQuery', () => {
  it('drops a slow response for a key that is no longer current (chat A → B)', async () => {
    const calls: Record<string, ReturnType<typeof deferred<string>>> = {}
    const signals: Record<string, AbortSignal> = {}
    const fetcherFor = (id: string) => (signal: AbortSignal) => {
      calls[id] = deferred<string>()
      signals[id] = signal
      return calls[id].promise
    }
    const { result, rerender } = renderHook(({ id }) => useLatestQuery(['chat', id], fetcherFor(id)), {
      initialProps: { id: 'A' },
    })
    expect(result.current.loading).toBe(true)
    rerender({ id: 'B' })
    expect(signals.A.aborted).toBe(true)

    await act(async () => { calls.B.resolve('B-messages') })
    expect(result.current.data).toBe('B-messages')
    // A's response arrives late — must not render under B
    await act(async () => { calls.A.resolve('A-messages') })
    expect(result.current.data).toBe('B-messages')
    expect(result.current.loading).toBe(false)
  })

  it('never returns the previous key\'s data unless keepPreviousData', async () => {
    const { result, rerender } = renderHook(({ id }) => useLatestQuery(id, async () => `data-${id}`), {
      initialProps: { id: 1 },
    })
    await waitFor(() => expect(result.current.data).toBe('data-1'))
    rerender({ id: 2 })
    // Same render as the key change: old data is hidden immediately
    expect(result.current.data).toBeUndefined()
    await waitFor(() => expect(result.current.data).toBe('data-2'))

    const kept = renderHook(({ id }) => useLatestQuery(id, async () => `d${id}`, { keepPreviousData: true }), { initialProps: { id: 1 } })
    await waitFor(() => expect(kept.result.current.data).toBe('d1'))
    kept.rerender({ id: 2 })
    expect(kept.result.current.data).toBe('d1')
    expect(kept.result.current.isStale).toBe(true)
    await waitFor(() => expect(kept.result.current.isStale).toBe(false))
  })

  it('refetch is latest-only; setData supersedes an in-flight older read', async () => {
    const pending: ReturnType<typeof deferred<number>>[] = []
    const { result } = renderHook(() => useLatestQuery('board', () => {
      const d = deferred<number>()
      pending.push(d)
      return d.promise
    }))
    await act(async () => { pending[0].resolve(1) })
    expect(result.current.data).toBe(1)

    act(() => { void result.current.refetch() }) // a WS-triggered read starts…
    act(() => result.current.setData(42))        // …then the optimistic move lands
    await act(async () => { pending[1].resolve(7) }) // the older read must not snap it back
    expect(result.current.data).toBe(42)

    let r: number | undefined
    await act(async () => {
      const p = result.current.refetch()
      pending[2].resolve(99)
      r = await p
    })
    expect(r).toBe(99)
    expect(result.current.data).toBe(99)
  })

  it('exposes errors for the current key only and ignores aborts', async () => {
    const { result, rerender } = renderHook(({ k }) => useLatestQuery(k, async () => {
      if (k === 'bad') throw new Error('nope')
      return 'ok'
    }), { initialProps: { k: 'bad' } })
    await waitFor(() => expect((result.current.error as Error)?.message).toBe('nope'))
    rerender({ k: 'good' })
    expect(result.current.error).toBeNull()
    await waitFor(() => expect(result.current.data).toBe('ok'))
  })

  it('does not fetch while disabled', async () => {
    const fetcher = vi.fn(async () => 1)
    const { result, rerender } = renderHook(({ on }) => useLatestQuery('x', fetcher, { enabled: on }), { initialProps: { on: false } })
    expect(fetcher).not.toHaveBeenCalled()
    expect(result.current.loading).toBe(false)
    rerender({ on: true })
    await waitFor(() => expect(result.current.data).toBe(1))
  })
})

describe('useDebouncedQuery', () => {
  it('debounces typing and only applies the latest query\'s results', async () => {
    vi.useFakeTimers()
    const resolvers: Record<string, (v: string[]) => void> = {}
    const fetcher = vi.fn((q: string) => new Promise<string[]>((res) => { resolvers[q] = res }))
    const { result, rerender } = renderHook(({ q }) => useDebouncedQuery(q, fetcher, { delayMs: 200 }), { initialProps: { q: '' } })
    expect(result.current.pending).toBe(false)
    rerender({ q: 'a' })
    expect(result.current.pending).toBe(true)
    rerender({ q: 'al' })
    rerender({ q: 'ali' })
    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher.mock.calls[0][0]).toBe('ali')

    rerender({ q: 'alic' })
    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    expect(fetcher).toHaveBeenCalledTimes(2)
    await act(async () => { resolvers.alic(['Alice']) })
    await act(async () => { resolvers.ali(['Ali', 'Alice']) }) // out of order — ignored
    expect(result.current.data).toEqual(['Alice'])
    expect(result.current.debouncedQuery).toBe('alic')

    rerender({ q: '' })
    expect(result.current.data).toBeUndefined()
  })
})

describe('useAsyncAction', () => {
  it('is single-flight: a double click runs the action once', async () => {
    const d = deferred<string>()
    const fn = vi.fn(() => d.promise)
    const { result } = renderHook(() => useAsyncAction(fn))
    let p1!: Promise<string | undefined>, p2!: Promise<string | undefined>
    act(() => {
      p1 = result.current.run()
      p2 = result.current.run()
    })
    expect(fn).toHaveBeenCalledTimes(1)
    expect(result.current.pending).toBe(true)
    await act(async () => { d.resolve('done') })
    expect(await p1).toBe('done')
    expect(await p2).toBe('done')
    expect(result.current.pending).toBe(false)
  })

  it('never rejects; exposes error and toasts with errorContext', async () => {
    const { result } = renderHook(() => useAsyncAction(async () => { throw new Error('boom') }, { errorContext: 'Forward failed' }))
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    let r: unknown = 'x'
    await act(async () => { r = await result.current.run() })
    expect(r).toBeUndefined()
    expect((result.current.error as Error).message).toBe('boom')
    expect(currentToasts().map((t) => t.message)).toEqual(['Forward failed: boom'])
    spy.mockRestore()
  })
})

describe('useSettingField', () => {
  it('debounces keystrokes into one write and adopts the server echo', async () => {
    vi.useFakeTimers()
    const set = vi.fn(async (v: number) => ({ value: Math.min(v, 4000) }))
    const { result } = renderHook(() => useSettingField({
      initial: 1000, serverValue: 1000, set, debounceMs: 300,
      echo: (r) => (r as { value: number }).value,
    }))
    act(() => result.current.setValue(8))
    act(() => result.current.setValue(80))
    act(() => result.current.setValue(8000))
    expect(result.current.value).toBe(8000) // no per-keystroke clamping
    await act(async () => { await vi.advanceTimersByTimeAsync(300) })
    expect(set).toHaveBeenCalledTimes(1)
    expect(set).toHaveBeenCalledWith(8000)
    expect(result.current.value).toBe(4000) // server-normalised value shown
  })

  it('serializes writes: never two in flight, latest value wins', async () => {
    const pending: ReturnType<typeof deferred<void>>[] = []
    const set = vi.fn(() => { const d = deferred<void>(); pending.push(d); return d.promise })
    const { result } = renderHook(() => useSettingField({ initial: 'a', serverValue: 'a', set, debounceMs: 0 }))
    act(() => result.current.setValue('b'))
    act(() => result.current.setValue('c'))
    act(() => result.current.setValue('d'))
    expect(set).toHaveBeenCalledTimes(1)
    await act(async () => { pending[0].resolve() })
    expect(set).toHaveBeenCalledTimes(2)
    expect(set).toHaveBeenLastCalledWith('d')
    await act(async () => { pending[1].resolve() })
    expect(result.current.value).toBe('d')
    expect(result.current.saving).toBe(false)
  })

  it('reverts to the last saved value and toasts on failure', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const set = vi.fn(async () => { throw new Error('Invalid time') })
    const { result } = renderHook(() => useSettingField({ initial: '08:00', serverValue: '08:00', set, debounceMs: 0, label: 'briefing time' }))
    await act(async () => { result.current.setValue('25:00') })
    await waitFor(() => expect(result.current.value).toBe('08:00'))
    expect(currentToasts().map((t) => t.message)).toEqual(["Couldn't save briefing time: Invalid time"])
    spy.mockRestore()
  })

  it('blocks invalid values and reverts them on flush', async () => {
    const set = vi.fn(async () => {})
    const { result } = renderHook(() => useSettingField({
      initial: 2000, serverValue: 2000, set, debounceMs: 0,
      validate: (v: number) => (v < 256 ? 'Too small' : null),
    }))
    act(() => result.current.setValue(25))
    expect(result.current.invalid).toBe('Too small')
    expect(set).not.toHaveBeenCalled()
    act(() => result.current.flush())
    expect(result.current.value).toBe(2000)
    expect(result.current.invalid).toBeNull()
  })

  it('adopts a late server value unless an edit is pending', async () => {
    const set = vi.fn(async () => {})
    const { result, rerender } = renderHook(({ sv }) => useSettingField({ initial: 'x', serverValue: sv, set }), {
      initialProps: { sv: undefined as string | undefined },
    })
    expect(result.current.value).toBe('x')
    rerender({ sv: 'server' })
    expect(result.current.value).toBe('server')
    act(() => result.current.setValue('typing'))
    rerender({ sv: 'server2' })
    expect(result.current.value).toBe('typing')
  })
})

describe('useCoalescedRefetch', () => {
  it('runs immediately once, then collapses a burst into one trailing call', async () => {
    vi.useFakeTimers()
    const fn = vi.fn()
    const { result } = renderHook(() => useCoalescedRefetch(fn, 1000))
    await act(async () => {
      for (let i = 0; i < 20; i++) result.current()
      await Promise.resolve()
    })
    expect(fn).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(fn).toHaveBeenCalledTimes(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(fn).toHaveBeenCalledTimes(2)
  })

  it('does not overlap a slow async fn and stops after unmount', async () => {
    vi.useFakeTimers()
    const d = deferred<void>()
    const fn = vi.fn(() => d.promise)
    const { result, unmount } = renderHook(() => useCoalescedRefetch(fn, 100))
    await act(async () => { result.current(); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(500); result.current() })
    expect(fn).toHaveBeenCalledTimes(1) // still running → queued, not overlapped
    unmount()
    await act(async () => { d.resolve(); await vi.advanceTimersByTimeAsync(1000) })
    expect(fn).toHaveBeenCalledTimes(1)
  })
})
