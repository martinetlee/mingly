/**
 * "Upcoming meetings" on a person's page: their next calendar events (not
 * declined), each opening its day in the Calendar space. Renders nothing
 * while there are none, so people you never meet don't get an empty box.
 */
import { Link } from 'react-router-dom'
import { CalendarDays } from 'lucide-react'
import { getUpcomingMeetings } from '../../api/calendar'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { useWsEvent } from '../../hooks/useWebSocket'
import { errorMessage } from '../../lib/toast'
import { calendarEventUrl, dayLabel, eventTimeLabel, parseDateKey, startOfLocalDay } from './agenda'
import { parseServerDate } from '../../utils/time'

export default function UpcomingMeetings({ personId }: { personId: number }) {
  const q = useLatestQuery(['upcoming-meetings', personId], (signal) => getUpcomingMeetings(personId, { signal }))
  useWsEvent('calendar_synced', () => { void q.refetch() })
  useWsEvent('resync', () => { void q.refetch() })

  const events = q.data?.events ?? []
  if (q.loading || (!q.error && events.length === 0)) return null

  return (
    <div className="mb-6 rounded-xl bg-slate-800 border border-slate-700/50 p-4" data-testid="upcoming-meetings">
      <h3 className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-500">Upcoming meetings</h3>
      {q.error && !q.data ? (
        <p className="py-2 text-xs text-red-400">{errorMessage(q.error)}</p>
      ) : (
        <div className="flex flex-col">
          {events.map((ev) => {
            const day = ev.all_day ? parseDateKey(ev.start_at) : parseServerDate(ev.start_at)
            const others = ev.attendees.filter((a) => !a.is_self && a.person_id !== personId)
            return (
              <Link
                key={ev.id}
                to={calendarEventUrl(ev)}
                className="flex items-center gap-2.5 rounded-lg px-2 py-1.5 text-sm hover:bg-slate-700/50 transition-colors"
                data-testid="upcoming-meeting"
              >
                <CalendarDays size={12} className="flex-shrink-0 text-slate-500" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-xs font-medium text-slate-300">{ev.title || '(no title)'}</p>
                  {(ev.location || others.length > 0) && (
                    <p className="truncate text-xs text-slate-500">
                      {[others.length > 0 ? `with ${others.map((a) => a.name).join(', ')}` : null, ev.location].filter(Boolean).join(' · ')}
                    </p>
                  )}
                </div>
                <span className="flex-shrink-0 text-right text-[11px] text-slate-500">
                  {day ? dayLabel(startOfLocalDay(day)) : ''} · {eventTimeLabel(ev)}
                </span>
              </Link>
            )
          })}
        </div>
      )}
    </div>
  )
}
