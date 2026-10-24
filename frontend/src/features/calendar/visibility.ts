/**
 * Calendar view preferences kept in this browser: the calendars hidden from
 * view (a view filter: their events stay synced) and the last view used.
 * Storage may be unavailable; everything falls back to the defaults.
 */
import { useCallback, useState } from 'react'
import type { CalendarEvent, SyncedCalendar } from '../../api/calendar'
import { PRIMARY_CALENDAR } from '../../api/calendar'
import { parseView, type CalendarView } from './agenda'

const HIDDEN_KEY = 'mingly.calendar.hidden'
const VIEW_KEY = 'mingly.calendar.view'

/** Default colour of an event whose calendar's colour isn't known (indigo-500) */
export const DEFAULT_EVENT_COLOR = '#6366f1'

/** Key of a calendar of an account (`calendar_id` as events carry it) */
export function calendarKey(accountId: number, calendarId: string | null | undefined): string {
  return `${accountId}:${calendarId ?? PRIMARY_CALENDAR}`
}

function readHidden(): Set<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(HIDDEN_KEY) ?? '[]')
    return new Set(Array.isArray(raw) ? raw.filter((k): k is string => typeof k === 'string') : [])
  } catch {
    return new Set()
  }
}

/** The hidden calendars and a toggle (saved per browser) */
export function useHiddenCalendars() {
  const [hidden, setHidden] = useState<Set<string>>(readHidden)
  const toggle = useCallback((key: string) => {
    setHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      try {
        localStorage.setItem(HIDDEN_KEY, JSON.stringify([...next]))
      } catch {
        // not persisted; still applies for this visit
      }
      return next
    })
  }, [])
  return { hidden, toggle }
}

export function lastView(): CalendarView | null {
  try {
    return parseView(localStorage.getItem(VIEW_KEY))
  } catch {
    return null
  }
}

export function rememberView(view: CalendarView): void {
  try {
    localStorage.setItem(VIEW_KEY, view)
  } catch {
    // not persisted
  }
}

/** Synced calendars of the accounts by `calendarKey` */
export function calendarIndex(accounts: { account_id: number; calendars?: SyncedCalendar[] }[]): Map<string, SyncedCalendar> {
  const out = new Map<string, SyncedCalendar>()
  for (const a of accounts) {
    for (const c of a.calendars ?? []) {
      out.set(calendarKey(a.account_id, c.id), c)
      // Events may carry the primary calendar under either id
      if (c.primary) {
        out.set(calendarKey(a.account_id, PRIMARY_CALENDAR), c)
      }
    }
  }
  return out
}

/** The key the visibility filter uses for the event's calendar */
export function eventCalendarKey(ev: CalendarEvent, calendars: Map<string, SyncedCalendar>): string {
  const cal = calendars.get(calendarKey(ev.account_id, ev.calendar_id))
  return calendarKey(ev.account_id, cal?.id ?? ev.calendar_id)
}

/** The colour to draw an event in: its own, else its calendar's */
export function eventColor(ev: CalendarEvent, calendars: Map<string, SyncedCalendar>): string {
  return ev.color || calendars.get(calendarKey(ev.account_id, ev.calendar_id))?.color || DEFAULT_EVENT_COLOR
}
