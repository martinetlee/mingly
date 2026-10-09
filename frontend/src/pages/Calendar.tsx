/**
 * Calendar (/calendar): the Google Calendar events of connected Gmail
 * accounts as an agenda (next two weeks, days with events) or a week (every
 * day, Monday first). Each event lists its time, title, location, the people
 * involved (links to their pages) and a link to it in Google Calendar.
 *
 * URL: ?date=YYYY-MM-DD anchors the view, &event=… highlights an event (links
 * from a person's timeline use pages/calendar/agenda calendarEventUrl).
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { AlertTriangle, CalendarDays, ChevronLeft, ChevronRight, Plug, RefreshCw } from 'lucide-react'
import { getCalendarAccounts, getCalendarEvents, localDateKey, syncCalendars } from '../api/calendar'
import { useAccounts } from '../hooks/useAccounts'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useWsEvent } from '../hooks/useWebSocket'
import { CONNECTIONS_PATH } from '../components/NoAccountsNotice'
import { errorMessage, reportError } from '../lib/toast'
import { dayLabel, groupByDay, parseDateKey, rangeLabel, shiftAnchor, startOfLocalDay, viewRange } from './calendar/agenda'
import type { CalendarView } from './calendar/agenda'
import EventRow from './calendar/EventRow'
import CalendarPicker from './calendar/CalendarPicker'

/** Empty state while no Gmail account is connected (same pattern as Mail's) */
export function NoCalendarAccountNotice() {
  return (
    <div className="m-auto max-w-sm rounded-xl border border-indigo-500/30 bg-indigo-500/5 p-5 text-center" data-testid="no-calendar-account">
      <Plug size={20} className="mx-auto mb-2 text-indigo-400" aria-hidden />
      <p className="text-sm font-medium text-slate-200">No calendar connected</p>
      <p className="mt-1 text-xs text-slate-400">Connect a Gmail account to see its Google Calendar here.</p>
      <Link
        to={CONNECTIONS_PATH}
        className="mt-4 inline-block rounded-lg bg-indigo-500 px-4 py-2 text-xs font-medium text-white transition-colors hover:bg-indigo-400"
      >
        Open Settings → Connections
      </Link>
    </div>
  )
}

const VIEWS: { id: CalendarView; label: string }[] = [
  { id: 'agenda', label: 'Agenda' },
  { id: 'week', label: 'Week' },
]

