import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { CalendarEvent } from '../../api/calendar'
import { makePerson } from '../chats/__tests__/fixtures'
import { WebSocketClient } from '../../lib/ws'
import { WebSocketProvider } from '../../context/WebSocketContext'
import { ConfirmProvider } from '../../components/ui'

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getTelegramProfile: vi.fn(),
  getPerson: vi.fn(),
  getPositions: vi.fn(async () => []),
  getPersonNotes: vi.fn(async () => []),
  getOrganizations: vi.fn(async () => []),
}))
vi.mock('../../api/timeline', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/timeline')>()),
  getPersonTimeline: vi.fn(async () => ({ items: [], has_more: false })),
}))
vi.mock('../../api/calendar', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/calendar')>()),
  getUpcomingMeetings: vi.fn(),
}))

import { getPerson, getTelegramProfile } from '../../api/client'
import { getUpcomingMeetings, localDateKey } from '../../api/calendar'
import Person from '../Person'

const me = { identity_id: 1, person_id: 1, name: 'Me', email: 'me@example.com', response_status: 'accepted', is_organizer: true, is_self: true }
const alice = { identity_id: 2, person_id: 42, name: 'Alice', email: 'alice@example.com', response_status: 'accepted', is_organizer: false, is_self: false }
const bob = { identity_id: 3, person_id: 8, name: 'Bob', email: 'bob@example.com', response_status: 'accepted', is_organizer: false, is_self: false }

const tomorrow = new Date()
tomorrow.setDate(tomorrow.getDate() + 1)
tomorrow.setHours(15, 0, 0, 0)

const meeting: CalendarEvent = {
  id: 11, account_id: 3, calendar_id: 'primary', title: 'Roadmap review', description: null, location: 'Room 4',
  start_at: tomorrow.toISOString(), end_at: new Date(tomorrow.getTime() + 3600_000).toISOString(), all_day: false,
  status: 'confirmed', html_link: 'https://calendar.google.com/x', organizer: me, attendees: [me, alice, bob],
}

beforeEach(() => {
  vi.mocked(getTelegramProfile).mockRejectedValue(new Error('offline'))
  vi.mocked(getPerson).mockResolvedValue(makePerson(42, 'Alice', { is_contact: false, positions: [] }))
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

function setup() {
  render(
    <WebSocketProvider client={new WebSocketClient({ url: () => null })}>
      <ConfirmProvider>
        <MemoryRouter initialEntries={['/people/42']}>
          <Routes><Route path="/people/:personId" element={<Person />} /></Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </WebSocketProvider>,
  )
}

describe('Person page: upcoming meetings', () => {
  it('lists the next meetings with the others involved, linking to their day in Calendar', async () => {
    vi.mocked(getUpcomingMeetings).mockResolvedValue({ events: [meeting] })
    setup()
    const block = await screen.findByTestId('upcoming-meetings')
    expect(getUpcomingMeetings).toHaveBeenCalledWith(42, expect.anything())
    expect(block.textContent).toContain('Upcoming meetings')
    const [row] = screen.getAllByTestId('upcoming-meeting')
    expect(row.textContent).toContain('Roadmap review')
    expect(row.textContent).toContain('Tomorrow')
    expect(row.textContent).toContain('with Bob') // not you, not Alice herself
    expect(row.textContent).toContain('Room 4')
    expect(row.getAttribute('href')).toBe(`/calendar?event=11&date=${localDateKey(tomorrow)}`)
    // The existing sections stay
    expect(screen.getByTestId('person-timeline')).toBeTruthy()
    expect(screen.getByTestId('person-notes')).toBeTruthy()
  })

  it('is hidden when there are no upcoming meetings', async () => {
    vi.mocked(getUpcomingMeetings).mockResolvedValue({ events: [] })
    setup()
    await screen.findByTestId('person-timeline')
    await waitFor(() => expect(getUpcomingMeetings).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.queryByTestId('upcoming-meetings')).toBeNull()
  })
})
