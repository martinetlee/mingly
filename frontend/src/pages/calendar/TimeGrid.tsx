/**
 * Day and Week views: a 24-hour time grid (hours down the side, labelled with
 * the viewer's time zone), an all-day row of spanning bars on top, events as
 * blocks sized by length and placed side by side when they overlap, and a
 * now-line on today. Opens scrolled to the selected event, else to now (on
 * today) or 08:00.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Repeat, Video } from 'lucide-react'
import type { CalendarEvent } from '../../api/calendar'
import { localDateKey } from '../../api/calendar'
import { addDays, startOfLocalDay } from '../../features/calendar/agenda'
import {
  allDayLanes, daySegments, laneCount, packColumns, segmentLabel, timeZoneLabel,
} from '../../features/calendar/layout'
import { chipStyle, SELECTED_RING } from './EventChip'

export const HOUR_PX = 48
const MIN_BLOCK_PX = 20
const LANE_PX = 22
const GUTTER = 'w-14'

interface Props {
  days: Date[]
  events: CalendarEvent[]
  colorOf: (ev: CalendarEvent) => string
  selectedId: number | null
  onSelect: (id: number) => void
  /** Open a day in the Day view (from the week's day headers) */
  onOpenDay?: (day: Date) => void
}

function useNow(): Date {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 60_000)
    return () => clearInterval(t)
  }, [])
  return now
}

