/**
 * Which Google calendars of an account are synced (default: the primary
 * one); unchecking one removes its events from Mingly. Loads the account's calendar list from Google when opened; each
 * toggle saves at once and the backend re-syncs.
 */
import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { getGoogleCalendars, PRIMARY_CALENDAR, setSyncedCalendars, type GoogleCalendar } from '../../api/calendar'
import { useLatestQuery } from '../../hooks/useLatestQuery'
import { errorMessage, reportError } from '../../lib/toast'

const syncId = (c: GoogleCalendar) => (c.primary ? PRIMARY_CALENDAR : c.id)

export default function CalendarPicker({ accountId, label, defaultOpen = false }: {
  accountId: number
  label: string | null
  /** Start expanded (in the synced-calendars dialog) */
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const [saving, setSaving] = useState(false)
  const list = useLatestQuery(['google-calendars', accountId], (signal) => getGoogleCalendars(accountId, signal), { enabled: open })

  const toggle = async (cal: GoogleCalendar) => {
    const current = list.data ?? []
    const next = current.map((c) => (c.id === cal.id ? { ...c, selected: !c.selected } : c))
    list.setData(next)
    setSaving(true)
    try {
      await setSyncedCalendars(accountId, next.filter((c) => c.selected).map(syncId))
    } catch (e) {
      reportError(e, 'Couldn’t change the synced calendars')
    } finally {
      setSaving(false)
      void list.refetch()
    }
  }

  return (
    <div className="flex flex-col gap-1" data-testid="calendar-picker">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 px-1 text-left text-[11px] text-slate-400 hover:text-slate-200"
        aria-expanded={open}
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span className="truncate">Calendars{label ? ` · ${label}` : ''}</span>
      </button>
      {open && (
        list.loading ? (
          <p className="px-2 text-[11px] text-slate-500">Loading…</p>
        ) : list.error ? (
          <p className="px-2 text-[11px] text-red-400">{errorMessage(list.error)}</p>
        ) : (
          <div className="flex flex-col gap-0.5 pl-2">
            {(list.data ?? []).map((c) => (
              <label key={c.id} className="flex items-center gap-2 rounded px-1 py-0.5 text-xs text-slate-300 hover:bg-slate-700/40">
                <input type="checkbox" checked={c.selected} disabled={saving} onChange={() => { void toggle(c) }} />
                <span className="h-2 w-2 flex-shrink-0 rounded-full" style={{ background: c.color ?? '#64748b' }} aria-hidden />
                <span className="truncate">{c.summary}</span>
              </label>
            ))}
          </div>
        )
      )}
    </div>
  )
}
