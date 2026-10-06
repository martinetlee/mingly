import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import type { Contact, ContactsPage, PersonSummary } from '../../api/types'

/** A promise you resolve/reject by hand. */
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const api = vi.hoisted(() => ({
  resolvePeople: vi.fn(),
  getContactsPage: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  resolvePeople: (...a: unknown[]) => api.resolvePeople(...a),
  getContactsPage: (...a: unknown[]) => api.getContactsPage(...a),
  isAbortError: (e: unknown) => e instanceof Error && e.name === 'AbortError',
}))

import { usePersonSummary, invalidatePerson, resetPersonStore } from '../usePersonSummary'
import { usePagedContacts } from '../usePagedContacts'

const person = (id: number, name: string, status: string | null = null): PersonSummary => ({
  telegram_id: id, name, username: null, is_important: false, contact_id: 1, relationship_status: status,
  last_interaction_at: null, primary_position: null, phone: null, twitter_handle: null, linkedin_url: null, has_notes: false,
})

const contact = (id: number, first: string): Contact => ({ id, telegram_id: id * 10, name: first, first_name: first, positions: [] } as unknown as Contact)
const page = (items: Contact[], total = items.length): ContactsPage => ({ items, total, limit: 2, offset: 0, status_counts: {} })

beforeEach(() => {
  vi.clearAllMocks()
  resetPersonStore()
})

describe('usePersonSummary (PersonCard store)', () => {
  it('batches lookups from the same tick into one request', async () => {
    api.resolvePeople.mockResolvedValue({ '1': person(1, 'Ann'), '2': person(2, 'Bob') })
    const a = renderHook(() => usePersonSummary(1))
    const b = renderHook(() => usePersonSummary(2))
    await waitFor(() => expect(a.result.current?.name).toBe('Ann'))
    expect(b.result.current?.name).toBe('Bob')
    expect(api.resolvePeople).toHaveBeenCalledTimes(1)
    expect(api.resolvePeople.mock.calls[0][0].sort()).toEqual([1, 2])
  })

  it('a response requested before an invalidation never overwrites the fresh one', async () => {
    const first = deferred<Record<string, PersonSummary>>()
    const second = deferred<Record<string, PersonSummary>>()
    api.resolvePeople.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { result } = renderHook(() => usePersonSummary(7))
    await waitFor(() => expect(api.resolvePeople).toHaveBeenCalledTimes(1))

    // Status changed on the server → mutation invalidates → refetch
    act(() => invalidatePerson(7))
    await waitFor(() => expect(api.resolvePeople).toHaveBeenCalledTimes(2))
    await act(async () => { second.resolve({ '7': person(7, 'Eve', 'active') }) })
    expect(result.current?.relationship_status).toBe('active')
    // The pre-mutation read lands late: ignored
    await act(async () => { first.resolve({ '7': person(7, 'Eve', 'cold') }) })
    expect(result.current?.relationship_status).toBe('active')
  })

  it('skips the request when the caller supplied every field', async () => {
    renderHook(() => usePersonSummary(9, false))
    await new Promise((r) => setTimeout(r, 5))
    expect(api.resolvePeople).not.toHaveBeenCalled()
  })
})

describe('usePagedContacts (server-side filter + pagination)', () => {
  it('drops a page for the previous status filter', async () => {
    const all = deferred<ContactsPage>()
    const stalled = deferred<ContactsPage>()
    api.getContactsPage.mockImplementation((p: { status: string }) => (p.status === 'all' ? all.promise : stalled.promise))
    const { result, rerender } = renderHook(({ s }) => usePagedContacts('', s), { initialProps: { s: 'all' } })
    rerender({ s: 'stalled' })
    expect(api.getContactsPage.mock.calls[1][0]).toMatchObject({ status: 'stalled', offset: 0 })
    await act(async () => { stalled.resolve(page([contact(2, 'Stalled Sam')])) })
    await act(async () => { all.resolve(page([contact(1, 'Anyone')])) })
    expect(result.current.items.map((c) => c.id)).toEqual([2])
  })

  it('loadMore advances the offset until items reach total, appending only for the current filter', async () => {
    api.getContactsPage.mockResolvedValueOnce(page([contact(1, 'A'), contact(2, 'B')], 3))
    const { result, rerender } = renderHook(({ q }) => usePagedContacts(q, 'all', 2), { initialProps: { q: '' } })
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.hasMore).toBe(true)

    const more = deferred<ContactsPage>()
    api.getContactsPage.mockReturnValueOnce(more.promise)
    act(() => { void result.current.loadMore() })
    expect(api.getContactsPage.mock.calls[1][0]).toMatchObject({ offset: 2, limit: 2 })
    await act(async () => { more.resolve({ ...page([contact(3, 'C')], 3), offset: 2 }) })
    expect(result.current.items.map((c) => c.id)).toEqual([1, 2, 3])
    expect(result.current.hasMore).toBe(false)

    // A load-more for an old search must not append into a new search
    api.getContactsPage.mockResolvedValueOnce(page([contact(4, 'D'), contact(5, 'E')], 4))
    rerender({ q: 'x' })
    await waitFor(() => expect(result.current.items.map((c) => c.id)).toEqual([4, 5]))
    const stale = deferred<ContactsPage>()
    api.getContactsPage.mockReturnValueOnce(stale.promise)
    act(() => { void result.current.loadMore() })
    api.getContactsPage.mockResolvedValueOnce(page([contact(9, 'Z')], 1))
    rerender({ q: 'xy' })
    await waitFor(() => expect(result.current.items.map((c) => c.id)).toEqual([9]))
    await act(async () => { stale.resolve(page([contact(6, 'F')], 4)) })
    expect(result.current.items.map((c) => c.id)).toEqual([9])
  })
})
