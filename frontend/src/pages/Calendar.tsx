/**
 * Calendar (/calendar): the Google Calendar events of connected Gmail
 * accounts, as Google-style views — Day and Week (time grid), Month, and
 * Agenda (a list) — with the selected event's details in a side panel.
 *
 * URL: ?view=day|week|month|agenda (default: the last view used, else week),
 * &date=YYYY-MM-DD anchors the view, &event=<id> selects an event (links
 * from a person's page use features/calendar/agenda calendarEventUrl).
 *
 * Sidebar: a small month to jump to any day, the account filter, the synced
 * calendars as a show / hide filter (choosing what is synced is a dialog),
 * and when the calendars last synced and which dates the sync covers.
 */
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { CalendarDays, ChevronLeft, ChevronRight, Loader2, RefreshCw, Settings2 } from 'lucide-react'
import { getCalendarAccounts, getCalendarEvents, localDateKey, syncCalendars } from '../api/calendar'
import type { CalendarEvent } from '../api/calendar'
import { useAccounts } from '../hooks/useAccounts'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useWsEvent } from '../hooks/useWebSocket'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import AccountGate from '../components/channels/AccountGate'
import { LinkifiedText, LoadError, Modal, StatusBanner } from '../components/ui'
import { errorMessage, reportError } from '../lib/toast'
import { fmtRelative } from '../utils/time'
import {
  addDays, CALENDAR_VIEWS, parseDateKey, parseView, rangeLabel, shiftAnchor, startOfLocalDay, viewRange, viewTitle,
} from '../features/calendar/agenda'
import type { CalendarView } from '../features/calendar/agenda'
import {
  calendarIndex, calendarKey, eventCalendarKey, eventColor, lastView, rememberView, useHiddenCalendars,
} from '../features/calendar/visibility'
import AgendaList from './calendar/AgendaList'
import CalendarPicker from './calendar/CalendarPicker'
import EventPanel from './calendar/EventPanel'
import MiniMonth from './calendar/MiniMonth'
import MonthGrid from './calendar/MonthGrid'
import TimeGrid, { gridDays } from './calendar/TimeGrid'
import { ColorDot } from './calendar/EventChip'

const VIEW_LABEL: Record<CalendarView, string> = { day: 'Day', week: 'Week', month: 'Month', agenda: 'Agenda' }

