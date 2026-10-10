import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import type { PeoplePage, Person } from '../../api/types'
import { makePerson } from '../../test/fixtures'

/** A promise you resolve/reject by hand. */
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const api = vi.hoisted(() => ({
  getPerson: vi.fn(),
  getPeoplePage: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  getPerson: (...a: unknown[]) => api.getPerson(...a),
  getPeoplePage: (...a: unknown[]) => api.getPeoplePage(...a),
  isAbortError: (e: unknown) => e instanceof Error && e.name === 'AbortError',
}))

import { usePersonSummary, invalidatePerson, resetPersonStore } from '../usePersonSummary'
import { usePagedContacts } from '../usePagedContacts'

const person = (id: number, name: string, status: Person['relationship_status'] = null): Person =>
  makePerson(id, name, { relationship_status: status })

const contact = (id: number, name: string): Person => makePerson(id, name)
const page = (items: Person[], total = items.length): PeoplePage => ({ items, total, limit: 2, offset: 0, status_counts: {} })

beforeEach(() => {
  vi.clearAllMocks()
  resetPersonStore()
})

describe('usePersonSummary (PersonCard store)', () => {
  it('shares one request per person between every card showing them', async () => {
    api.getPerson.mockImplementation(async (id: number) => person(id, id === 1 ? 'Ann' : 'Bob'))
    const a = renderHook(() => usePersonSummary(1))
    const a2 = renderHook(() => usePersonSummary(1))
    const b = renderHook(() => usePersonSummary(2))
    await waitFor(() => expect(a.result.current?.name).toBe('Ann'))
    await waitFor(() => expect(b.result.current?.name).toBe('Bob'))
    expect(a2.result.current?.name).toBe('Ann')
    expect(api.getPerson).toHaveBeenCalledTimes(2)
    expect(api.getPerson.mock.calls.map((c) => c[0]).sort()).toEqual([1, 2])
  })

  it('derives username and the active position from the person', async () => {
    api.getPerson.mockResolvedValue(makePerson(3, 'Cy', {
      identities: [{ id: 30, person_id: 3, channel: 'telegram', external_id: '9', display_name: 'Cy', username: 'cy', phone: '+1', avatar_url: null, is_contact: true }],
      positions: [
        { id: 1, organization: 'Old', organization_id: 1, title: null, is_active: false, ended_at: '2020-01-01' },
        { id: 2, organization: 'Acme', organization_id: 2, title: 'CTO', is_active: true, ended_at: null },
      ],
    }))
    const { result } = renderHook(() => usePersonSummary(3))
    await waitFor(() => expect(result.current?.username).toBe('cy'))
    expect(result.current?.primary_position).toMatchObject({ organization: 'Acme', title: 'CTO' })
    expect(result.current?.phone).toBe('+1')
  })

  it('a response requested before an invalidation never overwrites the fresh one', async () => {
    const first = deferred<Person>()
    const second = deferred<Person>()
    api.getPerson.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { result } = renderHook(() => usePersonSummary(7))
    await waitFor(() => expect(api.getPerson).toHaveBeenCalledTimes(1))

    // Status changed on the server → mutation invalidates → refetch
    act(() => invalidatePerson(7))
    await waitFor(() => expect(api.getPerson).toHaveBeenCalledTimes(2))
    await act(async () => { second.resolve(person(7, 'Eve', 'active')) })
    expect(result.current?.relationship_status).toBe('active')
    // The pre-mutation read lands late: ignored
    await act(async () => { first.resolve(person(7, 'Eve', 'cold')) })
    expect(result.current?.relationship_status).toBe('active')
  })

  it('skips the request when the caller supplied every field', async () => {
    renderHook(() => usePersonSummary(9, false))
    await new Promise((r) => setTimeout(r, 5))
    expect(api.getPerson).not.toHaveBeenCalled()
  })
})

describe('usePagedContacts (server-side filter + pagination)', () => {
  it('drops a page for the previous status filter', async () => {
    const all = deferred<PeoplePage>()
    const stalled = deferred<PeoplePage>()
    api.getPeoplePage.mockImplementation((p: { status: string }) => (p.status === 'all' ? all.promise : stalled.promise))
    const { result, rerender } = renderHook(({ s }) => usePagedContacts('', s), { initialProps: { s: 'all' } })
    rerender({ s: 'stalled' })
    expect(api.getPeoplePage.mock.calls[1][0]).toMatchObject({ status: 'stalled', offset: 0, contactsOnly: true })
    await act(async () => { stalled.resolve(page([contact(2, 'Stalled Sam')])) })
    await act(async () => { all.resolve(page([contact(1, 'Anyone')])) })
    expect(result.current.items.map((c) => c.id)).toEqual([2])
  })

  it('loadMore advances the offset until items reach total, appending only for the current filter', async () => {
    api.getPeoplePage.mockResolvedValueOnce(page([contact(1, 'A'), contact(2, 'B')], 3))
    const { result, rerender } = renderHook(({ q }) => usePagedContacts(q, 'all', 2), { initialProps: { q: '' } })
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.hasMore).toBe(true)

    const more = deferred<PeoplePage>()
    api.getPeoplePage.mockReturnValueOnce(more.promise)
    act(() => { void result.current.loadMore() })
    expect(api.getPeoplePage.mock.calls[1][0]).toMatchObject({ offset: 2, limit: 2 })
    await act(async () => { more.resolve({ ...page([contact(3, 'C')], 3), offset: 2 }) })
    expect(result.current.items.map((c) => c.id)).toEqual([1, 2, 3])
    expect(result.current.hasMore).toBe(false)

    // A load-more for an old search must not append into a new search
    api.getPeoplePage.mockResolvedValueOnce(page([contact(4, 'D'), contact(5, 'E')], 4))
    rerender({ q: 'x' })
    await waitFor(() => expect(result.current.items.map((c) => c.id)).toEqual([4, 5]))
    const stale = deferred<PeoplePage>()
    api.getPeoplePage.mockReturnValueOnce(stale.promise)
    act(() => { void result.current.loadMore() })
    api.getPeoplePage.mockResolvedValueOnce(page([contact(9, 'Z')], 1))
    rerender({ q: 'xy' })
    await waitFor(() => expect(result.current.items.map((c) => c.id)).toEqual([9]))
    await act(async () => { stale.resolve(page([contact(6, 'F')], 4)) })
    expect(result.current.items.map((c) => c.id)).toEqual([9])
  })
})
