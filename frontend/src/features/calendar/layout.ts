/**
 * Calendar grid layout (pure): where events go in the Day / Week time grid
 * and the Month grid.
 *
 * - Timed events shorter than a day are blocks in the time grid, clipped to
 *   each local day they touch (`daySegments`) and placed side by side when
 *   they overlap (`packColumns`, the usual cluster → columns layout).
 * - All-day events, and timed ones lasting 24h or more, are bars spanning
 *   days in a row of lanes (`allDayLanes`): the week's all-day row and each
 *   week of the month.
 *
 * Positions are wall-clock minutes of the local day, so a DST day still maps
 * 09:00 to the 09:00 line.
 */
import type { CalendarEvent } from '../../api/calendar'
import { fmtTime, parseServerDate } from '../../utils/time'
import { addDays, eventTimeLabel, parseDateKey, startOfLocalDay } from './agenda'

export const DAY_MINUTES = 24 * 60

/** The event's [start, end) as local Dates (all-day: local midnights; end ≥ start) */
export function eventBounds(ev: CalendarEvent): { start: Date; end: Date } | null {
  if (ev.all_day) {
    const start = parseDateKey(ev.start_at)
    if (!start) return null
    const end = parseDateKey(ev.end_at)
    return { start, end: end && end > start ? end : addDays(start, 1) }
  }
  const start = parseServerDate(ev.start_at)
  if (!start) return null
  const end = parseServerDate(ev.end_at)
  return { start, end: end && end > start ? end : start }
}

/** Shown as a bar in the all-day row: all-day, or timed and 24 hours or longer */
export function isAllDayLike(ev: CalendarEvent): boolean {
  if (ev.all_day) return true
  const b = eventBounds(ev)
  return b != null && b.end.getTime() - b.start.getTime() >= DAY_MINUTES * 60_000
}

const wallMinutes = (d: Date) => d.getHours() * 60 + d.getMinutes()

export interface DaySegment {
  event: CalendarEvent
  /** Wall-clock minutes from the day's midnight, [start, end) */
  start: number
  end: number
  /** The event started on an earlier day / goes on past midnight */
  continuesBefore: boolean
  continuesAfter: boolean
}

/** The timed (non-all-day-like) events' parts that fall on `day`, by start */
export function daySegments(events: CalendarEvent[], day: Date): DaySegment[] {
  const dayStart = startOfLocalDay(day)
  const dayEnd = addDays(dayStart, 1)
  const out: DaySegment[] = []
  for (const ev of events) {
    if (isAllDayLike(ev)) continue
    const b = eventBounds(ev)
    if (!b) continue
    const zeroLength = b.end.getTime() === b.start.getTime()
    // Overlaps [dayStart, dayEnd); a zero-length event belongs to the day it starts on
    if (zeroLength ? !(b.start >= dayStart && b.start < dayEnd) : !(b.start < dayEnd && b.end > dayStart)) continue
    const continuesBefore = b.start < dayStart
    const continuesAfter = b.end > dayEnd
    const start = continuesBefore ? 0 : wallMinutes(b.start)
    let end = continuesAfter || b.end.getTime() === dayEnd.getTime() ? DAY_MINUTES : wallMinutes(b.end)
    if (end < start) end = DAY_MINUTES // a DST jump
    out.push({ event: ev, start, end, continuesBefore, continuesAfter })
  }
  return out.sort((a, b) => a.start - b.start || b.end - a.end || a.event.id - b.event.id)
}

export interface PlacedSegment extends DaySegment {
  /** Column within its overlap cluster, and how many columns the cluster has */
  column: number
  columns: number
}

/**
 * Side-by-side placement of overlapping segments: segments that overlap
 * (transitively) form a cluster; each takes the first free column and every
 * segment of the cluster shares its width. `minDuration` (minutes) is how
 * long a short event counts as, since it's drawn at a minimum height.
 */
