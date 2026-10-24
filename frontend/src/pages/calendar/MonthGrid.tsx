/**
 * Month view: six weeks from the Monday on or before the 1st. Multi-day and
 * all-day events are bars across the days they cover; timed events are
 * "● 09:00 Title" lines. What doesn't fit becomes "+N more", which opens
 * that day in the Day view. Days of other months are dimmed; today is circled.
 */
import { useMemo } from 'react'
import type { CalendarEvent } from '../../api/calendar'
import { localDateKey } from '../../api/calendar'
import { addDays, MONTH_WEEKS } from '../../features/calendar/agenda'
import { allDayLanes, daySegments, monthBarLanes, monthCell } from '../../features/calendar/layout'
import { fmtTime } from '../../utils/time'
import { chipStyle, ColorDot, eventState, SELECTED_RING } from './EventChip'

/** Lines per day cell (bars and timed events, "+N more" included) */
const CELL_LINES = 4
const LINE_PX = 20
const DATE_PX = 24

interface Props {
  from: Date
  /** The month shown (its days are full strength) */
  month: number
  events: CalendarEvent[]
  colorOf: (ev: CalendarEvent) => string
  selectedId: number | null
  onSelect: (id: number) => void
  onOpenDay: (day: Date) => void
}

export default function MonthGrid({ from, month, events, colorOf, selectedId, onSelect, onOpenDay }: Props) {
  const todayKey = localDateKey(new Date())
  const weeks = useMemo(() => Array.from({ length: MONTH_WEEKS }, (_, w) => {
    const start = addDays(from, w * 7)
    const days = Array.from({ length: 7 }, (_, i) => addDays(start, i))
    return { start, days, bars: allDayLanes(events, start, 7), segments: days.map((d) => daySegments(events, d)) }
  }), [from, events])

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="month-grid">
      <div className="grid grid-cols-7 border-b border-slate-700/50">
        {weeks[0].days.map((d) => (
          <div key={d.getDay()} className="py-1.5 text-center text-[11px] uppercase tracking-wider text-slate-500">
            {d.toLocaleDateString([], { weekday: 'short' })}
          </div>
        ))}
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        {weeks.map((week) => (
          <div
            key={localDateKey(week.start)}
            className="relative grid flex-1 grid-cols-7 border-b border-slate-700/40"
            style={{ minHeight: DATE_PX + CELL_LINES * LINE_PX + 6 }}
            data-testid="month-week"
          >
            {week.days.map((d, col) => {
              const { shown, hidden, usedLines } = monthCell(week.bars, col, week.segments[col], CELL_LINES)
              const isToday = localDateKey(d) === todayKey
              return (
                <div
                  key={localDateKey(d)}
                  className={`min-w-0 border-l border-slate-700/40 px-1 pb-1 ${d.getMonth() === month ? '' : 'bg-slate-900/60'}`}
                  data-testid="month-day"
                  data-day={localDateKey(d)}
                >
                  <button
                    type="button"
                    onClick={() => onOpenDay(d)}
                    className={`mx-auto mt-0.5 flex h-6 w-6 items-center justify-center rounded-full text-xs hover:bg-slate-700/60 ${
                      isToday ? 'bg-indigo-500 font-semibold text-white hover:bg-indigo-500'
                        : d.getMonth() === month ? 'text-slate-200' : 'text-slate-600'
                    }`}
                    aria-label={d.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' })}
                  >
                    {d.getDate()}
                  </button>
                  <div style={{ marginTop: usedLines * LINE_PX }}>
                    {shown.map((seg) => {
                      const ev = seg.event
                      const declined = eventState(ev) === 'declined'
                      return (
                        <button
                          key={ev.id}
                          type="button"
                          onClick={() => onSelect(ev.id)}
                          className={`flex w-full items-center gap-1 truncate rounded px-1 text-left text-[11px] leading-[20px] hover:bg-slate-700/50 ${
                            declined ? 'text-slate-500 line-through' : 'text-slate-300'
                          } ${ev.id === selectedId ? SELECTED_RING : ''}`}
                          style={{ height: LINE_PX }}
                          title={ev.title ?? '(no title)'}
                          data-testid="calendar-event"
                          data-event-id={ev.id}
                        >
                          <ColorDot color={colorOf(ev)} />
                          {!seg.continuesBefore && <span className="flex-shrink-0 tabular-nums text-slate-500">{fmtTime(ev.start_at)}</span>}
                          <span className="truncate">{ev.title || '(no title)'}</span>
                        </button>
                      )
                    })}
                    {hidden > 0 && (
                      <button
                        type="button"
                        onClick={() => onOpenDay(d)}
                        className="w-full rounded px-1 text-left text-[11px] font-medium leading-[20px] text-slate-400 hover:bg-slate-700/50 hover:text-slate-200"
                        data-testid="month-more"
                      >
                        +{hidden} more
                      </button>
                    )}
                  </div>
                </div>
              )
            })}
            {week.bars.filter((b) => b.lane < monthBarLanes(CELL_LINES)).map((b) => {
              const { style, className } = chipStyle(b.event, colorOf(b.event))
              return (
                <button
                  key={b.event.id}
                  type="button"
                  onClick={() => onSelect(b.event.id)}
                  className={`absolute truncate rounded px-1.5 text-left text-[11px] leading-[16px] ${className} ${
                    b.event.id === selectedId ? SELECTED_RING : ''
                  } ${b.continuesBefore ? 'rounded-l-none' : ''} ${b.continuesAfter ? 'rounded-r-none' : ''}`}
                  style={{
                    ...style,
                    top: DATE_PX + 2 + b.lane * LINE_PX,
                    height: LINE_PX - 3,
                    left: `calc(${(b.startCol / 7) * 100}% + 2px)`,
                    width: `calc(${(b.span / 7) * 100}% - 4px)`,
                  }}
                  title={b.event.title ?? '(no title)'}
                  data-testid="calendar-event"
                  data-event-id={b.event.id}
                >
                  {b.event.all_day || b.continuesBefore ? '' : `${fmtTime(b.event.start_at)} `}{b.event.title || '(no title)'}
                </button>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}
