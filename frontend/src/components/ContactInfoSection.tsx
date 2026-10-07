import { useEffect, useRef, useState } from 'react'
import { Pencil, Twitter, Linkedin, Phone, StickyNote, Loader2 } from 'lucide-react'
import type { Contact } from '../api/types'
import { getPerson, updateContact } from '../api/client'
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
  contact: Contact
  onContactChange: (updated: Contact) => void
  /** Denser layout for side panels (same actions) */
  compact?: boolean
}

export default function ContactInfoSection({ contact, onContactChange, compact }: ContactInfoSectionProps) {
  // Completions merge into the contact as rendered *now*, not as it was when
  // the request started — so a position change can't revert a concurrent
  // phone/notes edit (and vice versa)
  const contactRef = useRef(contact)
  contactRef.current = contact
  const onChangeRef = useRef(onContactChange)
  onChangeRef.current = onContactChange

  // Bumped when a local field save starts and ends: a refresh that overlapped
  // one is stale (it may predate the save) and is redone instead of applied
  const writeSeqRef = useRef(0)

  // An agent (MCP) changed this person: reload the contact in place. Fields
  // being edited keep their draft (EditableField adopts outside values only
  // when not editing), so the new value shows once the edit closes.
  const refreshContact = useCoalescedRefetch(async () => {
    const { id, telegram_id: tid } = contactRef.current
    const seq = writeSeqRef.current
    try {
      const person = await getPerson(tid)
      if (contactRef.current.id !== id || !person.contact) return
      if (seq !== writeSeqRef.current) { refreshContact(); return }
      onChangeRef.current({ ...person.contact, positions: person.positions })
    } catch (e) {
      reportError(e, "Couldn't refresh contact")
    }
  }, 1000)
  useWsEvent('person_changed', (e) => { if (e.telegram_id === contactRef.current.telegram_id) refreshContact() })

  const iconSize = compact ? 12 : 13
  const pad = compact ? 'p-3' : 'p-4'

  const handleUpdateField = async (field: 'phone' | 'twitter_handle' | 'linkedin_url' | 'notes', value: string) => {
    const id = contact.id
    writeSeqRef.current++
    let updated: Contact
    try {
      updated = await updateContact(id, { [field]: value })
    } finally {
      writeSeqRef.current++
    }
    invalidatePerson(updated.telegram_id)
    // Server response is the full contact (positions included)
    if (contactRef.current.id === id) onChangeRef.current(updated)
  }

  return (
    <>
      {/* Extra fields */}
      <div className={`rounded-xl bg-slate-800 border border-slate-700/50 ${pad} flex flex-col gap-3`}>
        <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500">Contact Info</h3>
        <EditableField
          icon={<Phone size={iconSize} />}
          label="Phone"
          value={contact.phone}
          placeholder="Add phone..."
          onSave={(v) => handleUpdateField('phone', v)}
        />
        <EditableField
          icon={<Twitter size={iconSize} />}
          label="Twitter"
          value={contact.twitter_handle}
          placeholder="Add handle..."
          onSave={(v) => handleUpdateField('twitter_handle', v)}
        />
        <EditableField
          icon={<Linkedin size={iconSize} />}
          label="LinkedIn"
          value={contact.linkedin_url}
          placeholder="Add URL..."
          onSave={(v) => handleUpdateField('linkedin_url', v)}
          isUrl
        />
        <EditableField
          icon={<StickyNote size={iconSize} />}
          label="Notes"
          value={contact.notes}
          placeholder="Add notes..."
          onSave={(v) => handleUpdateField('notes', v)}
        />
      </div>

      <PositionsEditor
        key={contact.telegram_id}
        telegramId={contact.telegram_id}
        initialPositions={contact.positions}
        compact={compact}
        onChange={(positions) => {
          const current = contactRef.current
          if (current.telegram_id === contact.telegram_id) onChangeRef.current({ ...current, positions })
        }}
      />
    </>
  )
}
