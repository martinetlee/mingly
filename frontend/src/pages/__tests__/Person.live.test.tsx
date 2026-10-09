/**
 * The Person page follows changes made elsewhere: person_changed re-reads the
 * person, a person merged into someone else redirects there (404 with
 * merged_into), and merges / detaches reload the timeline, notes and meetings.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, act, waitFor, fireEvent, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import type { MergeSuggestion } from '../../api/peopleLinks'
import { makePerson } from '../chats/__tests__/fixtures'
import { WebSocketClient } from '../../lib/ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { ConfirmProvider } from '../../components/ui'
import { ApiError } from '../../lib/http'
import { resetToasts } from '../../lib/toast'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getTelegramProfile: vi.fn(),
  getPerson: vi.fn(),
  getPositions: vi.fn(async () => []),
  getPersonNotes: vi.fn(async () => []),
  getOrganizations: vi.fn(async () => []),
}))
vi.mock('../../api/peopleLinks', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/peopleLinks')>()),
  getPersonMergeSuggestions: vi.fn(),
  getPersonMerges: vi.fn(async () => []),
  mergePerson: vi.fn(),
}))
vi.mock('../../api/timeline', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/timeline')>()),
  getPersonTimeline: vi.fn(async () => ({ items: [], has_more: false })),
}))
vi.mock('../../api/calendar', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/calendar')>()),
  getUpcomingMeetings: vi.fn(async () => ({ events: [] })),
}))

import { getPerson, getPersonNotes, getTelegramProfile } from '../../api/client'
import { getPersonMergeSuggestions, mergePerson } from '../../api/peopleLinks'
import { getPersonTimeline } from '../../api/timeline'
import { getUpcomingMeetings } from '../../api/calendar'
import Person, { PERSON_REFRESH_MS } from '../Person'

const alice = () => makePerson(42, 'Alice')
const signalIdentity = {
  id: 2001, person_id: 42, channel: 'signal' as const, external_id: 'aci', display_name: 'Alice S',
  username: null, phone: null, avatar_url: null, is_contact: false,
}
const merged = () => ({ ...alice(), identities: [...alice().identities, signalIdentity] })
const gone = (mergedInto?: number) => new ApiError('Person not found', {
  status: 404, code: 'http', path: '/api/people/42',
  body: { detail: 'Person not found', ...(mergedInto != null ? { merged_into: mergedInto } : {}) },
})

function Where() {
  const loc = useLocation()
  return <div data-testid="where">{loc.pathname}</div>
}

function setup(path = '/people/42') {
  const client = new WebSocketClient({ url: () => null })
  render(
    <WebSocketProvider client={client}>
      <ConfirmProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes><Route path="/people/:personId" element={<><Person /><Where /></>} /></Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </WebSocketProvider>,
  )
  return client
}

beforeEach(() => {
  vi.clearAllMocks()
  resetToasts()
  vi.mocked(getTelegramProfile).mockRejectedValue(new Error('offline'))
  vi.mocked(getPersonMergeSuggestions).mockResolvedValue([])
})
afterEach(() => { cleanup(); vi.useRealTimers() })

describe('Person page — live', () => {
  it('person_changed re-reads the person (accounts linked elsewhere appear)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.mocked(getPerson).mockResolvedValue(alice())
    const client = setup()
    await waitFor(() => expect(screen.getAllByTestId('identity')).toHaveLength(1))
    vi.mocked(getPerson).mockResolvedValue(merged())
    act(() => client.dispatch({ type: 'person_changed', person_id: 42 }))
    await act(async () => { vi.advanceTimersByTime(PERSON_REFRESH_MS + 10) })
    await waitFor(() => expect(screen.getAllByTestId('identity')).toHaveLength(2))
    // The identity set changed: the panels reloaded
    await waitFor(() => expect(getPersonTimeline).toHaveBeenCalledTimes(2))
    expect(getPersonNotes).toHaveBeenCalledTimes(2)
    expect(getUpcomingMeetings).toHaveBeenCalledTimes(2)
  })

  it('follows the person it was merged into', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.mocked(getPerson).mockImplementation(async (id: number) => (id === 42 ? alice() : makePerson(7, 'Alice Work')))
    const client = setup()
    await screen.findByTestId('linked-accounts')
    vi.mocked(getPerson).mockImplementation(async (id: number) => {
      if (id === 42) throw gone(7)
      return makePerson(7, 'Alice Work')
    })
    act(() => client.dispatch({ type: 'person_changed', person_id: 42 }))
    await act(async () => { vi.advanceTimersByTime(PERSON_REFRESH_MS + 10) })
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/people/7'))
    expect((await screen.findAllByText('Alice Work')).length).toBeGreaterThan(0)
  })

  it('a link to a merged-away person opens the surviving one', async () => {
    vi.mocked(getPerson).mockImplementation(async (id: number) => {
      if (id === 42) throw gone(7)
      return makePerson(7, 'Alice Work')
    })
    setup()
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/people/7'))
  })

  it('a person removed without a merge target keeps the last copy with a notice', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.mocked(getPerson).mockResolvedValue(alice())
    const client = setup()
    await screen.findByTestId('linked-accounts')
    vi.mocked(getPerson).mockRejectedValue(gone())
    act(() => client.dispatch({ type: 'person_changed', person_id: 42 }))
    await act(async () => { vi.advanceTimersByTime(PERSON_REFRESH_MS + 10) })
    expect(await screen.findByTestId('person-gone')).toBeTruthy()
    expect(screen.getByTestId('where').textContent).toBe('/people/42')
  })

  it('merging here reloads timeline, notes and upcoming meetings', async () => {
    const other = makePerson(7, 'Alice Work')
    const suggestion: MergeSuggestion = { id: 5, reason: 'same_name_shared_group', score: 0.6, created_at: null, person_a: other, person_b: alice() }
    vi.mocked(getPerson).mockResolvedValue(alice())
    vi.mocked(getPersonMergeSuggestions).mockResolvedValue([suggestion])
    vi.mocked(mergePerson).mockResolvedValue({ ...merged(), merge_id: 11 })
    setup()
    const list = await screen.findByTestId('merge-suggestions')
    await waitFor(() => expect(getPersonTimeline).toHaveBeenCalledTimes(1))
    fireEvent.click(within(list).getByRole('button', { name: 'Merge' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Merge' }))
    await waitFor(() => expect(getPersonTimeline).toHaveBeenCalledTimes(2))
    expect(getPersonNotes).toHaveBeenCalledTimes(2)
    expect(getUpcomingMeetings).toHaveBeenCalledTimes(2)
  })
})
