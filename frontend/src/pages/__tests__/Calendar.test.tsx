import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import type { CalendarAccount, CalendarEvent } from '../../api/calendar'
import type { Conversation } from '../../api/types'

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
vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getPersonConversations: vi.fn(),
}))

import { getCalendarAccounts, getCalendarEvents, getGoogleCalendars, localDateKey, setSyncedCalendars } from '../../api/calendar'
import { getPersonConversations } from '../../api/client'
import Calendar from '../Calendar'
import { addDays, eventDayKeys, groupByDay, startOfLocalDay, startOfWeek, viewRange } from '../../features/calendar/agenda'
import { HOUR_PX } from '../calendar/TimeGrid'

const gmail = { account_key: 'gmail:3', account_id: 3, channel: 'gmail', display_name: 'Gmail', label: 'me@example.com', capabilities: [], configured: true, status: { state: 'ready', error: null } }

const today = startOfLocalDay(new Date())
const monday = startOfWeek(today)
const at = (day: Date, h: number, m = 0) => new Date(day.getFullYear(), day.getMonth(), day.getDate(), h, m).toISOString()

const me = { identity_id: 1, person_id: 1, name: 'Me', email: 'me@example.com', response_status: 'accepted', is_organizer: true, is_self: true }
const ann = { identity_id: 2, person_id: 7, name: 'Ann Lee', email: 'ann@example.com', response_status: 'accepted', is_organizer: false, is_self: false }
const bob = { identity_id: 3, person_id: 8, name: 'Bob', email: 'bob@example.com', response_status: 'declined', is_organizer: false, is_self: false }

function event(id: number, over: Partial<CalendarEvent> = {}): CalendarEvent {
  return {
    id, account_id: 3, calendar_id: 'primary', title: `Event ${id}`, description: null, location: null,
    start_at: at(today, 10), end_at: at(today, 11), all_day: false, status: 'confirmed',
    html_link: `https://calendar.google.com/calendar/event?eid=${id}`, organizer: me, attendees: [me],
    conference_url: null, conference_name: null, my_response: null, recurrence: null, recurring: false,
    color: null, transparency: null, ...over,
  }
}

const tuesday = addDays(monday, 1)
const standup = event(1, {
  title: 'Standup with Ann', location: 'Room 4', attendees: [me, ann, bob], start_at: at(tuesday, 9, 30), end_at: at(tuesday, 10),
  conference_url: 'https://meet.google.com/abc-defg-hij', conference_name: 'Google Meet', my_response: 'tentative',
  recurring: true, recurrence: 'RRULE:FREQ=WEEKLY;BYDAY=TU',
  description: 'Agenda: <b>roadmap</b><script>alert(1)</script> <a href="javascript:alert(2)">bad</a> <a href="https://docs.example.com/x">doc</a>',
})
const offsite = event(2, { title: 'Offsite', all_day: true, start_at: localDateKey(addDays(monday, 2)), end_at: localDateKey(addDays(monday, 4)) })

const account = (over: Partial<CalendarAccount> = {}): CalendarAccount => ({
  account_id: 3, label: 'me@example.com', calendar_ids: ['primary'], syncing: true, last_sync_at: null, error: null, ...over,
})

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

const where = () => screen.getByTestId('where').textContent
const weekPath = `/calendar?view=week&date=${localDateKey(monday)}`
const eventEl = (id: number) => document.querySelector(`[data-testid="calendar-event"][data-event-id="${id}"]`) as HTMLElement | null

