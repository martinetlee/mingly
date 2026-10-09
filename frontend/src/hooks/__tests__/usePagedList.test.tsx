/**
 * usePagedList: latest-only pages per key, local patches that a read issued
 * before them can't undo (with a follow-up request), and load-more state that
 * a superseding reload always clears.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { deferred } from '../../pages/chats/__tests__/fixtures'
import { FOLLOW_UP_MS, usePagedList } from '../usePagedList'

type Row = { id: number }
const rows = (...ids: number[]): Row[] => ids.map((id) => ({ id }))
const ids = (r: { current: { items: Row[] } }) => r.current.items.map((x) => x.id)

afterEach(() => { vi.useRealTimers() })

function setup(fetchPage: (limit: number, offset: number, signal: AbortSignal) => Promise<Row[]>, key = 'a', pageSize = 2) {
  return renderHook(({ k }) => usePagedList<Row>(k, fetchPage, { pageSize }), { initialProps: { k: key } })
}

describe('usePagedList', () => {
  it('drops a slow first page of the previous key', async () => {
    const slowA = deferred<Row[]>()
    const fetchPage = vi.fn((_l: number, _o: number) => Promise.resolve(rows(7)))
    fetchPage.mockImplementationOnce(() => slowA.promise)
    const { result, rerender } = setup(fetchPage)
    rerender({ k: 'b' })
    await waitFor(() => expect(ids(result)).toEqual([7]))
    await act(async () => { slowA.resolve(rows(1, 2)) })
    expect(ids(result)).toEqual([7])
  })

  it('a reload issued before a local patch cannot undo it; a follow-up reload runs', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const reload = deferred<Row[]>()
    const fetchPage = vi.fn((_l: number, _o: number) => Promise.resolve(rows(1, 2)))
    const { result } = setup(fetchPage)
    await waitFor(() => expect(ids(result)).toEqual([1, 2]))

    fetchPage.mockImplementationOnce(() => reload.promise)
    act(() => { void result.current.reload() })
    // Row 1 is archived in the reading pane while the reload is in flight
    act(() => result.current.setItems((prev) => prev.filter((r) => r.id !== 1)))
    expect(ids(result)).toEqual([2])
    await act(async () => { reload.resolve(rows(1, 2)) }) // read before the archive
    expect(ids(result)).toEqual([2])

    fetchPage.mockImplementationOnce(() => Promise.resolve(rows(2, 3)))
    await act(async () => { vi.advanceTimersByTime(FOLLOW_UP_MS + 10) })
    await waitFor(() => expect(ids(result)).toEqual([2, 3]))
    expect(fetchPage).toHaveBeenCalledTimes(3)
  })

  it('a patch without anything in flight issues no request', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const fetchPage = vi.fn((_l: number, _o: number) => Promise.resolve(rows(1, 2)))
    const { result } = setup(fetchPage)
    await waitFor(() => expect(ids(result)).toEqual([1, 2]))
    act(() => result.current.setItems((prev) => prev.slice(1)))
    await act(async () => { vi.advanceTimersByTime(FOLLOW_UP_MS + 10) })
    expect(fetchPage).toHaveBeenCalledTimes(1)
    expect(ids(result)).toEqual([2])
  })

  it('a load-more superseded by a patch is redone from the rows held', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const more = deferred<Row[]>()
    const fetchPage = vi.fn((_l: number, _o: number) => Promise.resolve(rows(1, 2)))
    const { result } = setup(fetchPage)
    await waitFor(() => expect(result.current.hasMore).toBe(true))
    fetchPage.mockImplementationOnce(() => more.promise)
    act(() => { void result.current.loadMore() })
    act(() => result.current.setItems((prev) => prev.filter((r) => r.id !== 1)))
    await act(async () => { more.resolve(rows(1, 3)) }) // offset 2 page read before the removal
    expect(ids(result)).toEqual([2])
    expect(result.current.loadingMore).toBe(false)
    fetchPage.mockImplementationOnce(() => Promise.resolve(rows(3)))
    await act(async () => { vi.advanceTimersByTime(FOLLOW_UP_MS + 10) })
    await waitFor(() => expect(ids(result)).toEqual([2, 3]))
    expect(fetchPage).toHaveBeenLastCalledWith(2, 1, expect.any(AbortSignal))
  })

  it('a reload that fails while a load-more was in flight leaves load-more usable', async () => {
    const more = deferred<Row[]>()
    const fetchPage = vi.fn((_l: number, _o: number) => Promise.resolve(rows(1, 2)))
    const { result } = setup(fetchPage)
    await waitFor(() => expect(result.current.hasMore).toBe(true))
    fetchPage.mockImplementationOnce(() => more.promise)
    act(() => { void result.current.loadMore() })
    expect(result.current.loadingMore).toBe(true)
    fetchPage.mockImplementationOnce(() => Promise.reject(new Error('boom')))
    await act(async () => { await result.current.reload() })
    await act(async () => { more.resolve(rows(3, 4)) })
    expect(result.current.loadingMore).toBe(false)
    expect(result.current.error).toBeInstanceOf(Error)
    fetchPage.mockImplementationOnce(() => Promise.resolve(rows(3)))
    await act(async () => { await result.current.loadMore() })
    expect(ids(result)).toEqual([1, 2, 3])
  })

  it('reload fetches as many rows as are loaded', async () => {
    const all = rows(1, 2, 3)
    const fetchPage = vi.fn((limit: number, offset: number) => Promise.resolve(all.slice(offset, offset + limit)))
    const { result } = setup(fetchPage)
    await waitFor(() => expect(ids(result)).toEqual([1, 2]))
    await act(async () => { await result.current.loadMore() })
    expect(ids(result)).toEqual([1, 2, 3])
    await act(async () => { await result.current.reload() })
    expect(fetchPage).toHaveBeenLastCalledWith(4, 0, expect.any(AbortSignal))
  })
})
