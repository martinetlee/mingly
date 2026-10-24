/**
 * One visual vocabulary for events in every Calendar view: the event's
 * colour (its own, else its calendar's), and its state —
 *   - declined by you: dimmed and struck through
 *   - not answered / maybe (or tentative): outlined, lighter fill
 *   - "free" (transparent): lighter fill
 *   - selected: a ring
 */
import type { CSSProperties } from 'react'
import { Link } from 'react-router-dom'
import type { CalendarAttendee, CalendarEvent } from '../../api/calendar'
import { personUrl } from '../../lib/routes'

/** "#abc" / "#aabbcc" → "#aabbcc" (anything else: null) */
function hex6(color: string): string | null {
  const c = color.trim()
  if (/^#[0-9a-f]{6}$/i.test(c)) return c
  const m = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(c)
  return m ? `#${m[1]}${m[1]}${m[2]}${m[2]}${m[3]}${m[3]}` : null
}

/** `color` at `alpha` (0–1) as #rrggbbaa */
export function withAlpha(color: string, alpha: number): string {
  const c = hex6(color) ?? '#6366f1'
  return c + Math.round(alpha * 255).toString(16).padStart(2, '0')
}

export type EventState = 'declined' | 'unanswered' | 'free' | 'normal'

export function eventState(ev: CalendarEvent): EventState {
  if (ev.my_response === 'declined') return 'declined'
  if (ev.my_response === 'needsAction' || ev.my_response === 'tentative' || ev.status === 'tentative') return 'unanswered'
  if (ev.transparency === 'transparent') return 'free'
  return 'normal'
}

/** Fill, border and text style of an event drawn as a block or bar */
export function chipStyle(ev: CalendarEvent, color: string): { style: CSSProperties; className: string } {
  const state = eventState(ev)
  const base = hex6(color) ?? '#6366f1'
  switch (state) {
    case 'declined':
      return {
        style: { background: 'transparent', border: `1px solid ${withAlpha(base, 0.35)}` },
        className: 'text-slate-400 line-through opacity-60',
      }
    case 'unanswered':
      return {
        style: { background: withAlpha(base, 0.12), border: `1px dashed ${base}` },
        className: 'text-slate-100',
      }
    case 'free':
      return {
        style: { background: withAlpha(base, 0.15), borderLeft: `3px solid ${withAlpha(base, 0.6)}` },
        className: 'text-slate-200',
      }
    default:
      return {
        style: { background: withAlpha(base, 0.32), borderLeft: `3px solid ${base}` },
        className: 'text-slate-50',
      }
  }
}

export const SELECTED_RING = 'ring-2 ring-indigo-300/80 ring-offset-1 ring-offset-slate-900'

export const RESPONSE_LABEL: Record<string, string> = {
  accepted: 'Going', declined: 'Declined', tentative: 'Maybe', needsAction: 'Not answered',
}

/** A colour dot for lists */
export function ColorDot({ color, className = '' }: { color: string; className?: string }) {
  return <span className={`inline-block h-2 w-2 flex-shrink-0 rounded-full ${className}`} style={{ background: color }} aria-hidden />
}

/** A guest as a link to their person page (you as "You"; declined struck through) */
export function AttendeeLink({ attendee }: { attendee: CalendarAttendee }) {
  const declined = attendee.response_status === 'declined'
  const response = attendee.response_status ? RESPONSE_LABEL[attendee.response_status] ?? attendee.response_status : null
  const title = [attendee.email, attendee.is_organizer ? 'organizer' : null, response].filter(Boolean).join(' · ')
  return (
    <Link
      to={personUrl(attendee.person_id)}
      title={title}
      onClick={(e) => e.stopPropagation()}
      className={`rounded px-1.5 py-0.5 text-[11px] transition-colors hover:bg-slate-600/60 ${
        declined ? 'text-slate-500 line-through' : 'text-indigo-300'
      } ${attendee.is_organizer ? 'bg-slate-700/70' : 'bg-slate-700/40'}`}
      data-testid="event-attendee"
    >
      {attendee.is_self ? 'You' : attendee.name}
    </Link>
  )
}
