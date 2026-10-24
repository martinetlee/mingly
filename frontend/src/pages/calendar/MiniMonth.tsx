/**
 * The sidebar's small month: jump to any day. Days the main view shows are
 * tinted, today is circled; ‹ › page through months without moving the view.
 */
import { useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { localDateKey } from '../../api/calendar'
import { addDays, startOfWeek } from '../../features/calendar/agenda'

interface Props {
  /** The day the main view is anchored on (the mini month follows it) */
  anchor: Date
  /** The range the main view shows, [from, to) */
  from: Date
  to: Date
  onPick: (day: Date) => void
}

export default function MiniMonth({ anchor, from, to, onPick }: Props) {
  const [month, setMonth] = useState(() => new Date(anchor.getFullYear(), anchor.getMonth(), 1))
  // Follow the main view when it moves to another month
  const anchorMonth = `${anchor.getFullYear()}-${anchor.getMonth()}`
  const [followed, setFollowed] = useState(anchorMonth)
  if (followed !== anchorMonth) {
    setFollowed(anchorMonth)
    setMonth(new Date(anchor.getFullYear(), anchor.getMonth(), 1))
  }

  const start = startOfWeek(month)
  const days = Array.from({ length: 42 }, (_, i) => addDays(start, i))
  const todayKey = localDateKey(new Date())
  const step = (n: number) => setMonth((m) => new Date(m.getFullYear(), m.getMonth() + n, 1))

  return (
    <div data-testid="mini-month">
      <div className="mb-1 flex items-center justify-between px-1">
        <span className="text-xs font-medium text-slate-300">{month.toLocaleDateString([], { month: 'long', year: 'numeric' })}</span>
        <span className="flex">
          <button type="button" onClick={() => step(-1)} className="rounded p-0.5 text-slate-500 hover:bg-slate-700/50 hover:text-slate-200" aria-label="Previous month">
            <ChevronLeft size={13} />
          </button>
          <button type="button" onClick={() => step(1)} className="rounded p-0.5 text-slate-500 hover:bg-slate-700/50 hover:text-slate-200" aria-label="Next month">
            <ChevronRight size={13} />
          </button>
        </span>
      </div>
      <div className="grid grid-cols-7 text-center">
        {days.slice(0, 7).map((d) => (
          <span key={d.getDay()} className="py-0.5 text-[10px] text-slate-500">
            {d.toLocaleDateString([], { weekday: 'narrow' })}
          </span>
        ))}
        {days.map((d) => {
          const key = localDateKey(d)
          const inView = d >= from && d < to
          const isToday = key === todayKey
          return (
            <button
              key={key}
              type="button"
              onClick={() => onPick(d)}
              className={`flex h-6 items-center justify-center text-[11px] ${inView ? 'bg-indigo-500/15' : ''}`}
              aria-label={d.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })}
              data-testid="mini-day"
              data-day={key}
            >
              <span className={`flex h-5 w-5 items-center justify-center rounded-full ${
                isToday ? 'bg-indigo-500 font-semibold text-white'
                  : d.getMonth() === month.getMonth() ? 'text-slate-300 hover:bg-slate-700/60' : 'text-slate-600 hover:bg-slate-700/60'
              }`}>
                {d.getDate()}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
