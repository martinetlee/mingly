import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import type { CalendarEvent } from '../../api/calendar'

const accountsState = { accounts: [] as unknown[], channels: [], loaded: true, error: null, refresh: async () => {} }
vi.mock('../../hooks/useAccounts', () => ({ useAccounts: () => accountsState }))
vi.mock('../../hooks/useWebSocket', () => ({ useWsEvent: () => {}, useWebSocket: () => {} }))
vi.mock('../../api/calendar', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/calendar')>()),
  getCalendarEvents: vi.fn(),
  getCalendarAccounts: vi.fn(),
  getGoogleCalendars: vi.fn(),
  setSyncedCalendars: vi.fn(),
  syncCalendars: vi.fn(),
}))

import { getCalendarAccounts, getCalendarEvents, getGoogleCalendars, localDateKey, setSyncedCalendars } from '../../api/calendar'
import Calendar from '../Calendar'
import { addDays, eventDayKeys, groupByDay, startOfLocalDay, startOfWeek, viewRange } from '../../features/calendar/agenda'

const gmail = { account_key: 'gmail:3', account_id: 3, channel: 'gmail', display_name: 'Gmail', label: 'me@example.com', capabilities: [], configured: true, status: { state: 'ready', error: null } }

const today = startOfLocalDay(new Date())
const at = (day: Date, h: number, m = 0) => new Date(day.getFullYear(), day.getMonth(), day.getDate(), h, m).toISOString()

const me = { identity_id: 1, person_id: 1, name: 'Me', email: 'me@example.com', response_status: 'accepted', is_organizer: true, is_self: true }
const ann = { identity_id: 2, person_id: 7, name: 'Ann Lee', email: 'ann@example.com', response_status: 'accepted', is_organizer: false, is_self: false }
const bob = { identity_id: 3, person_id: 8, name: 'Bob', email: 'bob@example.com', response_status: 'declined', is_organizer: false, is_self: false }

function event(id: number, over: Partial<CalendarEvent> = {}): CalendarEvent {
  return {
    id, account_id: 3, calendar_id: 'primary', title: `Event ${id}`, description: null, location: null,
    start_at: at(today, 10), end_at: at(today, 11), all_day: false, status: 'confirmed',
    html_link: `https://calendar.google.com/calendar/event?eid=${id}`, organizer: me, attendees: [me], ...over,
  }
}

const standup = event(1, { title: 'Standup with Ann', location: 'Room 4', attendees: [me, ann, bob], start_at: at(today, 9, 30), end_at: at(today, 10) })
const offsite = event(2, { title: 'Offsite', all_day: true, start_at: localDateKey(addDays(today, 1)), end_at: localDateKey(addDays(today, 3)) })