export default function TimeGrid({ days, events, colorOf, selectedId, onSelect, onOpenDay }: Props) {
  const now = useNow()
  const todayKey = localDateKey(now)
  const from = days[0]
  const bars = useMemo(() => allDayLanes(events, from, days.length), [events, from, days.length])
  const lanes = Math.max(laneCount(bars), 1)
  const columns = useMemo(
    () => days.map((d) => packColumns(daySegments(events, d), (MIN_BLOCK_PX / HOUR_PX) * 60)),
    [events, days],
  )
  const tz = timeZoneLabel(from)

  // Scroll once per range: to the selected event, else now (today shown) or 08:00
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const rangeKey = `${localDateKey(from)}:${days.length}`
  const scrolledFor = useRef<string | null>(null)
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el || scrolledFor.current === `${rangeKey}:${selectedId ?? ''}`) return
    scrolledFor.current = `${rangeKey}:${selectedId ?? ''}`
    const selected = columns.flat().find((s) => s.event.id === selectedId)
    const minutes = selected ? selected.start
      : days.some((d) => localDateKey(d) === todayKey) ? now.getHours() * 60 + now.getMinutes()
      : 8 * 60
    el.scrollTop = Math.max(minutes / 60 * HOUR_PX - HOUR_PX, 0)
  }, [rangeKey, selectedId, columns, days, todayKey, now])

  const nowMinutes = now.getHours() * 60 + now.getMinutes()

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="time-grid">
      {/* Day headers */}
      <div className="flex border-b border-slate-700/50">
        <div className={`${GUTTER} flex-shrink-0`} />
        {days.map((d) => {
          const isToday = localDateKey(d) === todayKey
          return (
            <button
              key={localDateKey(d)}
              type="button"
              onClick={() => onOpenDay?.(d)}
              disabled={!onOpenDay}
              className="flex min-w-0 flex-1 flex-col items-center py-1.5 text-slate-400 enabled:hover:bg-slate-800/60"
              data-testid="grid-day-header"
            >
              <span className={`text-[11px] uppercase tracking-wider ${isToday ? 'text-indigo-300' : ''}`}>
                {d.toLocaleDateString([], { weekday: 'short' })}
              </span>
              <span className={`mt-0.5 flex h-7 w-7 items-center justify-center rounded-full text-sm ${
                isToday ? 'bg-indigo-500 font-semibold text-white' : 'text-slate-200'
              }`}>
                {d.getDate()}
              </span>
            </button>
          )
        })}
      </div>

      {/* All-day row */}
      <div className="flex border-b border-slate-700/50">
        <div className={`${GUTTER} flex-shrink-0 py-1 pr-2 text-right text-[10px] text-slate-500`}>all-day</div>
        <div className="relative flex-1" style={{ height: lanes * LANE_PX + 4 }} data-testid="all-day-row">
          <div className="absolute inset-0 flex">
            {days.map((d) => <div key={localDateKey(d)} className="flex-1 border-l border-slate-700/40" />)}
          </div>
          {bars.map((b) => {
            const { style, className } = chipStyle(b.event, colorOf(b.event))
            return (
              <button
                key={b.event.id}
                type="button"
                onClick={() => onSelect(b.event.id)}
                className={`absolute truncate rounded px-1.5 text-left text-[11px] leading-[18px] ${className} ${
                  b.event.id === selectedId ? SELECTED_RING : ''
                } ${b.continuesBefore ? 'rounded-l-none' : ''} ${b.continuesAfter ? 'rounded-r-none' : ''}`}
                style={{
                  ...style,
                  top: 2 + b.lane * LANE_PX,
                  height: LANE_PX - 4,
                  left: `calc(${(b.startCol / days.length) * 100}% + 2px)`,
                  width: `calc(${(b.span / days.length) * 100}% - 4px)`,
                }}
                title={b.event.title ?? '(no title)'}
                data-testid="calendar-event"
                data-event-id={b.event.id}
              >
                {b.continuesBefore ? '‹ ' : ''}{b.event.title || '(no title)'}
              </button>
            )
          })}
        </div>
      </div>

      {/* Hours */}
      <div ref={scrollRef} className="relative flex-1 overflow-y-auto" data-testid="time-grid-scroll">
        <div className="relative flex" style={{ height: 24 * HOUR_PX }}>
          <div className={`${GUTTER} relative flex-shrink-0`}>
            <span className="absolute right-2 top-0.5 text-[10px] text-slate-600" title="Your time zone">{tz}</span>
            {Array.from({ length: 23 }, (_, i) => i + 1).map((h) => (
              <span key={h} className="absolute right-2 -translate-y-1/2 text-[10px] tabular-nums text-slate-500" style={{ top: h * HOUR_PX }}>
                {new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: 'numeric' })}
              </span>
            ))}
          </div>
          {days.map((d, i) => {
            const isToday = localDateKey(d) === todayKey
            return (
              <div key={localDateKey(d)} className="relative flex-1 border-l border-slate-700/40" data-testid="grid-day" data-day={localDateKey(d)}>
                {Array.from({ length: 24 }, (_, h) => (
                  <div key={h} className="absolute inset-x-0 border-t border-slate-700/30" style={{ top: h * HOUR_PX }} />
                ))}
                {columns[i].map((seg) => {
                  const ev = seg.event
                  const top = (seg.start / 60) * HOUR_PX
                  const height = Math.max(((seg.end - seg.start) / 60) * HOUR_PX, MIN_BLOCK_PX)
                  const { style, className } = chipStyle(ev, colorOf(ev))
                  const compact = height < 38
                  return (
                    <button
                      key={`${ev.id}:${seg.start}`}
                      type="button"
                      onClick={() => onSelect(ev.id)}
                      className={`absolute overflow-hidden rounded px-1.5 py-0.5 text-left text-[11px] leading-tight ${className} ${
                        ev.id === selectedId ? `${SELECTED_RING} z-10` : ''
                      }`}
                      style={{
                        ...style,
                        top: top + 1,
                        height: height - 2,
                        left: `calc(${(seg.column / seg.columns) * 100}% + 1px)`,
                        width: `calc(${100 / seg.columns}% - 3px)`,
                      }}
                      title={`${ev.title || '(no title)'} · ${segmentLabel(seg)}`}
                      data-testid="calendar-event"
                      data-event-id={ev.id}
                    >
                      <span className={`flex items-center gap-1 ${compact ? 'truncate' : ''}`}>
                        <span className="truncate font-medium">{ev.title || '(no title)'}</span>
                        {compact && <span className="flex-shrink-0 opacity-75">{segmentLabel(seg).split(' – ')[0]}</span>}
                        {ev.recurring && <Repeat size={10} className="flex-shrink-0 opacity-60" aria-label="Recurring" />}
                        {ev.conference_url && <Video size={10} className="flex-shrink-0 opacity-75" aria-label="Video call" />}
                      </span>
                      {!compact && <span className="block truncate opacity-75">{segmentLabel(seg)}</span>}
                      {!compact && height > 56 && ev.location && <span className="block truncate opacity-60">{ev.location}</span>}
                    </button>
                  )
                })}
                {isToday && (
                  <div className="pointer-events-none absolute inset-x-0 z-20" style={{ top: (nowMinutes / 60) * HOUR_PX }} data-testid="now-line">
                    <div className="absolute -left-1 -top-1 h-2 w-2 rounded-full bg-red-500" />
                    <div className="border-t-2 border-red-500" />
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

/** The days a Day / Week view shows */
export function gridDays(from: Date, to: Date): Date[] {
  const out: Date[] = []
  for (let d = startOfLocalDay(from); d < to; d = addDays(d, 1)) out.push(d)
  return out
}

