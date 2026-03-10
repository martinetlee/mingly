import { useEffect, useState } from 'react'
import { Plus, Building2, Pencil, Twitter, Linkedin, Phone, StickyNote, X, UserMinus, RotateCcw } from 'lucide-react'
import type { Contact, ContactPosition } from '../api/types'
import { updateContact, addPosition, removePosition, updatePosition } from '../api/client'
import ComboInput from './ComboInput'

// ---------------------------------------------------------------------------
// EditableField
// ---------------------------------------------------------------------------

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
  onSave: (val: string) => void
  isUrl?: boolean
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value || '')

  useEffect(() => { setDraft(value || ''); setEditing(false) }, [value])

  if (editing) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-slate-500 flex-shrink-0">{icon}</span>
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { onSave(draft); setEditing(false) }
            if (e.key === 'Escape') setEditing(false)
          }}
          onBlur={() => { onSave(draft); setEditing(false) }}
          placeholder={placeholder}
          className="flex-1 bg-slate-700 border border-slate-600 rounded px-2 py-0.5 text-xs text-slate-200 outline-none focus:border-indigo-500"
        />
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2 group">
      <span className="text-slate-500 flex-shrink-0">{icon}</span>
      <span className="w-16 text-[10px] text-slate-500 uppercase tracking-wider flex-shrink-0">{label}</span>
      {value ? (
        isUrl ? (
          <a
            href={value.startsWith('http') ? value : `https://${value}`}
            target="_blank"
            rel="noopener noreferrer"
            className="flex-1 text-xs text-indigo-400 hover:text-indigo-300 truncate transition-colors"
          >
            {value}
          </a>
        ) : (
          <span className="flex-1 text-xs text-gray-200 truncate">{value}</span>
        )
      ) : (
        <span className="flex-1 text-xs text-slate-600 italic">{placeholder}</span>
      )}
      <button
        onClick={() => setEditing(true)}
        className="opacity-0 group-hover:opacity-100 text-slate-500 hover:text-slate-300 transition-opacity p-0.5"
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
  /** Compact mode hides former-member management and uses smaller icons */
  compact?: boolean
}