function Where() {
  const loc = useLocation()
  return <div data-testid="where">{loc.pathname + loc.search}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/calendar" element={<><Calendar /><Where /></>} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  accountsState.accounts = [gmail]
  vi.mocked(getCalendarEvents).mockResolvedValue({ events: [offsite, standup] })
  vi.mocked(getCalendarAccounts).mockResolvedValue([
    { account_id: 3, label: 'me@example.com', calendar_ids: ['primary'], syncing: true, last_sync_at: null, error: null },
  ])
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('Calendar space', () => {
  it('shows the connect notice without a Gmail account (like Mail)', () => {
    accountsState.accounts = []
    renderAt('/calendar')
    expect(screen.getByTestId('no-accounts').textContent).toContain('No calendar connected')
    expect(screen.getByText('Open Settings → Connections').getAttribute('href')).toBe('/settings?tab=connections')
    expect(getCalendarEvents).not.toHaveBeenCalled()
  })

  it('agenda: events by day with time, title, location, people links and the Google Calendar link', async () => {
    renderAt('/calendar')
    const row = (await screen.findByText('Standup with Ann')).closest('[data-testid="calendar-event"]') as HTMLElement
    const [from, to] = vi.mocked(getCalendarEvents).mock.calls[0]
    expect(from).toEqual(today)
    expect(to).toEqual(addDays(today, 14))

    const days = screen.getAllByTestId('calendar-day')
    expect(days.map((d) => d.querySelector('h3')?.textContent)).toEqual(['Today', 'Tomorrow', expect.any(String)])
    expect(within(days[0]).getByText('Standup with Ann')).toBeTruthy()
    // A two-day all-day event shows on both of its dates (not shifted by time zone)
    expect(within(days[1]).getByText('Offsite')).toBeTruthy()
    expect(within(days[2]).getByText('Offsite')).toBeTruthy()
    expect(within(days[1]).getByText('All day')).toBeTruthy()

    expect(row.textContent).toContain('Room 4')
    const people = within(row).getAllByTestId('event-attendee')
    expect(people.map((p) => p.textContent)).toEqual(['You', 'Ann Lee', 'Bob'])
    expect(people[1].getAttribute('href')).toBe('/people/7')
    expect(people[2].className).toContain('line-through') // declined
    const google = within(row).getByTitle('Open in Google Calendar')
    expect(google.getAttribute('href')).toBe('https://calendar.google.com/calendar/event?eid=1')
    expect(google.getAttribute('target')).toBe('_blank')
    // Alone on your own event: no attendee chips
    expect(within(days[1]).queryAllByTestId('event-attendee')).toHaveLength(0)
  })

  it('week view lists every day from Monday; prev / next / today move the range', async () => {
    renderAt('/calendar')
    await screen.findByText('Standup with Ann')
    fireEvent.click(screen.getByRole('button', { name: 'Week' }))
    await waitFor(() => expect(screen.getAllByTestId('calendar-day')).toHaveLength(7))
    const monday = startOfWeek(today)
    expect(vi.mocked(getCalendarEvents).mock.lastCall?.[0]).toEqual(monday)
    expect(vi.mocked(getCalendarEvents).mock.lastCall?.[1]).toEqual(addDays(monday, 7))

    fireEvent.click(screen.getByLabelText('Next'))
    expect(screen.getByTestId('where').textContent).toBe(`/calendar?date=${localDateKey(addDays(today, 7))}`)
    await waitFor(() => expect(vi.mocked(getCalendarEvents).mock.lastCall?.[0]).toEqual(addDays(monday, 7)))
    fireEvent.click(screen.getByText('Today'))
    expect(screen.getByTestId('where').textContent).toBe('/calendar')
  })

  it('keeps the previous range on screen while the next one loads', async () => {
    renderAt('/calendar')
    await screen.findByText('Standup with Ann')
    let resolve!: (v: { events: CalendarEvent[] }) => void
    vi.mocked(getCalendarEvents).mockImplementationOnce(() => new Promise((r) => { resolve = r }))
    fireEvent.click(screen.getByLabelText('Next'))
    await waitFor(() => expect(getCalendarEvents).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('Loading…')).toBeNull()
    expect(screen.getByText('Standup with Ann')).toBeTruthy()
    expect(screen.getByTestId('calendar-stale')).toBeTruthy()
    resolve({ events: [] })
    await waitFor(() => expect(screen.queryByTestId('calendar-stale')).toBeNull())
    expect(screen.queryByText('Standup with Ann')).toBeNull()
  })

  it('t / ← / → / w / a move the range and switch the view', async () => {
    renderAt('/calendar')
    await screen.findByText('Standup with Ann')
    const key = (k: string) => fireEvent.keyDown(window, { key: k })
    key('ArrowRight')
    expect(screen.getByTestId('where').textContent).toBe(`/calendar?date=${localDateKey(addDays(today, 14))}`)
    key('ArrowLeft')
    expect(screen.getByTestId('where').textContent).toBe(`/calendar?date=${localDateKey(today)}`)
    key('ArrowRight')
    key('t')
    expect(screen.getByTestId('where').textContent).toBe('/calendar')
    key('w')
    await waitFor(() => expect(screen.getAllByTestId('calendar-day')).toHaveLength(7))
    key('a')
    expect(screen.getByRole('button', { name: 'Agenda' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('a failed load shows the error with Retry', async () => {
    vi.mocked(getCalendarEvents).mockRejectedValueOnce(new Error('offline'))
    renderAt('/calendar')
    expect((await screen.findByTestId('load-error')).textContent).toContain('Couldn’t load events: offline')
    fireEvent.click(screen.getByText('Retry'))
    await screen.findByText('Standup with Ann')
  })

  it('?date anchors the view and &event highlights that event', async () => {
    const day = addDays(today, 30)
    vi.mocked(getCalendarEvents).mockResolvedValue({ events: [event(9, { title: 'Linked', start_at: at(day, 15), end_at: at(day, 16) })] })
    renderAt(`/calendar?date=${localDateKey(day)}&event=9`)
    const row = (await screen.findByText('Linked')).closest('[data-testid="calendar-event"]') as HTMLElement
    expect(vi.mocked(getCalendarEvents).mock.calls[0][0]).toEqual(day)
    expect(row.className).toContain('border-indigo-500/60')
  })

  it('shows a sync error (e.g. the Calendar API not enabled) and the empty state', async () => {
    vi.mocked(getCalendarEvents).mockResolvedValue({ events: [] })
    vi.mocked(getCalendarAccounts).mockResolvedValue([
      { account_id: 3, label: 'me@example.com', calendar_ids: ['primary'], syncing: true, last_sync_at: null, error: 'Google Calendar API 403: not enabled' },
    ])
    renderAt('/calendar')
    expect((await screen.findByTestId('calendar-error')).textContent).toContain('Google Calendar API 403')
    expect(await screen.findByTestId('calendar-empty')).toBeTruthy()
  })

  it('chooses the synced calendars (the primary one as "primary")', async () => {
    vi.mocked(getGoogleCalendars).mockResolvedValue([
      { id: 'me@example.com', summary: 'me@example.com', primary: true, selected: true, color: '#9fe1e7' },
      { id: 'team@group.calendar.google.com', summary: 'Team', primary: false, selected: false, color: null },
    ])
    vi.mocked(setSyncedCalendars).mockResolvedValue({ calendar_ids: ['primary', 'team@group.calendar.google.com'] })
    renderAt('/calendar')
    fireEvent.click(screen.getByText('Calendars'))
    fireEvent.click(await screen.findByLabelText('Team'))
    await waitFor(() => expect(setSyncedCalendars).toHaveBeenCalledWith(3, ['primary', 'team@group.calendar.google.com']))
  })
})

describe('calendar day math', () => {
  it('a timed event ending at midnight stays on its day; one crossing midnight spans two', () => {
    const { from, to } = viewRange('agenda', today)
    const tomorrow = addDays(today, 1)
    expect(eventDayKeys(event(1, { start_at: at(today, 23), end_at: tomorrow.toISOString() }), from, to)).toEqual([localDateKey(today)])
    expect(eventDayKeys(event(1, { start_at: at(today, 23), end_at: at(tomorrow, 1) }), from, to))
      .toEqual([localDateKey(today), localDateKey(tomorrow)])
  })

  it('groups all-day events first, then by start; clips to the range', () => {
    const { from, to } = viewRange('agenda', today)
    const early = event(3, { start_at: at(today, 8), end_at: at(today, 9) })
    const allDay = event(4, { all_day: true, start_at: localDateKey(addDays(today, -2)), end_at: localDateKey(addDays(today, 1)) })
    const [first] = groupByDay([standup, early, allDay], from, to, false)
    expect(first.events.map((e) => e.id)).toEqual([4, 3, 1])
    expect(groupByDay([allDay], from, to, false)).toHaveLength(1) // only today is in range
  })
})