const fmtWindowDay = (key: string) => parseDateKey(key)?.toLocaleDateString([], { month: 'short', day: 'numeric' }) ?? key

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

  const view: CalendarView = parseView(params.get('view')) ?? lastView() ?? 'week'
  const anchor = parseDateKey(params.get('date')) ?? startOfLocalDay(new Date())
  const anchorKey = localDateKey(anchor)
  const selectedId = params.get('event') ? Number(params.get('event')) : null
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

  const scopeAccounts = useMemo(
    () => (status.data ?? []).filter((a) => accountId == null || a.account_id === accountId),
    [status.data, accountId],
  )
  const calendars = useMemo(() => calendarIndex(status.data ?? []), [status.data])
  const { hidden, toggle: toggleCalendar } = useHiddenCalendars()
  const shown = useMemo(
    () => (events.data?.events ?? []).filter((ev) => !hidden.has(eventCalendarKey(ev, calendars))),
    [events.data, hidden, calendars],
  )
  const colorOf = useMemo(() => (ev: CalendarEvent) => eventColor(ev, calendars), [calendars])

  // The selected event, kept while the view moves to a range without it
  const [selected, setSelected] = useState<CalendarEvent | null>(null)
  useEffect(() => {
    if (selectedId == null) {
      setSelected(null)
      return
    }
    const found = events.data?.events.find((e) => e.id === selectedId)
    if (found) setSelected(found)
  }, [selectedId, events.data])

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params)
    for (const [k, v] of Object.entries(changes)) {
      if (v == null) next.delete(k)
      else next.set(k, v)
    }
    setParams(next)
  }
  const goTo = (d: Date | null) => update({ date: d ? localDateKey(d) : null })
  const setView = (v: CalendarView) => {
    rememberView(v)
    update({ view: v })
  }
  const openDay = (d: Date) => {
    rememberView('day')
    update({ view: 'day', date: localDateKey(d) })
  }
  const select = (id: number) => update({ event: String(id) })
  const closePanel = () => update({ event: null })

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
  const [choosing, setChoosing] = useState(false)

  // t: today · ←/→: previous / next · d / w / m / a: views · Esc: close the details
  // (the page never scrolls sideways, so ←/→ needn't yield to scrolling)
  useKeyboardShortcuts({
    t: () => goTo(null),
    arrowleft: () => goTo(shiftAnchor(view, anchor, -1)),
    arrowright: () => goTo(shiftAnchor(view, anchor, 1)),
    d: () => setView('day'),
    w: () => setView('week'),
    m: () => setView('month'),
    a: () => setView('agenda'),
    escape: () => (selectedId != null ? closePanel() : false),
  }, { enabled: hasAccounts, yieldScrollKeys: false })

  if (!hasAccounts) {
    return <AccountGate state={accountsState} channels={['gmail']} title="No calendar connected" body="Connect a Gmail account to see its Google Calendar here." />
  }

  const multi = gmailAccounts.length > 1
  const problems = scopeAccounts.filter((a) => a.error)
  // The dates every account in scope syncs; outside them only events synced earlier show
  const windows = scopeAccounts.map((a) => a.window).filter((w): w is { from: string; to: string } => !!w)
  const covered = windows.length > 0 ? {
    from: windows.map((w) => w.from).sort().reverse()[0],
    to: windows.map((w) => w.to).sort()[0],
  } : null
  const outsideWindow = covered != null && (localDateKey(from) < covered.from || localDateKey(to) > covered.to)
  const lastSync = scopeAccounts.map((a) => a.last_sync_at).filter((s): s is string => !!s).sort()[0] ?? null
  const accountLabel = (id: number) => (multi ? gmailAccounts.find((a) => a.account_id === id)?.label ?? null : null)
  const loading = !loaded || (events.data == null && events.loading)
  const failed = events.error != null && (!events.data || events.isStale)

  return (
    <div className="flex flex-1 overflow-hidden" data-testid="calendar-page">
      <aside className="flex w-56 flex-shrink-0 flex-col gap-4 overflow-y-auto border-r border-slate-700/50 bg-slate-800 p-3">
        <MiniMonth anchor={anchor} from={from} to={to} onPick={(d) => goTo(d)} />
        {multi ? (
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
        <div className="flex flex-col gap-1 border-t border-slate-700/50 pt-3" data-testid="calendar-list">
          <p className="px-1 text-[11px] font-medium uppercase tracking-wider text-slate-500">My calendars</p>
          {scopeAccounts.flatMap((a) => (a.calendars ?? []).map((c) => {
            const key = calendarKey(a.account_id, c.id)
            return (
              <label key={key} className="flex items-center gap-2 rounded px-1 py-0.5 text-xs text-slate-300 hover:bg-slate-700/40" title={multi ? a.label : undefined}>
                <input type="checkbox" checked={!hidden.has(key)} onChange={() => toggleCalendar(key)} className="accent-indigo-500" />
                <ColorDot color={c.color ?? '#6366f1'} />
                <span className="truncate">{c.name}</span>
              </label>
            )
          }))}
          <button
            type="button"
            onClick={() => setChoosing(true)}
            className="mt-1 flex items-center gap-1.5 px-1 text-left text-[11px] text-slate-400 hover:text-slate-200"
          >
            <Settings2 size={12} /> Choose synced calendars…
          </button>
        </div>
        <p className="mt-auto px-1 text-[11px] leading-snug text-slate-500" data-testid="calendar-sync-info">
          {lastSync ? `Synced ${fmtRelative(lastSync).toLowerCase()}` : 'Not synced yet'}
          {covered && <> · covers {fmtWindowDay(covered.from)} – {fmtWindowDay(localDateKey(addDays(parseDateKey(covered.to) ?? new Date(), -1)))}</>}
        </p>
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
          <h2 className="ml-1 flex-1 truncate text-sm font-medium text-slate-200" data-testid="calendar-range">{viewTitle(view, anchor, from, to)}</h2>
          {/* The previous range stays on screen while the new one loads */}
          {events.isStale && !events.error && <Loader2 size={13} className="animate-spin text-slate-500" aria-label="Loading" data-testid="calendar-stale" />}
          <nav className="flex rounded-lg border border-slate-600 p-0.5" aria-label="Views">
            {CALENDAR_VIEWS.map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setView(v)}
                aria-pressed={view === v}
                className={`rounded-md px-2.5 py-0.5 text-xs transition-colors ${
                  view === v ? 'bg-indigo-500/25 text-indigo-200' : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {VIEW_LABEL[v]}
              </button>
            ))}
          </nav>
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
          <StatusBanner key={a.account_id} className="mx-4 mt-3" testId="calendar-error">
            {multi ? `${a.label}: ` : ''}<LinkifiedText text={a.error ?? ''} />
          </StatusBanner>
        ))}
        {outsideWindow && covered && (
          <p className="mx-4 mt-2 text-[11px] text-slate-500" data-testid="calendar-window-note">
            Mingly syncs {fmtWindowDay(covered.from)} – {fmtWindowDay(localDateKey(addDays(parseDateKey(covered.to) ?? new Date(), -1)))}; outside those dates only events synced earlier show.
          </p>
        )}

        <div className="flex min-h-0 flex-1">
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
            {loading ? (
              <p className="px-4 py-3 text-xs text-slate-500">Loading…</p>
            ) : failed ? (
              <div className="px-4 py-3">
                <LoadError message={`Couldn’t load events: ${errorMessage(events.error)}`} onRetry={() => { void events.refetch() }} />
              </div>
            ) : (
              <CalendarBody
                view={events.data?.view ?? view}
                from={events.data?.from ?? from}
                to={events.data?.to ?? to}
                anchor={anchor}
                events={shown}
                colorOf={colorOf}
                selectedId={selectedId}
                onSelect={select}
                onOpenDay={openDay}
              />
            )}
          </div>
          {selected && selectedId === selected.id && (
            <EventPanel
              event={selected}
              color={colorOf(selected)}
              calendarName={calendars.get(calendarKey(selected.account_id, selected.calendar_id))?.name ?? null}
              accountLabel={accountLabel(selected.account_id)}
              onClose={closePanel}
            />
          )}
        </div>
      </main>

      <Modal open={choosing} onClose={() => setChoosing(false)} title="Synced calendars" size="md">
        <div className="flex flex-col gap-3 p-5">
          <p className="text-xs text-slate-400">
            Mingly syncs the checked calendars. Unchecking one removes its events from Mingly; to just hide a calendar, use its checkbox in the sidebar.
          </p>
          {gmailAccounts
            .filter((a) => accountId == null || a.account_id === accountId)
            .map((a) => (
              <CalendarPicker key={a.account_key} accountId={a.account_id!} label={multi ? a.label ?? null : null} defaultOpen />
            ))}
        </div>
      </Modal>
    </div>
  )
}

function CalendarBody({ view, from, to, anchor, events, colorOf, selectedId, onSelect, onOpenDay }: {
  view: CalendarView
  from: Date
  to: Date
  anchor: Date
  events: CalendarEvent[]
  colorOf: (ev: CalendarEvent) => string
  selectedId: number | null
  onSelect: (id: number) => void
  onOpenDay: (day: Date) => void
}) {
  const days = useMemo(() => gridDays(from, to), [from, to])
  if (view === 'day' || view === 'week') {
    return (
      <TimeGrid
        days={days}
        events={events}
        colorOf={colorOf}
        selectedId={selectedId}
        onSelect={onSelect}
        onOpenDay={view === 'week' ? onOpenDay : undefined}
      />
    )
  }
  if (view === 'month') {
    return (
      <MonthGrid
        from={from}
        month={anchor.getMonth()}
        events={events}
        colorOf={colorOf}
        selectedId={selectedId}
        onSelect={onSelect}
        onOpenDay={onOpenDay}
      />
    )
  }
  return (
    <div className="flex-1 overflow-y-auto">
      <AgendaList
        from={from}
        to={to}
        events={events}
        colorOf={colorOf}
        selectedId={selectedId}
        onSelect={onSelect}
        emptyText={`No events in ${rangeLabel(from, to)}`}
      />
    </div>
  )
}
