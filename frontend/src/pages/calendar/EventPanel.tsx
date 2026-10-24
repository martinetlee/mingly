/**
 * The selected event, in a panel beside the calendar: when, the calendar,
 * Join (video call), where, your reply, the organizer and guests grouped by
 * their reply (each a link to the person), the description, your recent
 * conversations with the guests (to prepare), and a link to Google Calendar.
 * Read-only: replies are made in Google Calendar.
 */
import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { CalendarDays, Clock, ExternalLink, MapPin, MessagesSquare, Repeat, Video, X } from 'lucide-react'
import type { CalendarAttendee, CalendarEvent } from '../../api/calendar'
import { getPersonConversations } from '../../api/client'
import type { Conversation } from '../../api/types'
import ChannelIcon from '../../components/channels/ChannelIcon'
import { LinkifiedText } from '../../components/ui'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { addDays, parseDateKey } from '../../features/calendar/agenda'
import { formatRecurrence } from '../../features/calendar/layout'
import { conversationUrl, personUrl } from '../../lib/routes'
import { fmtRelative, fmtTime, parseServerDate } from '../../utils/time'
import { ColorDot, RESPONSE_LABEL } from './EventChip'
import RichDescription from './RichDescription'

/** Guests whose conversations are looked up */
const MAX_PEOPLE = 4
const MAX_CONVERSATIONS = 5

const fmtDay = (d: Date) => d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' })

/** "Tue, Oct 27, 2026 · 09:00 – 10:00", the all-day and multi-day forms */
export function eventWhen(ev: CalendarEvent): string {
  if (ev.all_day) {
    const start = parseDateKey(ev.start_at)
    const end = parseDateKey(ev.end_at)
    if (!start) return ''
    const last = end && end > start ? addDays(end, -1) : start
    return last.getTime() === start.getTime() ? `${fmtDay(start)} · All day` : `${fmtDay(start)} – ${fmtDay(last)}`
  }
  const start = parseServerDate(ev.start_at)
  const end = parseServerDate(ev.end_at)
  if (!start) return ''
  if (!end || end <= start) return `${fmtDay(start)} · ${fmtTime(start)}`
  const sameDay = start.toDateString() === new Date(end.getTime() - 1).toDateString()
  return sameDay
    ? `${fmtDay(start)} · ${fmtTime(start)} – ${fmtTime(end)}`
    : `${fmtDay(start)}, ${fmtTime(start)} – ${fmtDay(end)}, ${fmtTime(end)}`
}

const GROUPS: { id: string; label: string; match: (a: CalendarAttendee) => boolean }[] = [
  { id: 'going', label: 'Going', match: (a) => a.response_status === 'accepted' || (a.response_status == null && a.is_organizer) },
  { id: 'maybe', label: 'Maybe', match: (a) => a.response_status === 'tentative' },
  { id: 'declined', label: 'Declined', match: (a) => a.response_status === 'declined' },
  { id: 'none', label: 'No reply', match: (a) => a.response_status === 'needsAction' || (a.response_status == null && !a.is_organizer) },
]

const isUrl = (s: string) => /^https?:\/\/\S+$/i.test(s.trim())

interface Props {
  event: CalendarEvent
  color: string
  calendarName: string | null
  accountLabel: string | null
  onClose: () => void
}

