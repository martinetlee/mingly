import { useEffect, useRef, useState } from 'react'
import { Plus, Building2, X, UserMinus, RotateCcw, Loader2 } from 'lucide-react'
import type { ContactPosition } from '../api/types'
import { getPositions, addPosition, removePosition, updatePosition } from '../api/client'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useCoalescedRefetch } from '../hooks/useCoalescedRefetch'
import { useWsEvent } from '../hooks/useWebSocket'
import { invalidatePerson } from '../hooks/usePersonSummary'
import { useConfirm } from './ui'
import { errorMessage } from '../lib/toast'
import { parseServerDate } from '../utils/time'
import ComboInput from './ComboInput'

/** End dates are calendar dates: show them in UTC so they don't shift a day. */
function fmtEndDate(iso: string): string {
  const d = parseServerDate(iso)
  return d ? d.toLocaleDateString(undefined, { timeZone: 'UTC' }) : iso
}

/** Current positions first, then by org name (the server's order). */
function sortPositions(list: ContactPosition[]): ContactPosition[] {
  return [...list].sort((a, b) =>
    Number(b.is_active !== false) - Number(a.is_active !== false) ||
    a.organization.localeCompare(b.organization, undefined, { sensitivity: 'base' }),
  )
}

/**
 * Job history for any person (contact or not), keyed by person id.
 * The single positions UI for Profile, ContactInfoSection and the Contacts
 * info panel.
 *
 * Mutations apply to the latest list (never a closure from when the request
 * started), so overlapping add/remove/update calls can't resurrect or revert
 * each other. Failures are shown inline and keep the form open.
 */
