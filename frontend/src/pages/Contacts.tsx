import { useEffect, useState, useCallback, useRef } from 'react'
import { Link } from 'react-router-dom'
import { RefreshCw, Search, X, Plus, Building2, Pencil, Twitter, Linkedin, Phone, StickyNote, ExternalLink } from 'lucide-react'
import type { Contact, ContactPosition } from '../api/types'
import { getContacts, syncContacts, updateContact, addPosition, removePosition } from '../api/client'

function ContactCard({
  contact,
  selected,
  onClick,
}: {
  contact: Contact
  selected: boolean
  onClick: () => void
}) {
  return (
    <div
      onClick={onClick}
      className={`flex cursor-pointer items-center gap-3 px-4 py-3 transition-colors ${
        selected ? 'bg-indigo-500/15' : 'hover:bg-slate-700/50'
      }`}
    >
      <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-sm font-medium text-slate-300">
        {contact.name.charAt(0).toUpperCase()}
      </div>
      <div className="min-w-0 flex-1">
        <p className={`truncate text-sm font-medium ${selected ? 'text-indigo-300' : 'text-gray-200'}`}>
          {contact.name}
        </p>
        {contact.username && (
          <p className="truncate text-xs text-slate-500">@{contact.username}</p>
        )}
        {contact.positions.length > 0 && (
          <p className="truncate text-[10px] text-slate-500 mt-0.5">
            {contact.positions.map((p) => p.title ? `${p.title} @ ${p.organization}` : p.organization).join(', ')}
          </p>
        )}
      </div>
    </div>
  )
}

