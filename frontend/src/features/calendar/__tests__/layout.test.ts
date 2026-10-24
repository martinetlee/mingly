import { describe, it, expect } from 'vitest'
import type { CalendarEvent } from '../../../api/calendar'
import { localDateKey } from '../../../api/calendar'
import { addDays, shiftAnchor, viewTitle } from '../agenda'
import {
  allDayLanes, dayTimeLabel, daySegments, eventDayOf, formatRecurrence, isAllDayLike, laneCount, monthCell, packColumns,
} from '../layout'
import { calendarIndex, eventCalendarKey, eventColor, DEFAULT_EVENT_COLOR } from '../visibility'

const day = new Date(2026, 9, 13) // Tue Oct 13, 2026 (local)
const at = (d: Date, h: number, m = 0) => new Date(d.getFullYear(), d.getMonth(), d.getDate(), h, m).toISOString()

function ev(id: number, over: Partial<CalendarEvent> = {}): CalendarEvent {
  return {
    id, account_id: 1, calendar_id: 'primary', title: `E${id}`, description: null, location: null,
    start_at: at(day, 10), end_at: at(day, 11), all_day: false, status: 'confirmed', html_link: null,
    organizer: null, attendees: [], ...over,
  }
}

describe('daySegments', () => {
  it('places a timed event by wall-clock minutes', () => {
    const [s] = daySegments([ev(1, { start_at: at(day, 9, 30), end_at: at(day, 10, 15) })], day)
    expect([s.start, s.end, s.continuesBefore, s.continuesAfter]).toEqual([570, 615, false, false])
  })

  it('clips an event crossing midnight on both days, with continuation flags', () => {
    const late = ev(1, { start_at: at(day, 23), end_at: at(addDays(day, 1), 1) })
    const [first] = daySegments([late], day)
    const [second] = daySegments([late], addDays(day, 1))
    expect([first.start, first.end, first.continuesAfter]).toEqual([1380, 1440, true])
    expect([second.start, second.end, second.continuesBefore]).toEqual([0, 60, true])
  })

  it('an event ending exactly at midnight stays on its day; 24h+ and all-day events are bars, not blocks', () => {
    const toMidnight = ev(1, { start_at: at(day, 22), end_at: addDays(day, 1).toISOString() })
    expect(daySegments([toMidnight], day)[0].end).toBe(1440)
    expect(daySegments([toMidnight], addDays(day, 1))).toEqual([])
    const long = ev(2, { start_at: at(day, 9), end_at: at(addDays(day, 1), 9) })
    expect(isAllDayLike(long)).toBe(true)
    expect(daySegments([long, ev(3, { all_day: true, start_at: localDateKey(day), end_at: localDateKey(addDays(day, 1)) })], day)).toEqual([])
  })
})

describe('packColumns', () => {
  it('overlapping events share the width; a later separate event gets it all', () => {
    const segs = daySegments([
      ev(1, { start_at: at(day, 10), end_at: at(day, 11) }),
      ev(2, { start_at: at(day, 10, 30), end_at: at(day, 12) }),
      ev(3, { start_at: at(day, 11), end_at: at(day, 11, 30) }), // overlaps 2 only: reuses column 0
      ev(4, { start_at: at(day, 13), end_at: at(day, 14) }),
    ], day)
    const placed = Object.fromEntries(packColumns(segs).map((p) => [p.event.id, [p.column, p.columns]]))
    expect(placed).toEqual({ 1: [0, 2], 2: [1, 2], 3: [0, 2], 4: [0, 1] })
  })

  it('a short event counts at its minimum drawn height', () => {
    const segs = daySegments([
      ev(1, { start_at: at(day, 10), end_at: at(day, 10, 5) }),
      ev(2, { start_at: at(day, 10, 10), end_at: at(day, 11) }),
    ], day)
    expect(packColumns(segs).map((p) => p.columns)).toEqual([1, 1])
    expect(packColumns(segs, 25).map((p) => p.columns)).toEqual([2, 2])
  })
})

