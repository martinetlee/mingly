/**
 * Calendar date math: view ranges (day, week, month, agenda) in local days and
 * grouping events by the local day(s) they fall on. All-day events are dates ("YYYY-MM-DD", end
 * exclusive) and are placed by date, never shifted by time zone.
 */
import { localDateKey, type CalendarEvent } from '../../api/calendar'
import { fmtTime, parseServerDate } from '../../utils/time'

export type CalendarView = 'day' | 'week' | 'month' | 'agenda'

export const CALENDAR_VIEWS: CalendarView[] = ['day', 'week', 'month', 'agenda']

/** Days shown by the agenda view */
export const AGENDA_DAYS = 14

/** Weeks shown by the month view (every month fits in six) */
export const MONTH_WEEKS = 6

export function parseView(value: string | null | undefined): CalendarView | null {
  return CALENDAR_VIEWS.includes(value as CalendarView) ? value as CalendarView : null
}

export function startOfLocalDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate())
}

export function addDays(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n)
}

/** "YYYY-MM-DD" → local midnight (null when invalid) */
export function parseDateKey(key: string | null | undefined): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(key ?? '')
  if (!m) return null
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
  return Number.isNaN(d.getTime()) || localDateKey(d) !== key ? null : d
}

/** Monday of the week containing `d` */
export function startOfWeek(d: Date): Date {
  const day = startOfLocalDay(d)
  return addDays(day, -((day.getDay() + 6) % 7))
}

/** The local days [from, to) a view shows around `anchor` */
export function viewRange(view: CalendarView, anchor: Date): { from: Date; to: Date } {
  switch (view) {
    case 'day': {
      const from = startOfLocalDay(anchor)
      return { from, to: addDays(from, 1) }
    }
    case 'week': {
      const from = startOfWeek(anchor)
      return { from, to: addDays(from, 7) }
    }
    case 'month': {
      const from = startOfWeek(new Date(anchor.getFullYear(), anchor.getMonth(), 1))
      return { from, to: addDays(from, 7 * MONTH_WEEKS) }
    }
    default: {
      const from = startOfLocalDay(anchor)
      return { from, to: addDays(from, AGENDA_DAYS) }
    }
  }
}

/** Where prev / next go from `anchor` */
export function shiftAnchor(view: CalendarView, anchor: Date, dir: -1 | 1): Date {
  switch (view) {
    case 'day': return addDays(anchor, dir)
    case 'week': return addDays(anchor, dir * 7)
    // The 1st of the next / previous month (a 31st has no equivalent in every month)
    case 'month': return new Date(anchor.getFullYear(), anchor.getMonth() + dir, 1)
    default: return addDays(anchor, dir * AGENDA_DAYS)
  }
}

/** The header title: "Tue, Oct 27, 2026" · "Oct 26 – Nov 1, 2026" · "October 2026" */
export function viewTitle(view: CalendarView, anchor: Date, from: Date, to: Date): string {
  if (view === 'day') {
    return anchor.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' })
  }
  if (view === 'month') return anchor.toLocaleDateString([], { month: 'long', year: 'numeric' })
  return rangeLabel(from, to)
}

/** Local day keys of [from, to) that `ev` falls on */
export function eventDayKeys(ev: CalendarEvent, from: Date, to: Date): string[] {
  let first: Date | null
  let lastExclusive: Date | null
  if (ev.all_day) {
    first = parseDateKey(ev.start_at)
    const end = parseDateKey(ev.end_at)
    lastExclusive = end && first && end > first ? end : first && addDays(first, 1)
  } else {
    const start = parseServerDate(ev.start_at)
    const end = parseServerDate(ev.end_at)
    if (!start) return []
    first = startOfLocalDay(start)
    // An event ending exactly at midnight doesn't spill into the next day
    const lastInstant = end && end > start ? new Date(end.getTime() - 1) : start
    lastExclusive = addDays(startOfLocalDay(lastInstant), 1)
  }
  if (!first || !lastExclusive) return []
  const keys: string[] = []
  for (let d = first < from ? from : first; d < lastExclusive && d < to; d = addDays(d, 1)) {
    keys.push(localDateKey(d))
  }
  return keys
}

export interface DayGroup {
  key: string
  date: Date
  events: CalendarEvent[]
}

/**
 * Events grouped by local day over [from, to): all-day events first, then by
 * start. `includeEmpty` keeps days without events (week view).
 */
export function groupByDay(events: CalendarEvent[], from: Date, to: Date, includeEmpty: boolean): DayGroup[] {
  const byDay = new Map<string, CalendarEvent[]>()
  for (let d = from; d < to; d = addDays(d, 1)) byDay.set(localDateKey(d), [])
  for (const ev of events) {
    for (const key of eventDayKeys(ev, from, to)) byDay.get(key)?.push(ev)
  }
  const startMs = (ev: CalendarEvent) => parseServerDate(ev.start_at)?.getTime() ?? 0
  const out: DayGroup[] = []
  for (const [key, list] of byDay) {
    if (!includeEmpty && list.length === 0) continue
    list.sort((a, b) => Number(b.all_day) - Number(a.all_day) || startMs(a) - startMs(b) || a.id - b.id)
    out.push({ key, date: parseDateKey(key)!, events: list })
  }
  return out
}

/** "All day" · "09:00 – 10:00" */
export function eventTimeLabel(ev: CalendarEvent): string {
  if (ev.all_day) return 'All day'
  const start = fmtTime(ev.start_at)
  const end = fmtTime(ev.end_at)
  return end && end !== start ? `${start} – ${end}` : start
}

/** "Today" · "Tomorrow" · "Mon, Oct 12" */
export function dayLabel(date: Date, now: Date = new Date()): string {
  const today = startOfLocalDay(now)
  if (date.getTime() === today.getTime()) return 'Today'
  if (date.getTime() === addDays(today, 1).getTime()) return 'Tomorrow'
  if (date.getTime() === addDays(today, -1).getTime()) return 'Yesterday'
  return date.toLocaleDateString([], {
    weekday: 'short', month: 'short', day: 'numeric',
    ...(date.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}),
  })
}

/** Range title: "Oct 12 – 18, 2026" style via the locale */
export function rangeLabel(from: Date, to: Date): string {
  const last = addDays(to, -1)
  const opts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' }
  return `${from.toLocaleDateString([], opts)} – ${last.toLocaleDateString([], { ...opts, year: 'numeric' })}`
}

/** The event in the Calendar space (its week, details open): /calendar?view=week&date=…&event=… */
export function calendarEventUrl(ev: Pick<CalendarEvent, 'id' | 'start_at' | 'all_day'>): string {
  const day = ev.all_day ? parseDateKey(ev.start_at) : parseServerDate(ev.start_at)
  const q = new URLSearchParams({ view: 'week' })
  if (day) q.set('date', localDateKey(day))
  q.set('event', String(ev.id))
  return `/calendar?${q}`
}

/** A day in the Calendar space: /calendar?view=day&date=… */
export function calendarDayUrl(day: Date): string {
  return `/calendar?${new URLSearchParams({ view: 'day', date: localDateKey(day) })}`
}
