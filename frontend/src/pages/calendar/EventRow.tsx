/**
 * One calendar event in the Calendar space: time, title, location, the
 * people involved (links to their person pages; you and declined guests are
 * marked) and a link to the event in Google Calendar.
 */
import { forwardRef } from 'react'
import { Link } from 'react-router-dom'
import { ExternalLink, MapPin } from 'lucide-react'
import type { CalendarAttendee, CalendarEvent } from '../../api/calendar'
import { personUrl } from '../../lib/routes'
import { eventTimeLabel } from '../../features/calendar/agenda'

const RESPONSE_LABEL: Record<string, string> = {
  accepted: 'accepted', declined: 'declined', tentative: 'maybe', needsAction: 'no reply',
}

export function AttendeeLink({ attendee }: { attendee: CalendarAttendee }) {
  const declined = attendee.response_status === 'declined'
  const response = attendee.response_status ? RESPONSE_LABEL[attendee.response_status] ?? attendee.response_status : null
  const title = [attendee.email, attendee.is_organizer ? 'organizer' : null, response].filter(Boolean).join(' · ')
  return (
    <Link
      to={personUrl(attendee.person_id)}
      title={title}
      className={`rounded px-1.5 py-0.5 text-[11px] transition-colors hover:bg-slate-600/60 ${
        declined ? 'text-slate-500 line-through' : 'text-indigo-300'
      } ${attendee.is_organizer ? 'bg-slate-700/70' : 'bg-slate-700/40'}`}
      data-testid="event-attendee"
    >
      {attendee.is_self ? 'You' : attendee.name}
    </Link>
  )
}

interface Props {
  event: CalendarEvent
  highlighted?: boolean
}

const EventRow = forwardRef<HTMLDivElement, Props>(function EventRow({ event, highlighted = false }, ref) {
  // Just you (an event without guests): nothing to list
  const people = event.attendees.length === 1 && event.attendees[0].is_self ? [] : event.attendees
  return (
    <div
      ref={ref}
      className={`flex gap-3 rounded-lg border px-3 py-2 transition-colors ${
        highlighted ? 'border-indigo-500/60 bg-indigo-500/10' : 'border-slate-700/50 bg-slate-800'
      }`}
      data-testid="calendar-event"
    >
      <div className="w-28 flex-shrink-0 pt-0.5 text-xs tabular-nums text-slate-400">{eventTimeLabel(event)}</div>
      <div className="min-w-0 flex-1">
        <div className="flex items-start gap-2">
          <p className={`min-w-0 flex-1 text-sm font-medium ${event.status === 'tentative' ? 'italic text-slate-300' : 'text-slate-100'}`}>
            {event.title || '(no title)'}
          </p>
          {event.html_link && (
            <a
              href={event.html_link}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex flex-shrink-0 items-center gap-1 text-[11px] text-slate-500 hover:text-slate-300"
              title="Open in Google Calendar"
            >
              <ExternalLink size={11} /> Google Calendar
            </a>
          )}
        </div>
        {event.location && (
          <p className="mt-0.5 flex items-center gap-1 truncate text-xs text-slate-400">
            <MapPin size={11} className="flex-shrink-0" /> {event.location}
          </p>
        )}
        {people.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1">
            {people.map((a) => <AttendeeLink key={a.identity_id} attendee={a} />)}
          </div>
        )}
      </div>
    </div>
  )
})

export default EventRow