export function packColumns(segments: DaySegment[], minDuration = 0): PlacedSegment[] {
  const sorted = [...segments].sort((a, b) => a.start - b.start || b.end - a.end || a.event.id - b.event.id)
  const out: PlacedSegment[] = []
  let cluster: PlacedSegment[] = []
  let columnEnds: number[] = []
  let clusterEnd = -1
  const close = () => {
    for (const s of cluster) s.columns = columnEnds.length
    cluster = []
    columnEnds = []
  }
  for (const seg of sorted) {
    const end = Math.max(seg.end, seg.start + minDuration)
    if (seg.start >= clusterEnd) close()
    let column = columnEnds.findIndex((e) => e <= seg.start)
    if (column === -1) {
      column = columnEnds.length
      columnEnds.push(end)
    } else {
      columnEnds[column] = end
    }
    const placed = { ...seg, column, columns: 1 }
    cluster.push(placed)
    out.push(placed)
    clusterEnd = Math.max(clusterEnd, end)
  }
  close()
  return out
}

export interface LaneBar {
  event: CalendarEvent
  /** First day index within the row, and how many days the bar covers */
  startCol: number
  span: number
  lane: number
  continuesBefore: boolean
  continuesAfter: boolean
}

/** Whole local days between two local midnights (DST-safe) */
function dayIndex(from: Date, d: Date): number {
  return Math.round((startOfLocalDay(d).getTime() - from.getTime()) / 86_400_000)
}

/**
 * All-day-like events over the row of `days` days starting `from`, as bars
 * packed into lanes (longest first within a start day, like Google).
 */
export function allDayLanes(events: CalendarEvent[], from: Date, days: number): LaneBar[] {
  const items: Omit<LaneBar, 'lane'>[] = []
  for (const ev of events) {
    if (!isAllDayLike(ev)) continue
    const b = eventBounds(ev)
    if (!b) continue
    const first = dayIndex(from, b.start)
    // The last day touched: an end at midnight (all-day ends always are) doesn't count
    const lastInstant = new Date(Math.max(b.end.getTime() - 1, b.start.getTime()))
    const last = dayIndex(from, lastInstant)
    if (last < 0 || first >= days) continue
    const startCol = Math.max(first, 0)
    const endCol = Math.min(last, days - 1)
    items.push({ event: ev, startCol, span: endCol - startCol + 1, continuesBefore: first < 0, continuesAfter: last >= days })
  }
  items.sort((a, b) => a.startCol - b.startCol || b.span - a.span || a.event.id - b.event.id)
  const laneEnds: number[] = []
  return items.map((item) => {
    let lane = laneEnds.findIndex((end) => end <= item.startCol)
    if (lane === -1) {
      lane = laneEnds.length
      laneEnds.push(0)
    }
    laneEnds[lane] = item.startCol + item.span
    return { ...item, lane }
  })
}

/** How many lanes the bars use (rows needed for the all-day area) */
export function laneCount(bars: LaneBar[]): number {
  return bars.reduce((n, b) => Math.max(n, b.lane + 1), 0)
}

/** Bars covering day `col` of the row */
export function barsOnDay(bars: LaneBar[], col: number): LaneBar[] {
  return bars.filter((b) => b.startCol <= col && col < b.startCol + b.span)
}

/**
 * One month cell's content within `rows` lines. Bars keep their lane line
 * (lanes from `rows - 1` on are hidden, so "+N more" always has a line);
 * timed events fill the lines left. Returns the timed segments shown, how
 * many items (bars or timed) don't fit, and the lines the bars take on top.
 */
export function monthCell(bars: LaneBar[], col: number, segments: DaySegment[], rows: number) {
  const barLanes = rows - 1
  const onDay = barsOnDay(bars, col)
  const hiddenBars = onDay.filter((b) => b.lane >= barLanes).length
  const usedLines = onDay.reduce((n, b) => (b.lane < barLanes ? Math.max(n, b.lane + 1) : n), 0)
  const free = rows - usedLines
  if (segments.length <= free && hiddenBars === 0) return { shown: segments, hidden: 0, usedLines }
  // Something doesn't fit: leave a line for "+N more"
  const shown = segments.slice(0, Math.max(free - 1, 0))
  return { shown, hidden: hiddenBars + segments.length - shown.length, usedLines }
}

/** Bar lanes a month cell of `rows` lines draws */
export const monthBarLanes = (rows: number) => rows - 1