export default function ContactInfoSection({ contact, onContactChange, compact }: ContactInfoSectionProps) {
  const [showAddPos, setShowAddPos] = useState(false)
  const [newOrg, setNewOrg] = useState('')
  const [newTitle, setNewTitle] = useState('')
  const [newIsFormer, setNewIsFormer] = useState(false)
  const [newEndedAt, setNewEndedAt] = useState('')
  const [formerDrafts, setFormerDrafts] = useState<Record<number, string>>({})
  const [confirmingFormer, setConfirmingFormer] = useState<Set<number>>(new Set())

  // Reset position form when contact changes
  useEffect(() => {
    setShowAddPos(false)
    setNewOrg('')
    setNewTitle('')
    setNewIsFormer(false)
    setNewEndedAt('')
    setConfirmingFormer(new Set())
    setFormerDrafts({})
  }, [contact.id])

  const iconSize = compact ? 12 : 13

  const handleUpdateField = async (field: string, value: string) => {
    const updated = await updateContact(contact.id, { [field]: value })
    onContactChange(updated)
  }

  const handleAddPosition = async () => {
    if (!newOrg.trim()) return
    const pos = await addPosition(
      contact.id,
      newOrg.trim(),
      newTitle.trim() || undefined,
      !newIsFormer,
      newIsFormer ? newEndedAt || undefined : undefined,
    )
    onContactChange({ ...contact, positions: [...contact.positions, pos] })
    setNewOrg('')
    setNewTitle('')
    setNewIsFormer(false)
    setNewEndedAt('')
    setShowAddPos(false)
  }

  const handleRemovePosition = async (posId: number) => {
    await removePosition(contact.id, posId)
    onContactChange({ ...contact, positions: contact.positions.filter((p) => p.id !== posId) })
  }

  const handleMarkFormer = async (posId: number, endedAt: string) => {
    const updatedPos = await updatePosition(contact.id, posId, {
      is_active: false,
      ended_at: endedAt || null,
    })
    onContactChange({
      ...contact,
      positions: contact.positions.map((p) => p.id === posId ? updatedPos : p),
    })
  }

  const handleReactivate = async (posId: number) => {
    const updatedPos = await updatePosition(contact.id, posId, {
      is_active: true,
      ended_at: null,
    })
    onContactChange({
      ...contact,
      positions: contact.positions.map((p) => p.id === posId ? updatedPos : p),
    })
  }

  return (
    <>
      {/* Extra fields */}
      <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4 flex flex-col gap-3">
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

      {/* Positions */}
      <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4 flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
            <Building2 size={11} />
            Positions
          </h3>
          <button
            onClick={() => setShowAddPos((v) => !v)}
            className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors flex items-center gap-1"
          >
            <Plus size={11} />
            Add
          </button>
        </div>

        {contact.positions.length === 0 && !showAddPos && (
          <p className="text-xs text-slate-500">No positions added</p>
        )}

        {contact.positions.map((p) => {
          const isFormer = p.is_active === false
          const isConfirming = confirmingFormer.has(p.id)
          return (
            <div key={p.id} className="flex flex-col gap-1">
              <div className={`flex items-center gap-2 group ${isFormer ? 'opacity-60' : ''}`}>
                <Building2 size={12} className={`flex-shrink-0 ${isFormer ? 'text-slate-600' : 'text-slate-500'}`} />
                <div className="flex-1 min-w-0">
                  <p className={`text-xs truncate ${isFormer ? 'line-through text-slate-500' : 'text-gray-200'}`}>
                    {p.title ? <><span className={isFormer ? 'text-slate-600' : 'text-slate-400'}>{p.title}</span> @ </> : ''}
                    {p.organization}
                  </p>
                  {isFormer && p.ended_at && (
                    <p className="text-[10px] text-slate-600">
                      left {new Date(p.ended_at).toLocaleDateString()}
                    </p>
                  )}
                  {isFormer && !p.ended_at && (
                    <p className="text-[10px] text-slate-600 italic">former</p>
                  )}
                </div>
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  {!compact && isFormer && (
                    <button
                      onClick={() => handleReactivate(p.id)}
                      title="Reactivate"
                      className="text-slate-500 hover:text-emerald-400 transition-colors p-0.5"
                    >
                      <RotateCcw size={10} />
                    </button>
                  )}
                  {!compact && !isFormer && (
                    <button
                      onClick={() => {
                        setConfirmingFormer((prev) => {
                          const next = new Set(prev)
                          if (next.has(p.id)) { next.delete(p.id) } else { next.add(p.id) }
                          return next
                        })
                        setFormerDrafts((prev) => ({ ...prev, [p.id]: '' }))
                      }}
                      title="Mark as former"
                      className="text-slate-500 hover:text-amber-400 transition-colors p-0.5"
                    >
                      <UserMinus size={10} />
                    </button>
                  )}
                  <button
                    onClick={() => handleRemovePosition(p.id)}
                    title="Remove position"
                    className="text-slate-500 hover:text-rose-400 transition-colors p-0.5"
                  >
                    <X size={10} />
                  </button>
                </div>
              </div>
              {!compact && isConfirming && !isFormer && (
                <div className="ml-5 flex items-center gap-2 rounded bg-slate-700/50 border border-slate-600 px-2 py-1.5">
                  <span className="text-[10px] text-slate-400 whitespace-nowrap">End date:</span>
                  <input
                    type="date"
                    value={formerDrafts[p.id] || ''}
                    onChange={(e) => setFormerDrafts((prev) => ({ ...prev, [p.id]: e.target.value }))}
                    className="flex-1 bg-transparent text-[10px] text-slate-200 outline-none min-w-0"
                  />
                  <button
                    onClick={async () => {
                      await handleMarkFormer(p.id, formerDrafts[p.id] || '')
                      setConfirmingFormer((prev) => { const next = new Set(prev); next.delete(p.id); return next })
                    }}
                    className="text-[10px] text-amber-400 hover:text-amber-300 transition-colors whitespace-nowrap font-medium"
                  >
                    Mark former
                  </button>
                  <button
                    onClick={() => setConfirmingFormer((prev) => { const next = new Set(prev); next.delete(p.id); return next })}
                    className="text-[10px] text-slate-500 hover:text-slate-300 transition-colors"
                  >
                    <X size={9} />
                  </button>
                </div>
              )}
            </div>
          )
        })}

        {showAddPos && (
          <div className="flex flex-col gap-2 rounded-lg border border-slate-600 bg-slate-700/50 p-3">
            <ComboInput
              value={newOrg}
              onChange={setNewOrg}
              placeholder="Organization name..."
              autoFocus
            />
            <input
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') handleAddPosition() }}
              placeholder="Title (optional)..."
              className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
            />
            {!compact && (
              <>
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
              </>
            )}
            <div className="flex gap-2">
              <button
                onClick={handleAddPosition}
                disabled={!newOrg.trim()}
                className={`flex-1 rounded px-2 py-1 text-xs font-medium text-white disabled:opacity-40 transition-colors ${
                  newIsFormer ? 'bg-amber-600 hover:bg-amber-500' : 'bg-indigo-500 hover:bg-indigo-600'
                }`}
              >
                {newIsFormer ? 'Add Former Position' : 'Add Position'}
              </button>
              <button
                onClick={() => { setShowAddPos(false); setNewIsFormer(false); setNewEndedAt('') }}
                className="rounded px-2 py-1 text-xs text-slate-400 hover:text-slate-200 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </>
  )
}