describe('allDayLanes', () => {
  const monday = new Date(2026, 9, 12)
  const allDay = (id: number, from: number, days: number) => ev(id, {
    all_day: true, start_at: localDateKey(addDays(monday, from)), end_at: localDateKey(addDays(monday, from + days)),
  })

  it('spans bars over their days, clipped to the row, packed into lanes', () => {
    const bars = allDayLanes([allDay(1, 0, 3), allDay(2, 1, 1), allDay(3, 3, 2), allDay(4, -2, 4), allDay(5, 6, 3)], monday, 7)
    const by = Object.fromEntries(bars.map((b) => [b.event.id, [b.startCol, b.span, b.lane]]))
    expect(by[1]).toEqual([0, 3, 0]) // the longest bar of a day takes the top lane
    expect(by[4]).toEqual([0, 2, 1]) // from last week: clipped to the row
    expect(bars.find((b) => b.event.id === 4)!.continuesBefore).toBe(true)
    expect(by[2]).toEqual([1, 1, 2])
    expect(by[3]).toEqual([3, 2, 0])
    expect(by[5]).toEqual([6, 1, 0])
    expect(bars.find((b) => b.event.id === 5)!.continuesAfter).toBe(true)
    expect(laneCount(bars)).toBe(3)
  })

  it('month cells keep a line for "+N more"', () => {
    const monday2 = new Date(2026, 9, 12)
    const bars = allDayLanes([allDay(1, 0, 1), allDay(2, 0, 1), allDay(3, 0, 1), allDay(4, 0, 1)], monday2, 7)
    const timed = daySegments([ev(9, { start_at: at(monday2, 9), end_at: at(monday2, 10) })], monday2)
    // 4 lines: 3 bar lanes; the 4th bar and the timed event are "+2 more"
    expect(monthCell(bars, 0, timed, 4)).toEqual({ shown: [], hidden: 2, usedLines: 3 })
    expect(monthCell([], 0, timed, 4)).toEqual({ shown: timed, hidden: 0, usedLines: 0 })
  })
})

describe('labels', () => {
  it('multi-day events say which day it is', () => {
    const late = ev(1, { start_at: at(day, 23), end_at: at(addDays(day, 2), 1) })
    expect(eventDayOf(late, addDays(day, 1))).toEqual({ n: 2, total: 3 })
    expect(dayTimeLabel(late, day)).toMatch(/^From /)
    expect(dayTimeLabel(late, addDays(day, 1))).toBe('All day')
    expect(dayTimeLabel(late, addDays(day, 2))).toMatch(/^Until /)
    expect(eventDayOf(ev(2), day)).toBeNull()
  })

  it('formats recurrence rules', () => {
    expect(formatRecurrence('RRULE:FREQ=WEEKLY;BYDAY=MO,WE')).toBe('Weekly on Mon, Wed')
    expect(formatRecurrence('RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR')).toBe('Weekly on weekdays')
    expect(formatRecurrence('RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR')).toBe('Every 2 weeks on Fri')
    expect(formatRecurrence('RRULE:FREQ=DAILY;COUNT=5')).toBe('Daily, 5 times')
    expect(formatRecurrence('RRULE:FREQ=MONTHLY;UNTIL=20261231T000000Z')).toMatch(/^Monthly, until /)
    expect(formatRecurrence('EXDATE:20261010\nRRULE:FREQ=YEARLY')).toBe('Yearly')
    expect(formatRecurrence(null)).toBe('Repeats')
    expect(formatRecurrence('RRULE:FREQ=SECONDLY')).toBe('Repeats')
  })

  it('month view steps by month from the 1st; titles per view', () => {
    expect(shiftAnchor('month', new Date(2026, 0, 31), 1)).toEqual(new Date(2026, 1, 1))
    expect(shiftAnchor('day', day, -1)).toEqual(addDays(day, -1))
    expect(viewTitle('month', day, day, day)).toContain('2026')
  })
})

describe('calendar colours', () => {
  const accounts = [{
    account_id: 1,
    calendars: [
      { id: 'me@example.com', name: 'Me', color: '#123456', primary: true },
      { id: 'team', name: 'Team', color: '#abcdef', primary: false },
    ],
  }]

  it('an event takes its own colour, else its calendar\'s (the primary one under either id)', () => {
    const index = calendarIndex(accounts)
    expect(eventColor(ev(1, { color: '#ff0000' }), index)).toBe('#ff0000')
    expect(eventColor(ev(1), index)).toBe('#123456') // calendar_id "primary"
    expect(eventColor(ev(1, { calendar_id: 'team' }), index)).toBe('#abcdef')
    expect(eventColor(ev(1, { calendar_id: 'unknown' }), index)).toBe(DEFAULT_EVENT_COLOR)
    expect(eventCalendarKey(ev(1), index)).toBe('1:me@example.com')
  })
})
