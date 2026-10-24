/**
 * Agenda view: the days with events, each event as a row (time, colour,
 * title, recurrence, Join, your reply, location, guests). A multi-day event
 * shows on each of its days with "Day 2 of 3". Clicking a row opens it.
 */
import { useEffect, useMemo, useRef } from 'react'
import { MapPin, Repeat, Video } from 'lucide-react'
import type { CalendarEvent } from '../../api/calendar'
import { dayLabel, groupByDay } from '../../features/calendar/agenda'
import { dayTimeLabel, eventDayOf } from '../../features/calendar/layout'
import { AttendeeLink, ColorDot, eventState, RESPONSE_LABEL, SELECTED_RING } from './EventChip'

const MAX_GUESTS = 4

interface Props {
  from: Date
  to: Date
  events: CalendarEvent[]
  colorOf: (ev: CalendarEvent) => string
  selectedId: number | null
  onSelect: (id: number) => void
  emptyText: string
}

export default function AgendaList({ from, to, events, colorOf, selectedId, onSelect, emptyText }: Props) {
  const days = useMemo(() => groupByDay(events, from, to, false), [events, from, to])
  // Bring the selected event into view once it's rendered
  const selectedRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    selectedRef.current?.scrollIntoView?.({ block: 'center' })
  }, [selectedId, events])

  if (days.length === 0) {
    return <p className="py-8 text-center text-sm text-slate-500" data-testid="calendar-empty">{emptyText}</p>
  }
  return (
    <div className="flex flex-col gap-4 px-4 py-3" data-testid="agenda">
      {days.map((day) => (
        <section key={day.key} data-testid="calendar-day">
          <h3 className="mb-1.5 text-xs font-medium uppercase tracking-wider text-slate-500">{dayLabel(day.date)}</h3>
          <div className="flex flex-col gap-1.5">
            {day.events.map((ev) => (
              <AgendaRow
                key={ev.id}
                event={ev}
                day={day.date}
                color={colorOf(ev)}
                selected={ev.id === selectedId}
                rowRef={ev.id === selectedId ? selectedRef : undefined}
                onSelect={() => onSelect(ev.id)}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}

function AgendaRow({ event: ev, day, color, selected, rowRef, onSelect }: {
  event: CalendarEvent
  day: Date
  color: string
  selected: boolean
  rowRef?: React.Ref<HTMLDivElement>
  onSelect: () => void
}) {
  // Just you (an event without guests): nothing to list
  const people = ev.attendees.length === 1 && ev.attendees[0].is_self ? [] : ev.attendees
  const part = eventDayOf(ev, day)
  const state = eventState(ev)
  return (
    <div
      ref={rowRef}
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => { if (e.key === 'Enter') onSelect() }}
      className={`flex cursor-pointer gap-3 rounded-lg border px-3 py-2 transition-colors hover:bg-slate-700/40 ${
        selected ? `border-indigo-500/60 bg-indigo-500/10 ${SELECTED_RING}` : 'border-slate-700/50 bg-slate-800'
      } ${state === 'declined' ? 'opacity-60' : ''}`}
      data-testid="calendar-event"
      data-event-id={ev.id}
    >
      <div className="w-28 flex-shrink-0 pt-0.5 text-xs tabular-nums text-slate-400">
        {dayTimeLabel(ev, day)}
        {part && <span className="block text-[10px] text-slate-500">Day {part.n} of {part.total}</span>}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <ColorDot color={color} />
          <p className={`min-w-0 flex-1 truncate text-sm font-medium ${
            state === 'declined' ? 'text-slate-400 line-through' : state === 'unanswered' ? 'italic text-slate-300' : 'text-slate-100'
          }`}>
            {ev.title || '(no title)'}
          </p>
          {ev.recurring && <Repeat size={12} className="flex-shrink-0 text-slate-500" aria-label="Recurring" />}
          {ev.my_response && ev.my_response !== 'accepted' && (
            <span className="flex-shrink-0 rounded bg-slate-700/60 px-1.5 py-0.5 text-[10px] text-slate-300" data-testid="my-response">
              {RESPONSE_LABEL[ev.my_response]}
            </span>
          )}
          {ev.conference_url && (
            <a
              href={ev.conference_url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="inline-flex flex-shrink-0 items-center gap-1 rounded-md bg-indigo-500/20 px-2 py-0.5 text-[11px] text-indigo-200 hover:bg-indigo-500/30"
            >
              <Video size={11} /> Join
            </a>
          )}
        </div>
        {ev.location && (
          <p className="mt-0.5 flex items-center gap-1 truncate text-xs text-slate-400">
            <MapPin size={11} className="flex-shrink-0" /> {ev.location}
          </p>
        )}
        {people.length > 0 && (
          <div className="mt-1.5 flex flex-wrap items-center gap-1">
            {people.slice(0, MAX_GUESTS).map((a) => <AttendeeLink key={a.identity_id} attendee={a} />)}
            {people.length > MAX_GUESTS && <span className="text-[11px] text-slate-500">+{people.length - MAX_GUESTS} more</span>}
          </div>
        )}
      </div>
    </div>
  )
}
