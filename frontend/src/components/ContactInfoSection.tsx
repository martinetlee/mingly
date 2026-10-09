import { useEffect, useRef, useState } from 'react'
import { Pencil, Twitter, Linkedin, Phone, StickyNote, Loader2 } from 'lucide-react'
import type { Person } from '../api/types'
import { getPerson, updatePerson } from '../api/client'
import { sanitizeUrl } from '../utils/sanitizeUrl'
import { errorMessage, reportError } from '../lib/toast'
import { invalidatePerson } from '../hooks/usePersonSummary'
import { useCoalescedRefetch } from '../hooks/useCoalescedRefetch'
import { useWsEvent } from '../hooks/useWebSocket'
import PositionsEditor from './PositionsEditor'

// ---------------------------------------------------------------------------
// EditableField
// ---------------------------------------------------------------------------

/**
 * Click-to-edit text field. Enter/blur saves; the field leaves edit mode only
 * once the save succeeded — on failure it stays open with the error shown.
 */
export function EditableField({
  icon,
  label,
  value,
  placeholder,
  onSave,
  isUrl,
}: {
  icon: React.ReactNode
  label: string
  value: string | null
  placeholder: string
  onSave: (val: string) => Promise<unknown> | void
  isUrl?: boolean
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value || '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const savingRef = useRef(false)

  // Adopt outside changes only while not editing — never clobber a draft
  useEffect(() => { if (!editing) setDraft(value || '') }, [value, editing])

  const cancel = () => { setEditing(false); setError(null); setDraft(value || '') }

  const save = async () => {
    if (savingRef.current) return
    if (draft === (value || '')) { cancel(); return }
    savingRef.current = true
    setSaving(true)
    setError(null)
    try {
      await onSave(draft)
      setEditing(false)
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      savingRef.current = false
      setSaving(false)
    }
  }

  if (editing) {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="text-slate-500 flex-shrink-0">{icon}</span>
          <input
            autoFocus
            aria-label={label}
            value={draft}
            disabled={saving}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') save()
              if (e.key === 'Escape') { e.stopPropagation(); cancel() }
            }}
            onBlur={() => { if (!error) save() }}
            placeholder={placeholder}
            className={`flex-1 bg-slate-700 border rounded px-2 py-0.5 text-xs text-slate-200 outline-none focus:border-indigo-500 ${error ? 'border-rose-500/60' : 'border-slate-600'}`}
          />
          {saving && <Loader2 size={11} className="animate-spin text-slate-500" />}
        </div>
        {error && (
          <p role="alert" className="pl-5 text-[10px] text-rose-400">
            {error} <button onClick={() => save()} className="underline hover:text-rose-300">Retry</button>{' '}
            <button onClick={cancel} className="underline hover:text-rose-300">Discard</button>
          </p>
        )}
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2 group">
      <span className="text-slate-500 flex-shrink-0">{icon}</span>
      <span className="w-16 text-[10px] text-slate-500 uppercase tracking-wider flex-shrink-0">{label}</span>
      {value ? (
        isUrl ? (
          (() => {
            const safeHref = sanitizeUrl(value)
            return safeHref ? (
              <a
                href={safeHref}
                target="_blank"
                rel="noopener noreferrer"
                className="flex-1 text-xs text-indigo-400 hover:text-indigo-300 truncate transition-colors"
              >
                {value}
              </a>
            ) : (
              <span className="flex-1 text-xs text-gray-200 truncate">{value}</span>
            )
          })()
        ) : (
          <span className="flex-1 text-xs text-gray-200 truncate">{value}</span>
        )
      ) : (
        <span className="flex-1 text-xs text-slate-600 italic">{placeholder}</span>
      )}
      <button
        onClick={() => setEditing(true)}
        title={`Edit ${label.toLowerCase()}`}
        className="opacity-0 group-hover:opacity-100 focus:opacity-100 text-slate-500 hover:text-slate-300 transition-opacity p-0.5"
      >
        <Pencil size={10} />
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// ContactInfoSection
// ---------------------------------------------------------------------------

interface ContactInfoSectionProps {
  person: Person
  onPersonChange: (updated: Person) => void
  /** Denser layout for side panels (same actions) */
  compact?: boolean
}

export default function ContactInfoSection({ person, onPersonChange, compact }: ContactInfoSectionProps) {
  // Completions merge into the person as rendered *now*, not as it was when
  // the request started — so a position change can't revert a concurrent
  // notes edit (and vice versa)
  const personRef = useRef(person)
  personRef.current = person
  const onChangeRef = useRef(onPersonChange)
  onChangeRef.current = onPersonChange

  // Bumped when a local field save starts and ends: a refresh that overlapped
  // one is stale (it may predate the save) and is redone instead of applied
  const writeSeqRef = useRef(0)

  // An agent (MCP) changed this person: reload it in place. Fields being
  // edited keep their draft (EditableField adopts outside values only when
  // not editing), so the new value shows once the edit closes.
  const refreshPerson = useCoalescedRefetch(async () => {
    const id = personRef.current.id
    const seq = writeSeqRef.current
    try {
      const fresh = await getPerson(id)
      if (personRef.current.id !== id) return
      if (seq !== writeSeqRef.current) { refreshPerson(); return }
      onChangeRef.current(fresh)
    } catch (e) {
      reportError(e, "Couldn't refresh person")
    }
  }, 1000)
  useWsEvent('person_changed', (e) => { if (e.person_id === personRef.current.id) refreshPerson() })

  const iconSize = compact ? 12 : 13
  const pad = compact ? 'p-3' : 'p-4'

  const handleUpdateField = async (field: 'twitter_handle' | 'linkedin_url' | 'notes', value: string) => {
    const id = person.id
    writeSeqRef.current++
    let updated: Person
    try {
      updated = await updatePerson(id, { [field]: value })
    } finally {
      writeSeqRef.current++
    }
    invalidatePerson(updated.id)
    // Server response is the full person; keep the positions we hold if it omits them
    if (personRef.current.id === id) {
      onChangeRef.current({ ...updated, positions: updated.positions ?? personRef.current.positions })
    }
  }

  // Phones come from the person's channel identities (read-only here)
  const phones = Array.from(new Set(person.identities.map((i) => i.phone).filter((p): p is string => !!p)))

  return (
    <>
      {/* Extra fields */}
      <div className={`rounded-xl bg-slate-800 border border-slate-700/50 ${pad} flex flex-col gap-3`}>
        <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500">Contact Info</h3>
        {phones.length > 0 && (
          <div className="flex items-center gap-2">
            <span className="text-slate-500 flex-shrink-0"><Phone size={iconSize} /></span>
            <span className="w-16 text-[10px] text-slate-500 uppercase tracking-wider flex-shrink-0">Phone</span>
            <span className="flex-1 text-xs text-gray-200 truncate">{phones.join(', ')}</span>
          </div>
        )}
        <EditableField
          icon={<Twitter size={iconSize} />}
          label="Twitter"
          value={person.twitter_handle}
          placeholder="Add handle..."
          onSave={(v) => handleUpdateField('twitter_handle', v)}
        />
        <EditableField
          icon={<Linkedin size={iconSize} />}
          label="LinkedIn"
          value={person.linkedin_url}
          placeholder="Add URL..."
          onSave={(v) => handleUpdateField('linkedin_url', v)}
          isUrl
        />
        <EditableField
          icon={<StickyNote size={iconSize} />}
          label="Notes"
          value={person.notes}
          placeholder="Add notes..."
          onSave={(v) => handleUpdateField('notes', v)}
        />
      </div>

      <PositionsEditor
        key={person.id}
        personId={person.id}
        initialPositions={person.positions}
        compact={compact}
        onChange={(positions) => {
          const current = personRef.current
          if (current.id === person.id) onChangeRef.current({ ...current, positions })
        }}
      />
    </>
  )
}