export default function EventPanel({ event: ev, color, calendarName, accountLabel, onClose }: Props) {
  const guests = ev.attendees.filter((a) => !a.is_self)
  const groups = GROUPS.map((g) => ({ ...g, people: ev.attendees.filter(g.match) })).filter((g) => g.people.length > 0)
  const showGuests = guests.length > 0

  return (
    <aside
      className="flex w-96 flex-shrink-0 flex-col overflow-hidden border-l border-slate-700/50 bg-slate-800"
      aria-label="Event details"
      data-testid="event-panel"
    >
      <header className="flex items-start gap-2 border-b border-slate-700/50 px-4 py-3">
        <span className="mt-1.5 h-3 w-3 flex-shrink-0 rounded-sm" style={{ background: color }} aria-hidden />
        <h2 className={`min-w-0 flex-1 text-base font-semibold [overflow-wrap:anywhere] ${
          ev.my_response === 'declined' ? 'text-slate-400 line-through' : 'text-slate-100'
        }`}>
          {ev.title || '(no title)'}
        </h2>
        <button type="button" onClick={onClose} className="rounded p-1 text-slate-400 hover:bg-slate-700/60 hover:text-slate-200" aria-label="Close details">
          <X size={16} />
        </button>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-3 text-sm">
        <div className="flex flex-col gap-2 text-xs text-slate-300">
          <p className="flex items-start gap-2"><Clock size={13} className="mt-0.5 flex-shrink-0 text-slate-500" /> <span>{eventWhen(ev)}</span></p>
          {ev.recurring && (
            <p className="flex items-start gap-2" data-testid="event-recurrence">
              <Repeat size={13} className="mt-0.5 flex-shrink-0 text-slate-500" /> <span>{formatRecurrence(ev.recurrence)}</span>
            </p>
          )}
          {(calendarName || accountLabel) && (
            <p className="flex items-center gap-2">
              <CalendarDays size={13} className="flex-shrink-0 text-slate-500" />
              <ColorDot color={color} />
              <span className="truncate">{[calendarName, accountLabel].filter(Boolean).join(' · ')}</span>
            </p>
          )}
          {ev.location && (
            <p className="flex items-start gap-2">
              <MapPin size={13} className="mt-0.5 flex-shrink-0 text-slate-500" />
              <span className="min-w-0 [overflow-wrap:anywhere]">
                <LinkifiedText text={ev.location} linkClassName="text-indigo-300 underline underline-offset-2 hover:text-indigo-200" />
                {!isUrl(ev.location) && (
                  <a
                    href={`https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(ev.location)}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="ml-1.5 whitespace-nowrap text-indigo-300 hover:text-indigo-200"
                  >
                    Open in Maps
                  </a>
                )}
              </span>
            </p>
          )}
        </div>

        {ev.conference_url && (
          <a
            href={ev.conference_url}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-3 flex w-full items-center justify-center gap-2 rounded-lg bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-500"
            data-testid="event-join"
          >
            <Video size={15} /> Join {ev.conference_name || 'video call'}
          </a>
        )}

        {ev.my_response && (
          <div className="mt-3 flex items-center justify-between gap-2 rounded-lg bg-slate-900/50 px-3 py-2 text-xs" data-testid="event-my-response">
            <span className="text-slate-300">You: <span className="font-medium text-slate-100">{RESPONSE_LABEL[ev.my_response] ?? ev.my_response}</span></span>
            {ev.html_link && (
              <a href={ev.html_link} target="_blank" rel="noopener noreferrer" className="text-indigo-300 hover:text-indigo-200">
                Reply in Google Calendar
              </a>
            )}
          </div>
        )}

        {showGuests && (
          <section className="mt-4" data-testid="event-guests">
            <h3 className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-slate-500">
              {ev.attendees.length} {ev.attendees.length === 1 ? 'guest' : 'guests'}
            </h3>
            {ev.organizer && (
              <p className="mb-2 text-xs text-slate-400">
                Organized by{' '}
                <Link to={personUrl(ev.organizer.person_id)} className="text-indigo-300 hover:underline">
                  {ev.organizer.is_self ? 'you' : ev.organizer.name}
                </Link>
              </p>
            )}
            <div className="flex flex-col gap-2">
              {groups.map((g) => (
                <div key={g.id} data-testid={`guests-${g.id}`}>
                  <p className="mb-0.5 text-[11px] text-slate-500">{g.label} · {g.people.length}</p>
                  <ul className="flex flex-col">
                    {g.people.map((a) => (
                      <li key={a.identity_id}>
                        <Link
                          to={personUrl(a.person_id)}
                          className={`flex items-baseline gap-2 rounded px-1 py-0.5 text-xs hover:bg-slate-700/50 ${
                            g.id === 'declined' ? 'text-slate-500 line-through' : 'text-slate-200'
                          }`}
                          title={a.email}
                        >
                          <span className="truncate">{a.is_self ? 'You' : a.name}</span>
                          {a.is_organizer && <span className="text-[10px] text-slate-500">organizer</span>}
                          {a.name !== a.email && !a.is_self && <span className="truncate text-[10px] text-slate-500">{a.email}</span>}
                        </Link>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </section>
        )}

        {ev.description && (
          <section className="mt-4">
            <h3 className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-slate-500">Notes</h3>
            <RichDescription text={ev.description} />
          </section>
        )}

        {showGuests && <RecentConversations guests={guests} />}

        {ev.html_link && (
          <a
            href={ev.html_link}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-4 inline-flex items-center gap-1 text-xs text-slate-400 hover:text-slate-200"
            title="Open in Google Calendar"
          >
            <ExternalLink size={12} /> Open in Google Calendar
          </a>
        )}
      </div>
    </aside>
  )
}

/** The latest direct conversations with the first guests, newest first */
function RecentConversations({ guests }: { guests: CalendarAttendee[] }) {
  const personIds = useMemo(
    () => [...new Set(guests.map((g) => g.person_id))].slice(0, MAX_PEOPLE),
    [guests],
  )
  const query = useLatestQuery(
    ['event-conversations', personIds.join(',')],
    async (signal) => {
      const lists = await Promise.all(personIds.map((id) => getPersonConversations(id, signal).catch(() => [] as Conversation[])))
      const byId = new Map<number, Conversation>()
      for (const c of lists.flat()) byId.set(c.id, c)
      const at = (c: Conversation) => parseServerDate(c.last_activity_at ?? c.last_message?.date)?.getTime() ?? 0
      return [...byId.values()].sort((a, b) => at(b) - at(a)).slice(0, MAX_CONVERSATIONS)
    },
    { enabled: personIds.length > 0 },
  )
  const conversations = query.data ?? []
  if (conversations.length === 0) return null
  return (
    <section className="mt-4" data-testid="event-conversations">
      <h3 className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-slate-500">
        <MessagesSquare size={12} /> Recent conversations
      </h3>
      <ul className="flex flex-col">
        {conversations.map((c) => (
          <li key={c.id}>
            <Link to={conversationUrl(c)} className="flex items-center gap-2 rounded px-1 py-1 text-xs text-slate-200 hover:bg-slate-700/50">
              <ChannelIcon channel={c.channel} size={12} className="flex-shrink-0 text-slate-400" />
              <span className="min-w-0 flex-1 truncate">{c.title || 'Conversation'}</span>
              <span className="flex-shrink-0 text-[10px] text-slate-500">{fmtRelative(c.last_activity_at ?? c.last_message?.date)}</span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  )
}
