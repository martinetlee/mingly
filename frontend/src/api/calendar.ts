/**
 * Calendar (Google Calendar of connected Gmail accounts): GET /api/calendar/…
 *
 * Event times: `start_at` / `end_at` are UTC ISO instants ("…Z"), except for
 * all-day events (`all_day`), whose times are dates ("YYYY-MM-DD", end
 * exclusive) — a calendar date, not an instant: never shift them by time zone.
 */
import { request } from '../lib/http'

export interface CalendarAttendee {
  identity_id: number
  person_id: number
  name: string
  email: string
  /** accepted | declined | tentative | needsAction (null = organizer not invited) */
  response_status: string | null
  is_organizer: boolean
  is_self: boolean
}

export interface CalendarEvent {
  id: number
  account_id: number
  calendar_id: string | null
  title: string | null
  description: string | null
  location: string | null
  start_at: string | null
  end_at: string | null
  all_day: boolean
  status: string | null
  /** The event in Google Calendar */
  html_link: string | null
  organizer: CalendarAttendee | null
  /** Organizer first */
  attendees: CalendarAttendee[]
}

export interface CalendarAccount {
  account_id: number
  label: string
  calendar_ids: string[]
  /** The account's calendar loop is running */
  syncing: boolean
  last_sync_at: string | null
  error: string | null
}

export interface GoogleCalendar {
  id: string
  summary: string
  primary: boolean
  selected: boolean
  color: string | null
}

/** Id the backend syncs the primary calendar under */
export const PRIMARY_CALENDAR = 'primary'

/** Local calendar date as YYYY-MM-DD */
export function localDateKey(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/**
 * Events overlapping the local days [from, to) (`to` exclusive, both local
 * midnights): timed events by instant, all-day events by date.
 */
export function getCalendarEvents(
  from: Date,
  to: Date,
  opts: { accountId?: number | null; personId?: number | null; signal?: AbortSignal } = {},
) {
  const q = new URLSearchParams({
    start: from.toISOString(), end: to.toISOString(), from_date: localDateKey(from), to_date: localDateKey(to),
  })
  if (opts.accountId != null) q.set('account_id', String(opts.accountId))
  if (opts.personId != null) q.set('person_id', String(opts.personId))
  return request<{ events: CalendarEvent[] }>(`/api/calendar/events?${q}`, { signal: opts.signal })
}

/** Meetings with the person that aren't over yet, earliest first */
export const getUpcomingMeetings = (personId: number, opts: { limit?: number; signal?: AbortSignal } = {}) =>
  request<{ events: CalendarEvent[] }>(
    `/api/calendar/people/${personId}/upcoming?limit=${opts.limit ?? 5}`, { signal: opts.signal },
  )

export const getCalendarAccounts = (signal?: AbortSignal) =>
  request<CalendarAccount[]>('/api/calendar/accounts', { signal })

/** The account's calendars at Google (409 while the account isn't connected) */
export const getGoogleCalendars = (accountId: number, signal?: AbortSignal) =>
  request<GoogleCalendar[]>(`/api/calendar/accounts/${accountId}/calendars`, { signal })

/** Choose the synced calendars (the primary one as PRIMARY_CALENDAR; empty = primary only) */
export const setSyncedCalendars = (accountId: number, calendarIds: string[]) =>
  request<{ calendar_ids: string[] }>(`/api/calendar/accounts/${accountId}/calendars`, {
    method: 'PUT', body: JSON.stringify({ calendar_ids: calendarIds }),
  })

export const syncCalendars = () => request<{ accounts: number }>('/api/calendar/sync', { method: 'POST' })
