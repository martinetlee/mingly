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
import { useSearchParams } from 'react-router-dom'
import { AlertTriangle, CalendarDays, ChevronLeft, ChevronRight, Loader2, RefreshCw } from 'lucide-react'
import { getCalendarAccounts, getCalendarEvents, localDateKey, syncCalendars } from '../api/calendar'
import { useAccounts } from '../hooks/useAccounts'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useWsEvent } from '../hooks/useWebSocket'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import AccountGate from '../components/AccountGate'
import { LoadError } from '../components/ui'
import { errorMessage, reportError } from '../lib/toast'
import { dayLabel, groupByDay, parseDateKey, rangeLabel, shiftAnchor, startOfLocalDay, viewRange } from '../features/calendar/agenda'
import type { CalendarView } from '../features/calendar/agenda'
import EventRow from './calendar/EventRow'
import CalendarPicker from './calendar/CalendarPicker'

const VIEWS: { id: CalendarView; label: string }[] = [
  { id: 'agenda', label: 'Agenda' },
  { id: 'week', label: 'Week' },
]

export default function Calendar() {
  const [params, setParams] = useSearchParams()
  const accountsState = useAccounts()
  const { accounts, loaded } = accountsState
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
    // Tagged with its range: while the next range loads, the previous one stays
    // on screen laid out as it was
    (signal) => getCalendarEvents(from, to, { accountId, signal }).then((r) => ({ ...r, from, to, view })),
    { enabled: hasAccounts, keepPreviousData: true },
  )
  const status = useLatestQuery(['calendar-accounts'], (signal) => getCalendarAccounts(signal), { enabled: hasAccounts })
  useWsEvent('calendar_synced', () => { void events.refetch(); void status.refetch() })
  useWsEvent('resync', () => { void events.refetch() })

  const days = useMemo(() => {
    const d = events.data
    return d ? groupByDay(d.events, d.from, d.to, d.view === 'week') : []
  }, [events.data])

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

  // t: today · ←/→: previous / next range · w / a: week / agenda view
  // (the page never scrolls sideways, so ←/→ needn't yield to scrolling)
  useKeyboardShortcuts({
    t: () => goTo(null),
    arrowleft: () => goTo(shiftAnchor(view, anchor, -1)),
    arrowright: () => goTo(shiftAnchor(view, anchor, 1)),
    w: () => setView('week'),
    a: () => setView('agenda'),
  }, { enabled: hasAccounts, yieldScrollKeys: false })

  if (!hasAccounts) {
    return <AccountGate state={accountsState} channels={['gmail']} title="No calendar connected" body="Connect a Gmail account to see its Google Calendar here." />
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
          {/* The previous range stays on screen while the new one loads */}
          {events.isStale && !events.error && <Loader2 size={13} className="animate-spin text-slate-500" aria-label="Loading" data-testid="calendar-stale" />}
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
          {!loaded || (events.data == null && events.loading) ? (
            <p className="py-2 text-xs text-slate-500">Loading…</p>
          ) : events.error && (!events.data || events.isStale) ? (
            <LoadError message={`Couldn’t load events: ${errorMessage(events.error)}`} onRetry={() => { void events.refetch() }} />
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