function EditableField({
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

export default function Contacts() {
  const [contacts, setContacts] = useState<Contact[]>([])
  const [selected, setSelected] = useState<Contact | null>(null)
  const [search, setSearch] = useState('')
  const [syncing, setSyncing] = useState(false)
  const [loading, setLoading] = useState(true)
  const [sidebarWidth, setSidebarWidth] = useState(320)
  const dragging = useRef(false)

  // Position form
  const [showAddPos, setShowAddPos] = useState(false)
  const [newOrg, setNewOrg] = useState('')
  const [newTitle, setNewTitle] = useState('')

  const loadContacts = useCallback(async (query = '') => {
    const data = await getContacts(query)
    setContacts(data)
    setLoading(false)
  }, [])

  useEffect(() => { loadContacts() }, [loadContacts])

  useEffect(() => {
    const timer = setTimeout(() => loadContacts(search), 200)
    return () => clearTimeout(timer)
  }, [search, loadContacts])

  const handleSync = async () => {
    setSyncing(true)
    try {
      await syncContacts()
      await loadContacts(search)
    } finally {
      setSyncing(false)
    }
  }

  const handleSelect = (c: Contact) => {
    setSelected(c)
    setShowAddPos(false)
  }

  const handleUpdateField = async (field: string, value: string) => {
    if (!selected) return
    const updated = await updateContact(selected.id, { [field]: value })
    setSelected(updated)
    setContacts((prev) => prev.map((c) => c.id === updated.id ? updated : c))
  }

  const handleAddPosition = async () => {
    if (!selected || !newOrg.trim()) return
    const pos = await addPosition(selected.id, newOrg.trim(), newTitle.trim() || undefined)
    const updated = { ...selected, positions: [...selected.positions, pos] }
    setSelected(updated)
    setContacts((prev) => prev.map((c) => c.id === updated.id ? updated : c))
    setNewOrg('')
    setNewTitle('')
    setShowAddPos(false)
  }

  const handleRemovePosition = async (posId: number) => {
    if (!selected) return
    await removePosition(selected.id, posId)
    const updated = { ...selected, positions: selected.positions.filter((p) => p.id !== posId) }
    setSelected(updated)
    setContacts((prev) => prev.map((c) => c.id === updated.id ? updated : c))
  }

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    dragging.current = true
    const startX = e.clientX
    const startWidth = sidebarWidth
    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return
      setSidebarWidth(Math.min(480, Math.max(240, startWidth + e.clientX - startX)))
    }
    const onUp = () => {
      dragging.current = false
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [sidebarWidth])

  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Contact list */}
      <div className="flex flex-shrink-0 flex-col border-r border-slate-700/50 bg-slate-800 relative" style={{ width: sidebarWidth }}>
        <div
          onMouseDown={onDragStart}
          className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-indigo-500 transition-colors z-10"
        />

        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-700/50 px-4 py-3">
          <h2 className="text-sm font-semibold text-gray-100">Contacts</h2>
          <button
            onClick={handleSync}
            disabled={syncing}
            title="Sync contacts from Telegram"
            className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50 transition-colors"
          >
            <RefreshCw size={13} className={syncing ? 'animate-spin' : ''} />
            Sync
          </button>
        </div>

        {/* Search */}
        <div className="border-b border-slate-700/50 px-3 py-3">
          <div className="relative">
            <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500" />
            <input
              type="text"
              placeholder="Search contacts..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full rounded-lg border border-slate-600 bg-slate-700 pl-8 pr-3 py-1.5 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
            />
          </div>
        </div>

        {/* List */}
        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <p className="px-4 py-6 text-center text-sm text-slate-500">Loading...</p>
          ) : contacts.length === 0 ? (
            <div className="px-4 py-6 text-center">
              <p className="text-sm text-slate-500">No contacts found</p>
              <button
                onClick={handleSync}
                disabled={syncing}
                className="mt-2 text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
              >
                Sync from Telegram
              </button>
            </div>
          ) : (
            contacts.map((c) => (
              <ContactCard
                key={c.id}
                contact={c}
                selected={selected?.id === c.id}
                onClick={() => handleSelect(c)}
              />
            ))
          )}
        </div>
      </div>

      {/* Detail panel */}
      <div className="flex flex-1 flex-col overflow-hidden bg-slate-900">
        {selected ? (
          <div className="flex-1 overflow-y-auto px-6 py-6 max-w-2xl">
            {/* Identity */}
            <div className="flex items-start gap-4 mb-6">
              <Link to={`/profile/${selected.telegram_id}`}>
                <div className="flex h-16 w-16 items-center justify-center rounded-full bg-slate-700 text-xl font-semibold text-slate-300 hover:ring-2 hover:ring-indigo-500 transition-all">
                  {selected.name.charAt(0).toUpperCase()}
                </div>
              </Link>
              <div className="flex-1 min-w-0">
                <Link
                  to={`/profile/${selected.telegram_id}`}
                  className="text-lg font-bold text-gray-100 hover:text-indigo-300 transition-colors"
                >
                  {selected.name}
                </Link>
                {selected.username && (
                  <p className="text-sm text-indigo-400">@{selected.username}</p>
                )}
                <Link
                  to={`/profile/${selected.telegram_id}`}
                  className="inline-flex items-center gap-1 mt-1 text-[10px] text-slate-500 hover:text-slate-300 transition-colors"
                >
                  <ExternalLink size={9} />
                  View Telegram profile
                </Link>
              </div>
            </div>

            {/* Extra fields */}
            <div className="rounded-xl bg-slate-800 border border-slate-700/50 p-4 mb-4 flex flex-col gap-3">
              <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500">Contact Info</h3>
              <EditableField
                icon={<Phone size={13} />}
                label="Phone"
                value={selected.phone}
                placeholder="Add phone..."
                onSave={(v) => handleUpdateField('phone', v)}
              />
              <EditableField
                icon={<Twitter size={13} />}
                label="Twitter"
                value={selected.twitter_handle}
                placeholder="Add handle..."
                onSave={(v) => handleUpdateField('twitter_handle', v)}
                isUrl={false}
              />
              <EditableField
                icon={<Linkedin size={13} />}
                label="LinkedIn"
                value={selected.linkedin_url}
                placeholder="Add URL..."
                onSave={(v) => handleUpdateField('linkedin_url', v)}
                isUrl
              />
              <EditableField
                icon={<StickyNote size={13} />}
                label="Notes"
                value={selected.notes}
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

              {selected.positions.length === 0 && !showAddPos && (
                <p className="text-xs text-slate-500">No positions added</p>
              )}

              {selected.positions.map((p) => (
                <div key={p.id} className="flex items-center gap-2 group">
                  <Building2 size={12} className="text-slate-500 flex-shrink-0" />
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-gray-200 truncate">
                      {p.title ? <><span className="text-slate-400">{p.title}</span> @ </> : ''}
                      {p.organization}
                    </p>
                  </div>
                  <button
                    onClick={() => handleRemovePosition(p.id)}
                    className="opacity-0 group-hover:opacity-100 text-slate-500 hover:text-rose-400 transition-all p-0.5"
                  >
                    <X size={10} />
                  </button>
                </div>
              ))}

              {showAddPos && (
                <div className="flex flex-col gap-2 rounded-lg border border-slate-600 bg-slate-700/50 p-3">
                  <input
                    autoFocus
                    value={newOrg}
                    onChange={(e) => setNewOrg(e.target.value)}
                    placeholder="Organization name..."
                    className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
                  />
                  <input
                    value={newTitle}
                    onChange={(e) => setNewTitle(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') handleAddPosition() }}
                    placeholder="Title (optional)..."
                    className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200 outline-none focus:border-indigo-500"
                  />
                  <div className="flex gap-2">
                    <button
                      onClick={handleAddPosition}
                      disabled={!newOrg.trim()}
                      className="flex-1 rounded bg-indigo-500 px-2 py-1 text-xs font-medium text-white hover:bg-indigo-600 disabled:opacity-40 transition-colors"
                    >
                      Add Position
                    </button>
                    <button
                      onClick={() => setShowAddPos(false)}
                      className="rounded px-2 py-1 text-xs text-slate-400 hover:text-slate-200 transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center text-slate-500">
            <p className="text-sm">Select a contact to view details</p>
          </div>
        )}
      </div>
    </div>
  )
}