beforeEach(() => {
  localStorage.clear()
  accountsState.accounts = [gmail]
  vi.mocked(getCalendarEvents).mockResolvedValue({ events: [offsite, standup] })
  vi.mocked(getCalendarAccounts).mockResolvedValue([account()])
  vi.mocked(getPersonConversations).mockResolvedValue([])
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

  it('opens on the week time grid: blocks placed by time, all-day events as spanning bars', async () => {
    renderAt('/calendar')
    await waitFor(() => expect(eventEl(1)).not.toBeNull())
    expect(screen.getByTestId('time-grid')).toBeTruthy()
    expect(vi.mocked(getCalendarEvents).mock.calls[0][0]).toEqual(monday)
    expect(vi.mocked(getCalendarEvents).mock.calls[0][1]).toEqual(addDays(monday, 7))
    expect(screen.getAllByTestId('grid-day').map((d) => d.getAttribute('data-day'))[1]).toBe(localDateKey(tuesday))

    // 09:30 – 10:00 on Tuesday: top at 9.5 hours, the minimum block height
    const block = eventEl(1)!
    expect(block.closest('[data-testid="grid-day"]')?.getAttribute('data-day')).toBe(localDateKey(tuesday))
    expect(block.style.top).toBe(`${9.5 * HOUR_PX + 1}px`)
    expect(block.style.height).toBe(`${HOUR_PX / 2 - 2}px`)

    // The two-day all-day event is one bar over Wednesday and Thursday
    const bar = eventEl(2)!
    expect(bar.closest('[data-testid="all-day-row"]')).not.toBeNull()
    expect(bar.style.left).toMatch(/^calc\(28\.57\d*% \+ 2px\)$/) // from Wednesday
    expect(bar.style.width).toMatch(/^calc\(28\.57\d*% - 4px\)$/) // two days
  })

  it('places overlapping events side by side', async () => {
    vi.mocked(getCalendarEvents).mockResolvedValue({ events: [
      event(5, { start_at: at(tuesday, 10), end_at: at(tuesday, 11) }),
      event(6, { start_at: at(tuesday, 10, 30), end_at: at(tuesday, 11, 30) }),
      event(7, { start_at: at(tuesday, 13), end_at: at(tuesday, 14) }),
    ] })
    renderAt(weekPath)
    await waitFor(() => expect(eventEl(5)).not.toBeNull())
    expect(eventEl(5)!.style.width).toBe('calc(50% - 3px)')
    expect(eventEl(6)!.style.left).toBe('calc(50% + 1px)')
    expect(eventEl(7)!.style.width).toBe('calc(100% - 3px)')
  })

  it('agenda: rows with time, title, location, guests, Join and your reply', async () => {
    renderAt(`/calendar?view=agenda&date=${localDateKey(monday)}`)
    const row = (await screen.findByText('Standup with Ann')).closest('[data-testid="calendar-event"]') as HTMLElement
    const [from, to] = vi.mocked(getCalendarEvents).mock.calls[0]
    expect(from).toEqual(monday)
    expect(to).toEqual(addDays(monday, 14))

    const days = screen.getAllByTestId('calendar-day')
    expect(days).toHaveLength(3)
    // A two-day all-day event shows on both of its dates (not shifted by time zone), as day 1 and 2
    expect(within(days[1]).getByText('Offsite')).toBeTruthy()
    expect(within(days[2]).getByText('Offsite')).toBeTruthy()
    expect(days[2].textContent).toContain('Day 2 of 2')

    expect(row.textContent).toContain('Room 4')
    const people = within(row).getAllByTestId('event-attendee')
    expect(people.map((p) => p.textContent)).toEqual(['You', 'Ann Lee', 'Bob'])
    expect(people[1].getAttribute('href')).toBe('/people/7')
    expect(people[2].className).toContain('line-through') // declined
    expect(within(row).getByText('Join').closest('a')?.getAttribute('href')).toBe('https://meet.google.com/abc-defg-hij')
    expect(within(row).getByTestId('my-response').textContent).toBe('Maybe')
    // Alone on your own event: no guest chips
    expect(within(days[1]).queryAllByTestId('event-attendee')).toHaveLength(0)
  })

  it('clicking an event opens its details; Esc closes them', async () => {
    vi.mocked(getPersonConversations).mockImplementation(async (id: number) => (id === 7 ? [
      { id: 40, kind: 'direct', channel: 'signal', title: 'Ann Lee', last_activity_at: new Date().toISOString() } as unknown as Conversation,
    ] : []))
    renderAt(weekPath)
    await waitFor(() => expect(eventEl(1)).not.toBeNull())
    fireEvent.click(eventEl(1)!)
    expect(where()).toBe(`${weekPath}&event=1`)

    const panel = await screen.findByTestId('event-panel')
    expect(within(panel).getByRole('heading', { name: 'Standup with Ann' })).toBeTruthy()
    expect(within(panel).getByTestId('event-join').getAttribute('href')).toBe('https://meet.google.com/abc-defg-hij')
    expect(within(panel).getByTestId('event-join').textContent).toContain('Join Google Meet')
    expect(within(panel).getByTestId('event-my-response').textContent).toContain('You: Maybe')
    expect(within(panel).getByTestId('event-recurrence').textContent).toBe(' Weekly on Tue')
    expect(within(panel).getByTestId('guests-going').textContent).toContain('Ann Lee')
    expect(within(panel).getByTestId('guests-declined').textContent).toContain('Bob')
    expect(within(panel).getByText('Open in Maps').getAttribute('href')).toContain('query=Room%204')

    // The description: formatting and safe links kept; script and javascript: links dropped
    const notes = within(panel).getByTestId('event-description')
    expect(notes.querySelector('b')?.textContent).toBe('roadmap')
    expect(notes.querySelector('script')).toBeNull()
    expect(notes.textContent).not.toContain('alert(1)')
    expect(within(notes).getByText('bad').closest('a')).toBeNull()
    expect(within(notes).getByText('doc').closest('a')?.getAttribute('href')).toBe('https://docs.example.com/x')

    // Recent conversations with the guests (not you)
    const recent = await within(panel).findByTestId('event-conversations')
    expect(within(recent).getByText('Ann Lee').closest('a')?.getAttribute('href')).toBe('/chats?conversationId=40')
    expect(vi.mocked(getPersonConversations).mock.calls.map((c) => c[0]).sort()).toEqual([7, 8])

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(where()).toBe(weekPath)
    expect(screen.queryByTestId('event-panel')).toBeNull()
  })

  it('a deep link opens its week with the event selected', async () => {
    const day = addDays(today, 30)
    vi.mocked(getCalendarEvents).mockResolvedValue({ events: [event(9, { title: 'Linked', start_at: at(day, 15), end_at: at(day, 16) })] })
    renderAt(`/calendar?view=week&date=${localDateKey(day)}&event=9`)
    expect(await screen.findByTestId('event-panel')).toBeTruthy()
    expect(vi.mocked(getCalendarEvents).mock.calls[0][0]).toEqual(startOfWeek(day))
    expect(eventEl(9)!.className).toContain('ring-2')
  })

  it('month view: six weeks from Monday, "+N more" opens the day', async () => {
    const day = addDays(new Date(today.getFullYear(), today.getMonth(), 1), 9)
    vi.mocked(getCalendarEvents).mockResolvedValue({
      events: [1, 2, 3, 4, 5].map((i) => event(20 + i, { title: `Busy ${i}`, start_at: at(day, 8 + i), end_at: at(day, 9 + i) })),
    })
    renderAt('/calendar')
    await waitFor(() => expect(getCalendarEvents).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Month' }))
    expect(where()).toBe('/calendar?view=month')
    await screen.findByTestId('month-grid')
    const first = new Date(today.getFullYear(), today.getMonth(), 1)
    await waitFor(() => expect(vi.mocked(getCalendarEvents).mock.lastCall?.[0]).toEqual(startOfWeek(first)))
    expect(vi.mocked(getCalendarEvents).mock.lastCall?.[1]).toEqual(addDays(startOfWeek(first), 42))
    expect(screen.getAllByTestId('month-week')).toHaveLength(6)

    const cell = (await screen.findAllByTestId('month-day')).find((c) => c.getAttribute('data-day') === localDateKey(day))!
    await waitFor(() => expect(within(cell).getAllByTestId('calendar-event')).toHaveLength(3))
    fireEvent.click(within(cell).getByTestId('month-more'))
    expect(where()).toBe(`/calendar?view=day&date=${localDateKey(day)}`)
    await waitFor(() => expect(screen.getAllByTestId('grid-day')).toHaveLength(1))
  })

  it('remembers the last view', async () => {
    renderAt('/calendar')
    await waitFor(() => expect(getCalendarEvents).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Agenda' }))
    cleanup()
    renderAt('/calendar')
    expect(screen.getByRole('button', { name: 'Agenda' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('t / ← / → / d / w / m / a move the range and switch the view', async () => {
    renderAt('/calendar')
    await waitFor(() => expect(eventEl(1)).not.toBeNull())
    const key = (k: string) => fireEvent.keyDown(window, { key: k })
    key('ArrowRight')
    expect(where()).toBe(`/calendar?date=${localDateKey(addDays(today, 7))}`)
    key('ArrowLeft')
    expect(where()).toBe(`/calendar?date=${localDateKey(today)}`)
    key('ArrowRight')
    key('t')
    expect(where()).toBe('/calendar')
    key('d')
    expect(where()).toBe('/calendar?view=day')
    await waitFor(() => expect(screen.getAllByTestId('grid-day')).toHaveLength(1))
    key('ArrowRight')
    expect(where()).toBe(`/calendar?view=day&date=${localDateKey(addDays(today, 1))}`)
    key('m')
    await screen.findByTestId('month-grid')
    key('a')
    expect(screen.getByRole('button', { name: 'Agenda' }).getAttribute('aria-pressed')).toBe('true')
    key('w')
    expect(screen.getByRole('button', { name: 'Week' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('the small month jumps to a day', async () => {
    renderAt('/calendar')
    await waitFor(() => expect(getCalendarEvents).toHaveBeenCalled())
    const target = addDays(today, 3)
    const btn = screen.getAllByTestId('mini-day').find((b) => b.getAttribute('data-day') === localDateKey(target))!
    fireEvent.click(btn)
    expect(where()).toBe(`/calendar?date=${localDateKey(target)}`)
  })

  it('keeps the previous range on screen while the next one loads', async () => {
    renderAt(`/calendar?view=agenda&date=${localDateKey(monday)}`)
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

  it('a failed load shows the error with Retry', async () => {
    vi.mocked(getCalendarEvents).mockRejectedValueOnce(new Error('offline'))
    renderAt(`/calendar?view=agenda&date=${localDateKey(monday)}`)
    expect((await screen.findByTestId('load-error')).textContent).toContain('Couldn’t load events: offline')
    fireEvent.click(screen.getByText('Retry'))
    await screen.findByText('Standup with Ann')
  })

  it('a sync error wraps and its link is clickable; the agenda says when there are no events', async () => {
    const url = 'https://console.developers.google.com/apis/api/calendar-json.googleapis.com/overview?project=272352178118'
    vi.mocked(getCalendarEvents).mockResolvedValue({ events: [] })
    vi.mocked(getCalendarAccounts).mockResolvedValue([account({
      error: `Google Calendar API 403: Google Calendar API has not been used in project 272352178118 before or it is disabled. Enable it by visiting ${url} then retry.`,
    })])
    renderAt('/calendar?view=agenda')
    const banner = await screen.findByTestId('calendar-error')
    const link = within(banner).getByRole('link')
    expect(link.getAttribute('href')).toBe(url)
    expect(link.getAttribute('target')).toBe('_blank')
    expect(banner.textContent).toContain('then retry.')
    expect((await screen.findByTestId('calendar-empty')).textContent).toMatch(/^No events in /)
  })

  it('hides and shows calendars without touching what is synced', async () => {
    vi.mocked(getCalendarAccounts).mockResolvedValue([account({
      calendars: [
        { id: 'primary', name: 'me@example.com', color: '#9fe1e7', primary: true },
        { id: 'team@group.calendar.google.com', name: 'Team', color: '#f83a22', primary: false },
      ],
    })])
    vi.mocked(getCalendarEvents).mockResolvedValue({ events: [
      standup, event(30, { title: 'Team sync', calendar_id: 'team@group.calendar.google.com', start_at: at(tuesday, 14), end_at: at(tuesday, 15) }),
    ] })
    renderAt(weekPath)
    await waitFor(() => expect(eventEl(30)).not.toBeNull())
    fireEvent.click(within(screen.getByTestId('calendar-list')).getByLabelText('Team'))
    expect(eventEl(30)).toBeNull()
    expect(eventEl(1)).not.toBeNull()
    expect(JSON.parse(localStorage.getItem('mingly.calendar.hidden') ?? '[]')).toEqual(['3:team@group.calendar.google.com'])
    expect(setSyncedCalendars).not.toHaveBeenCalled()
    fireEvent.click(within(screen.getByTestId('calendar-list')).getByLabelText('Team'))
    expect(eventEl(30)).not.toBeNull()
  })

  it('chooses the synced calendars in a dialog (the primary one as "primary")', async () => {
    vi.mocked(getGoogleCalendars).mockResolvedValue([
      { id: 'me@example.com', summary: 'me@example.com', primary: true, selected: true, color: '#9fe1e7' },
      { id: 'team@group.calendar.google.com', summary: 'Team', primary: false, selected: false, color: null },
    ])
    vi.mocked(setSyncedCalendars).mockResolvedValue({ calendar_ids: ['primary', 'team@group.calendar.google.com'] })
    renderAt('/calendar')
    fireEvent.click(screen.getByText('Choose synced calendars…'))
    fireEvent.click(await screen.findByLabelText('Team'))
    await waitFor(() => expect(setSyncedCalendars).toHaveBeenCalledWith(3, ['primary', 'team@group.calendar.google.com']))
  })

  it('says when it last synced, which dates the sync covers, and when the view is outside them', async () => {
    const synced = new Date(Date.now() - 3 * 60_000).toISOString()
    vi.mocked(getCalendarAccounts).mockResolvedValue([account({
      last_sync_at: synced, window: { from: localDateKey(addDays(today, -30)), to: localDateKey(addDays(today, 90)) },
    })])
    renderAt('/calendar')
    await waitFor(() => expect(screen.getByTestId('calendar-sync-info').textContent).toContain('Synced 3m ago · covers'))
    expect(screen.queryByTestId('calendar-window-note')).toBeNull()
    cleanup()
    renderAt(`/calendar?view=week&date=${localDateKey(addDays(today, 120))}`)
    expect((await screen.findByTestId('calendar-window-note')).textContent).toContain('only events synced earlier show')
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
    const later = event(1, { start_at: at(today, 9, 30), end_at: at(today, 10) })
    const allDay = event(4, { all_day: true, start_at: localDateKey(addDays(today, -2)), end_at: localDateKey(addDays(today, 1)) })
    const [first] = groupByDay([later, early, allDay], from, to, false)
    expect(first.events.map((e) => e.id)).toEqual([4, 3, 1])
    expect(groupByDay([allDay], from, to, false)).toHaveLength(1) // only today is in range
  })

  it('view ranges: day, week from Monday, month as six weeks, agenda as 14 days', () => {
    const anchor = new Date(2026, 9, 15) // Thu Oct 15, 2026
    expect(viewRange('day', anchor)).toEqual({ from: anchor, to: new Date(2026, 9, 16) })
    expect(viewRange('week', anchor)).toEqual({ from: new Date(2026, 9, 12), to: new Date(2026, 9, 19) })
    expect(viewRange('month', anchor)).toEqual({ from: new Date(2026, 8, 28), to: new Date(2026, 10, 9) })
    expect(viewRange('agenda', anchor)).toEqual({ from: anchor, to: new Date(2026, 9, 29) })
  })
})