export default function Calendar() {
  const [params, setParams] = useSearchParams()
  const { accounts, loaded } = useAccounts()
  const gmailAccounts = useMemo(
    () => accounts.filter((a) => a.channel === 'gmail' && a.configured && a.account_id != null),
    [accounts],
  )
  const hasAccounts = gmailAccounts.length > 0
  const [accountChoice, setAccountChoice] = useState<number | null>(null)
  const accountId = accountChoice != null && gmailAccounts.some((a) => a.account_id === accountChoice) ? accountChoice : null
  const [view, setView] = useState<CalendarView>('agenda')

  const anchor = parseDateKey(params.get('date')) ?? startOfLocalDay(new Date())
  const anchorKey = localDateKey(anchor)
  const highlightId = params.get('event') ? Number(params.get('event')) : null
  const { from, to } = useMemo(() => viewRange(view, parseDateKey(anchorKey)!), [view, anchorKey])

  const events = useLatestQuery(
    ['calendar-events', from.getTime(), to.getTime(), accountId],
    (signal) => getCalendarEvents(from, to, { accountId, signal }),
    { enabled: hasAccounts, keepPreviousData: true },
  )
  const status = useLatestQuery(['calendar-accounts'], (signal) => getCalendarAccounts(signal), { enabled: hasAccounts })
  useWsEvent('calendar_synced', () => { void events.refetch(); void status.refetch() })
  useWsEvent('resync', () => { void events.refetch() })

  const days = useMemo(
    () => groupByDay(events.data?.events ?? [], from, to, view === 'week'),
    [events.data, from, to, view],
  )

  // Bring a linked event into view once it's rendered
  const highlightRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    highlightRef.current?.scrollIntoView?.({ block: 'center' })
  }, [highlightId, events.data])

  const goTo = (d: Date | null) => setParams(d ? { date: localDateKey(d) } : {})
  const [syncing, setSyncing] = useState(false)
  const syncNow = async () => {
    setSyncing(true)
    try {
      await syncCalendars()
    } catch (e) {
      reportError(e, 'Couldn’t start a calendar sync')
    } finally {
      setSyncing(false)
    }
  }

  if (loaded && !hasAccounts) {
    return <div className="flex flex-1 p-6"><NoCalendarAccountNotice /></div>
  }

  const problems = (status.data ?? []).filter((a) => a.error && (accountId == null || a.account_id === accountId))
  const navCls = (active: boolean) => `flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-sm transition-colors ${
    active ? 'bg-indigo-500/20 text-indigo-300' : 'text-slate-400 hover:bg-slate-700/50 hover:text-slate-200'
  }`

  return (
    <div className="flex flex-1 overflow-hidden" data-testid="calendar-page">
      <aside className="flex w-52 flex-shrink-0 flex-col gap-3 overflow-y-auto border-r border-slate-700/50 bg-slate-800 p-3">
        {gmailAccounts.length > 1 ? (
          <select
            value={accountId ?? ''}
            onChange={(e) => setAccountChoice(e.target.value ? Number(e.target.value) : null)}
            aria-label="Account"
            className="w-full rounded-lg border border-slate-600 bg-slate-700 px-2 py-1.5 text-xs text-slate-200 outline-none"
          >
            <option value="">All accounts</option>
            {gmailAccounts.map((a) => <option key={a.account_key} value={a.account_id!}>{a.label ?? a.account_key}</option>)}
          </select>
        ) : (
          <p className="flex items-center gap-1.5 truncate px-1 text-xs text-slate-400" title={gmailAccounts[0]?.label ?? ''}>
            <CalendarDays size={13} className="flex-shrink-0" /> {gmailAccounts[0]?.label ?? 'Calendar'}
          </p>
        )}
        <nav className="flex flex-col gap-0.5" aria-label="Views">
          {VIEWS.map((v) => (
            <button key={v.id} type="button" onClick={() => setView(v.id)} className={navCls(view === v.id)} aria-pressed={view === v.id}>
              {v.label}
            </button>
          ))}
        </nav>
        <div className="flex flex-col gap-2 border-t border-slate-700/50 pt-3">
          {gmailAccounts
            .filter((a) => accountId == null || a.account_id === accountId)
            .map((a) => (
              <CalendarPicker key={a.account_key} accountId={a.account_id!} label={gmailAccounts.length > 1 ? a.label ?? null : null} />
            ))}
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col overflow-hidden bg-slate-900">
        <header className="flex items-center gap-2 border-b border-slate-700/50 px-4 py-2.5">
          <button type="button" onClick={() => goTo(shiftAnchor(view, anchor, -1))} className="rounded p-1 text-slate-400 hover:bg-slate-700/50 hover:text-slate-200" aria-label="Previous">
            <ChevronLeft size={16} />
          </button>
          <button type="button" onClick={() => goTo(null)} className="rounded-lg border border-slate-600 px-2.5 py-1 text-xs text-slate-300 hover:bg-slate-700/50">
            Today
          </button>
          <button type="button" onClick={() => goTo(shiftAnchor(view, anchor, 1))} className="rounded p-1 text-slate-400 hover:bg-slate-700/50 hover:text-slate-200" aria-label="Next">
            <ChevronRight size={16} />
          </button>
          <h2 className="ml-1 flex-1 truncate text-sm font-medium text-slate-200" data-testid="calendar-range">{rangeLabel(from, to)}</h2>
          <button
            type="button"
            onClick={() => { void syncNow() }}
            disabled={syncing}
            className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-slate-400 hover:bg-slate-700/50 hover:text-slate-200 disabled:opacity-50"
            title="Sync calendars now"
          >
            <RefreshCw size={12} className={syncing ? 'animate-spin' : ''} /> Sync
          </button>
        </header>

        {problems.map((a) => (
          <p key={a.account_id} className="mx-4 mt-3 flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-200" data-testid="calendar-error">
            <AlertTriangle size={13} className="mt-0.5 flex-shrink-0" />
            <span>{gmailAccounts.length > 1 ? `${a.label}: ` : ''}{a.error}</span>
          </p>
        ))}

        <div className="flex-1 overflow-y-auto px-4 py-3">
          {events.loading || !loaded ? (
            <p className="py-2 text-xs text-slate-500">Loading…</p>
          ) : events.error && !events.data ? (
            <p className="py-2 text-xs text-red-400">{errorMessage(events.error)}</p>
          ) : days.length === 0 ? (
            <p className="py-8 text-center text-sm text-slate-500" data-testid="calendar-empty">No events in these two weeks</p>
          ) : (
            <div className="flex flex-col gap-4">
              {days.map((day) => (
                <section key={day.key} data-testid="calendar-day">
                  <h3 className="mb-1.5 text-xs font-medium uppercase tracking-wider text-slate-500">{dayLabel(day.date)}</h3>
                  {day.events.length === 0 ? (
                    <p className="px-1 text-xs text-slate-600">No events</p>
                  ) : (
                    <div className="flex flex-col gap-1.5">
                      {day.events.map((ev) => (
                        <EventRow
                          key={ev.id}
                          event={ev}
                          highlighted={ev.id === highlightId}
                          ref={ev.id === highlightId ? highlightRef : undefined}
                        />
                      ))}
                    </div>
                  )}
                </section>
              ))}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