const WEEKDAY: Record<string, string> = { MO: 'Mon', TU: 'Tue', WE: 'Wed', TH: 'Thu', FR: 'Fri', SA: 'Sat', SU: 'Sun' }
const UNIT: Record<string, [string, string]> = {
  DAILY: ['Daily', 'days'], WEEKLY: ['Weekly', 'weeks'], MONTHLY: ['Monthly', 'months'], YEARLY: ['Yearly', 'years'],
}

/** An RRULE in words: "Weekly on Mon, Wed", "Every 2 weeks", … ("Repeats" when unknown) */
export function formatRecurrence(rule: string | null | undefined): string {
  const line = (rule ?? '').split(/\r?\n/).find((l) => /^RRULE:/i.test(l.trim()))
  if (!line) return 'Repeats'
  const parts = new Map(line.trim().slice(6).split(';').map((kv) => {
    const [k, v] = kv.split('=')
    return [k?.toUpperCase(), v ?? ''] as [string, string]
  }))
  const unit = UNIT[(parts.get('FREQ') ?? '').toUpperCase()]
  if (!unit) return 'Repeats'
  const interval = Number(parts.get('INTERVAL') ?? '1') || 1
  let text = interval > 1 ? `Every ${interval} ${unit[1]}` : unit[0]
  const byDay = (parts.get('BYDAY') ?? '').split(',').filter(Boolean)
  const plainDays = byDay.map((d) => WEEKDAY[d.toUpperCase()]).filter(Boolean)
  if (unit[0] === 'Weekly' && plainDays.length > 0) {
    text += ` on ${plainDays.length === 5 && !byDay.some((d) => /SA|SU/i.test(d)) ? 'weekdays' : plainDays.join(', ')}`
  }
  if (parts.get('COUNT')) text += `, ${parts.get('COUNT')} times`
  else if (parts.get('UNTIL')) {
    const m = /^(\d{4})(\d{2})(\d{2})/.exec(parts.get('UNTIL') ?? '')
    if (m) {
      const until = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
      text += `, until ${until.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })}`
    }
  }
  return text
}

/** The viewer's time zone as a short label ("GMT+8", "PDT") */
export function timeZoneLabel(at: Date = new Date()): string {
  try {
    const part = new Intl.DateTimeFormat([], { timeZoneName: 'short' }).formatToParts(at).find((p) => p.type === 'timeZoneName')
    return part?.value ?? ''
  } catch {
    return ''
  }
}

/** Which day of a multi-day event `day` is ({ n: 2, total: 3 }); null for a one-day event */
export function eventDayOf(ev: CalendarEvent, day: Date): { n: number; total: number } | null {
  const b = eventBounds(ev)
  if (!b) return null
  const first = startOfLocalDay(b.start)
  const lastInstant = new Date(Math.max(b.end.getTime() - 1, b.start.getTime()))
  const total = dayIndex(first, lastInstant) + 1
  if (total <= 1) return null
  return { n: dayIndex(first, day) + 1, total }
}

/**
 * The time of an event on one of its days: "09:00 – 10:00" on a one-day
 * event; "From 23:00" / "All day" / "Until 01:00" across days.
 */
export function dayTimeLabel(ev: CalendarEvent, day: Date): string {
  if (ev.all_day) return 'All day'
  const part = eventDayOf(ev, day)
  if (!part) return eventTimeLabel(ev)
  if (part.n === 1) return `From ${fmtTime(ev.start_at)}`
  if (part.n === part.total) return `Until ${fmtTime(ev.end_at)}`
  return 'All day'
}

/** "09:00 – 10:00" for a segment of the time grid (its clipped part on that day) */
export function segmentLabel(seg: DaySegment): string {
  const at = (minutes: number) => {
    const d = new Date(2000, 0, 1, Math.floor(minutes / 60), minutes % 60)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
  if (seg.continuesBefore && seg.continuesAfter) return 'All day'
  if (seg.end === seg.start) return at(seg.start)
  return `${at(seg.start)} – ${seg.end >= DAY_MINUTES ? at(0) : at(seg.end)}`
}