export default function PositionsEditor({
  personId,
  initialPositions,
  onChange,
  compact = false,
}: {
  personId: number
  /**
   * The person's positions when the caller already has them (GET /api/people/{id}
   * includes them): no fetch, and the caller's refreshes flow in through this prop.
   * Omitted → the list is fetched (and reloaded on person_changed) here.
   */
  initialPositions?: ContactPosition[]
  /** Called with the new list after every successful mutation */
  onChange?: (positions: ContactPosition[]) => void
  /** Denser layout for side panels (same actions) */
  compact?: boolean
}) {
  const confirm = useConfirm()
  const provided = initialPositions !== undefined
  const { data, error: loadError, setData, refetch } = useLatestQuery(
    ['positions', personId],
    (signal) => getPositions(personId, signal),
    { enabled: !provided },
  )
  // Local result of this editor's own mutations, until the caller's copy catches up
  const [local, setLocal] = useState<{ base: ContactPosition[] | undefined; list: ContactPosition[] } | null>(null)
  const ownList = local && local.base === initialPositions ? local.list : null
  const positions = provided ? (ownList ?? initialPositions ?? []) : (data ?? [])

  // Latest list, updated synchronously by every mutation so two completions
  // in the same tick both apply
  const listRef = useRef<ContactPosition[]>(positions)
  const keyRef = useRef(personId)
  useEffect(() => { if (data) listRef.current = data }, [data])
  // A fresh copy from the caller (its own refresh) replaces the list
  useEffect(() => { if (provided && initialPositions) listRef.current = initialPositions }, [provided, initialPositions])
  useEffect(() => {
    keyRef.current = personId
    listRef.current = initialPositions ?? []
    setShowAdd(false)
    resetForm()
    setRowError({})
    setFormError(null)
    setEndingId(null)
    // initialPositions is only a placeholder for the new person
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [personId])

  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  // An agent (MCP) changed this person: reload the list in place. The add
  // form and end-date picker are local drafts and survive the reload.
  const refreshingRef = useRef(false)
  const refresh = useCoalescedRefetch(async () => {
    refreshingRef.current = true
    try { await refetch() } finally { refreshingRef.current = false }
  }, 1000)
  // With caller-provided positions the caller refreshes the person (and passes the new list)
  useWsEvent('person_changed', (e) => { if (e.person_id === keyRef.current) refresh() }, !provided)

  const [showAdd, setShowAdd] = useState(false)
  const [newOrg, setNewOrg] = useState('')
  const [newTitle, setNewTitle] = useState('')
  const [newIsFormer, setNewIsFormer] = useState(false)
  const [newEndedAt, setNewEndedAt] = useState('')
  const [adding, setAdding] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  // Row being marked former (end-date picker open) and its draft date
  const [endingId, setEndingId] = useState<number | null>(null)
  const [endDraft, setEndDraft] = useState('')
  const [busy, setBusy] = useState<Set<number>>(new Set())
  const [rowError, setRowError] = useState<Record<number, string>>({})

  function resetForm() {
    setNewOrg('')
    setNewTitle('')
    setNewIsFormer(false)
    setNewEndedAt('')
  }

  /** Apply `fn` to the latest list — only if we're still on the same person. */
  const apply = (pid: number, fn: (prev: ContactPosition[]) => ContactPosition[]) => {
    invalidatePerson(pid)
    if (keyRef.current !== pid) return
    const next = fn(listRef.current)
    listRef.current = next
    if (provided) setLocal({ base: initialPositions, list: next })
    else setData(next)
    onChangeRef.current?.(next)
    // setData discards an in-flight agent refresh; run it again on top of this result
    if (refreshingRef.current) refresh()
  }

  const setRowBusy = (id: number, on: boolean) =>
    setBusy((prev) => {
      const next = new Set(prev)
      if (on) next.add(id); else next.delete(id)
      return next
    })

  const rowAction = async (id: number, fn: () => Promise<void>) => {
    if (busy.has(id)) return
    const pid = personId
    setRowBusy(id, true)
    setRowError(({ [id]: _drop, ...rest }) => rest)
    try {
      await fn()
    } catch (e) {
      if (keyRef.current === pid) setRowError((prev) => ({ ...prev, [id]: errorMessage(e) }))
    } finally {
      if (keyRef.current === pid) setRowBusy(id, false)
    }
  }

  const handleAdd = async () => {
    const org = newOrg.trim()
    if (!org || adding) return
    const pid = personId
    setAdding(true)
    setFormError(null)
    try {
      const pos = await addPosition(
        pid, org, newTitle.trim() || undefined, !newIsFormer, newIsFormer ? newEndedAt || undefined : undefined,
      )
      apply(pid, (prev) => sortPositions([...prev.filter((p) => p.id !== pos.id), pos]))
      if (keyRef.current === pid) {
        resetForm()
        setShowAdd(false)
      }
    } catch (e) {
      // Keep the form (and what was typed) so the user can fix and retry
      if (keyRef.current === pid) setFormError(errorMessage(e))
    } finally {
      if (keyRef.current === pid) setAdding(false)
    }
  }

  const handleRemove = async (p: ContactPosition) => {
    const pid = personId
    const ok = await confirm({
      title: 'Remove position?',
      message: `${p.title ? `${p.title} @ ` : ''}${p.organization} will be removed from this person's history.`,
      confirmLabel: 'Remove',
      destructive: true,
    })
    if (!ok) return
    await rowAction(p.id, async () => {
      await removePosition(pid, p.id)
      apply(pid, (prev) => prev.filter((x) => x.id !== p.id))
    })
  }

  const handleMarkFormer = (p: ContactPosition) => {
    const pid = personId
    return rowAction(p.id, async () => {
      const updated = await updatePosition(pid, p.id, { is_active: false, ended_at: endDraft || null })
      apply(pid, (prev) => sortPositions(prev.map((x) => (x.id === p.id ? updated : x))))
      if (keyRef.current === pid) setEndingId((v) => (v === p.id ? null : v))
    })
  }

  const handleReactivate = (p: ContactPosition) => {
    const pid = personId
    return rowAction(p.id, async () => {
      const updated = await updatePosition(pid, p.id, { is_active: true, ended_at: '' })
      apply(pid, (prev) => sortPositions(prev.map((x) => (x.id === p.id ? updated : x))))
    })
  }

  const pad = compact ? 'p-3' : 'p-4'

  return (
    <div className={`rounded-xl bg-slate-800 border border-slate-700/50 ${pad} flex flex-col gap-3`}>
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
          <Building2 size={11} />
          Positions
        </h3>
        <button
          onClick={() => { setShowAdd((v) => !v); setFormError(null) }}
          className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors flex items-center gap-1"
        >
          <Plus size={11} />
          Add
        </button>
      </div>

      {loadError != null && data === undefined && (
        <p className="text-[11px] text-rose-400">Couldn't load positions: {errorMessage(loadError)}</p>
      )}

      {positions.length === 0 && !showAdd && loadError == null && (
        <p className="text-xs text-slate-500">No positions added</p>
      )}

      {positions.map((p) => {
        const isFormer = p.is_active === false
        const isBusy = busy.has(p.id)
        return (
          <div key={p.id} className="flex flex-col gap-1">
            <div className={`flex items-center gap-2 group ${isFormer ? 'opacity-60' : ''}`}>
              <Building2 size={12} className={`flex-shrink-0 ${isFormer ? 'text-slate-600' : 'text-slate-500'}`} />
              <div className="flex-1 min-w-0">
                <p
                  title={`${p.title ? `${p.title} @ ` : ''}${p.organization}${p.organization_id == null ? ' (organization deleted)' : ''}`}
                  className={`text-xs truncate ${isFormer ? 'line-through text-slate-500' : 'text-gray-200'}`}
                >
                  {p.title ? <><span className={isFormer ? 'text-slate-600' : 'text-slate-400'}>{p.title}</span> @ </> : ''}
                  {p.organization}
                </p>
                {isFormer && (
                  <p className="text-[10px] text-slate-600 italic">
                    {p.ended_at ? `left ${fmtEndDate(p.ended_at)}` : 'former'}
                  </p>
                )}
              </div>
              <div className={`flex items-center gap-1 transition-opacity ${isBusy ? 'opacity-100' : 'opacity-0 group-hover:opacity-100 focus-within:opacity-100'}`}>
                {isBusy ? (
                  <Loader2 size={10} className="animate-spin text-slate-500" />
                ) : (
                  <>
                    {isFormer ? (
                      <button
                        onClick={() => handleReactivate(p)}
                        title="Reactivate"
                        className="text-slate-500 hover:text-emerald-400 transition-colors p-0.5"
                      >
                        <RotateCcw size={10} />
                      </button>
                    ) : (
                      <button
                        onClick={() => {
                          setEndingId((v) => (v === p.id ? null : p.id))
                          setEndDraft('')
                        }}
                        title="Mark as former"
                        className="text-slate-500 hover:text-amber-400 transition-colors p-0.5"
                      >
                        <UserMinus size={10} />
                      </button>
                    )}
                    <button
                      onClick={() => handleRemove(p)}
                      title="Remove position"
                      className="text-slate-500 hover:text-rose-400 transition-colors p-0.5"
                    >
                      <X size={10} />
                    </button>
                  </>
                )}
              </div>
            </div>
            {endingId === p.id && !isFormer && (
              <div className="ml-5 flex items-center gap-2 rounded bg-slate-700/50 border border-slate-600 px-2 py-1.5">
                <span className="text-[10px] text-slate-400 whitespace-nowrap">End date:</span>
                <input
                  type="date"
                  value={endDraft}
                  onChange={(e) => setEndDraft(e.target.value)}
                  className="flex-1 bg-transparent text-[10px] text-slate-200 outline-none min-w-0"
                />
                <button
                  onClick={() => handleMarkFormer(p)}
                  disabled={isBusy}
                  className="text-[10px] text-amber-400 hover:text-amber-300 transition-colors whitespace-nowrap font-medium disabled:opacity-50"
                >
                  Mark former
                </button>
                <button
                  onClick={() => setEndingId(null)}
                  title="Cancel"
                  className="text-[10px] text-slate-500 hover:text-slate-300 transition-colors"
                >
                  <X size={9} />
                </button>
              </div>
            )}
            {rowError[p.id] && (
              <p role="alert" className="ml-5 text-[10px] text-rose-400">{rowError[p.id]}</p>
            )}
          </div>
        )
      })}

      {showAdd && (
        <div className="flex flex-col gap-2 rounded-lg border border-slate-600 bg-slate-700/50 p-3">
          <ComboInput value={newOrg} onChange={setNewOrg} placeholder="Organization name..." autoFocus />
          <input
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') handleAdd() }}
            placeholder="Title (optional)..."
            className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
          />
          <label className="flex items-center gap-2 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={newIsFormer}
              onChange={(e) => {
                setNewIsFormer(e.target.checked)
                if (!e.target.checked) setNewEndedAt('')
              }}
              className="rounded border-slate-500 bg-slate-700 text-amber-500 focus:ring-amber-500/30"
            />
            <span className="text-xs text-slate-400">Former position</span>
          </label>
          {newIsFormer && (
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-slate-500 whitespace-nowrap">End date (optional):</span>
              <input
                type="date"
                value={newEndedAt}
                onChange={(e) => setNewEndedAt(e.target.value)}
                className="flex-1 bg-slate-700 border border-slate-600 rounded px-2 py-0.5 text-xs text-slate-200 outline-none focus:border-amber-500 min-w-0"
              />
            </div>
          )}
          {formError && <p role="alert" className="text-[11px] text-rose-400">{formError}</p>}
          <div className="flex gap-2">
            <button
              onClick={handleAdd}
              disabled={!newOrg.trim() || adding}
              className={`flex flex-1 items-center justify-center gap-1 rounded px-2 py-1 text-xs font-medium text-white disabled:opacity-40 transition-colors ${
                newIsFormer ? 'bg-amber-600 hover:bg-amber-500' : 'bg-indigo-500 hover:bg-indigo-600'
              }`}
            >
              {adding && <Loader2 size={11} className="animate-spin" />}
              {newIsFormer ? 'Add Former Position' : 'Add Position'}
            </button>
            <button
              onClick={() => { setShowAdd(false); resetForm(); setFormError(null) }}
              className="rounded px-2 py-1 text-xs text-slate-400 hover:text-slate-200 transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
