import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { RefreshCw, Search, ExternalLink, Star, RotateCcw } from 'lucide-react'
import type { Contact, RelationshipStatus } from '../api/types'
import { getContacts, syncContacts, setContactImportantByTelegram, setRelationshipStatus, resetRelationshipStatus, refreshAllStatuses } from '../api/client'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import ContactInfoSection from '../components/ContactInfoSection'
import PersonCard from '../components/PersonCard'

const STATUS_COLORS: Record<string, string> = {
  new: 'bg-blue-500/20 text-blue-400',
  active: 'bg-emerald-500/20 text-emerald-400',
  nurturing: 'bg-amber-500/20 text-amber-400',
  stalled: 'bg-orange-500/20 text-orange-400',
  cold: 'bg-slate-500/20 text-slate-400',
  needs_reply: 'bg-red-500/20 text-red-400',
}

const STATUS_LABELS: Record<string, string> = {
  new: 'New',
  active: 'Active',
  nurturing: 'Nurturing',
  stalled: 'Stalled',
  cold: 'Cold',
  needs_reply: 'Needs Reply',
}

function fmtRelative(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffDays = Math.floor(diffMs / 86400000)
  if (diffDays === 0) return 'Today'
  if (diffDays === 1) return 'Yesterday'
  if (diffDays < 7) return `${diffDays} days ago`
  if (diffDays < 30) return `${Math.floor(diffDays / 7)} weeks ago`
  if (diffDays < 365) return `${Math.floor(diffDays / 30)} months ago`
  return `${Math.floor(diffDays / 365)} years ago`
}

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
        <div className="flex items-center gap-1.5">
          <p title={contact.name} className={`truncate text-sm font-medium ${selected ? 'text-indigo-300' : 'text-gray-200'}`}>
            {contact.name}
          </p>
          {contact.is_important && (
            <Star size={11} className="flex-shrink-0 fill-amber-400 text-amber-400" />
          )}
          {contact.relationship_status && (
            <span className={`flex-shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-medium ${STATUS_COLORS[contact.relationship_status] ?? ''}`}>
              {STATUS_LABELS[contact.relationship_status] ?? contact.relationship_status}
            </span>
          )}
        </div>
        {contact.username && (
          <p title={`@${contact.username}`} className="truncate text-xs text-slate-500">@{contact.username}</p>
        )}
        {contact.positions.filter((p) => p.is_active !== false).length > 0 && (
          <p title={contact.positions.filter((p) => p.is_active !== false).map((p) => p.title ? `${p.title} @ ${p.organization}` : p.organization).join(', ')} className="truncate text-[10px] text-slate-500 mt-0.5">
            {contact.positions.filter((p) => p.is_active !== false).map((p) => p.title ? `${p.title} @ ${p.organization}` : p.organization).join(', ')}
          </p>
        )}
      </div>
    </div>
  )
}

export default function Contacts() {
  const location = useLocation()
  const [contacts, setContacts] = useState<Contact[]>([])
  const [selected, setSelected] = useState<Contact | null>(null)
  const [search, setSearch] = useState('')
  const [syncing, setSyncing] = useState(false)
  const [loading, setLoading] = useState(true)
  const [importantOnly, setImportantOnly] = useState(() => {
    const params = new URLSearchParams(location.search)
    return params.get('filter') === 'important'
  })
  const [statusFilter, setStatusFilter] = useState<RelationshipStatus | 'all'>('all')
  const [sidebarWidth, setSidebarWidth] = useState(320)
  const dragging = useRef(false)
  const searchInputRef = useRef<HTMLInputElement>(null)

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

  const handleSelect = (c: Contact) => setSelected(c)

  const handleContactChange = (updated: Contact) => {
    setSelected(updated)
    setContacts((prev) => prev.map((c) => c.id === updated.id ? updated : c))
  }

  const handleToggleImportant = async () => {
    if (!selected) return
    const newVal = !selected.is_important
    const updated = await setContactImportantByTelegram(selected.telegram_id, newVal)
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

  const visibleContacts = useMemo(() =>
    contacts
      .filter((c) => statusFilter === 'all' || c.relationship_status === statusFilter)
      .filter((c) => !importantOnly || c.is_important),
    [contacts, statusFilter, importantOnly]
  )

  // Keyboard shortcuts
  const contactShortcuts = useMemo(() => ({
    'j': () => {
      const idx = selected ? visibleContacts.findIndex((c) => c.id === selected.id) : -1
      const next = visibleContacts[idx + 1]
      if (next) setSelected(next)
    },
    'arrowdown': () => {
      const idx = selected ? visibleContacts.findIndex((c) => c.id === selected.id) : -1
      const next = visibleContacts[idx + 1]
      if (next) setSelected(next)
    },
    'k': () => {
      const idx = selected ? visibleContacts.findIndex((c) => c.id === selected.id) : -1
      const prev = visibleContacts[idx - 1]
      if (prev) setSelected(prev)
    },
    'arrowup': () => {
      const idx = selected ? visibleContacts.findIndex((c) => c.id === selected.id) : -1
      const prev = visibleContacts[idx - 1]
      if (prev) setSelected(prev)
    },
    '/': () => searchInputRef.current?.focus(),
  }), [selected, visibleContacts])

  useKeyboardShortcuts(contactShortcuts)

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
              ref={searchInputRef}
              type="text"
              placeholder="Search contacts..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full rounded-lg border border-slate-600 bg-slate-700 pl-8 pr-3 py-1.5 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
            />
          </div>
        </div>

        {/* Status filter tabs */}
        <div className="border-b border-slate-700/50 px-3 py-2 flex flex-wrap gap-1">
          <button
            onClick={() => setImportantOnly((v) => !v)}
            className={`rounded-full px-2 py-0.5 text-[10px] font-medium transition-colors ${
              importantOnly
                ? 'bg-amber-500/20 text-amber-400'
                : 'text-slate-500 hover:text-slate-300'
            }`}
          >
            ★ Important
          </button>
          <span className="border-r border-slate-700 mx-0.5" />
          {(['all', 'needs_reply', 'active', 'nurturing', 'stalled', 'cold'] as const).map((s) => (
            <button
              key={s}
              onClick={() => setStatusFilter(s)}
              className={`rounded-full px-2 py-0.5 text-[10px] font-medium transition-colors ${
                statusFilter === s
                  ? s === 'all' ? 'bg-indigo-500/20 text-indigo-400' : STATUS_COLORS[s]
                  : 'text-slate-500 hover:text-slate-300'
              }`}
            >
              {s === 'all' ? 'All' : STATUS_LABELS[s]}
            </button>
          ))}
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
            visibleContacts
              .map((c) => (
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
            <div className="mb-6">
              <PersonCard
                telegramId={selected.telegram_id}
                name={selected.name}
                username={selected.username}
                isImportant={selected.is_important}
                contactId={selected.id}
                position={
                  selected.positions.filter((p) => p.is_active !== false).length > 0
                    ? { title: selected.positions.filter((p) => p.is_active !== false)[0].title, organization: selected.positions.filter((p) => p.is_active !== false)[0].organization }
                    : null
                }
                phone={selected.phone}
                twitterHandle={selected.twitter_handle}
                linkedinUrl={selected.linkedin_url}
                hasNotes={!!selected.notes}
                mode="full"
                showStatus={false}
              />
              <div className="flex items-center gap-2 mt-2">
                <button
                  onClick={handleToggleImportant}
                  title={selected.is_important ? 'Remove from important' : 'Mark as important'}
                  className={`rounded-full p-0.5 transition-colors ${
                    selected.is_important
                      ? 'text-amber-400 hover:text-amber-300'
                      : 'text-slate-600 hover:text-amber-400'
                  }`}
                >
                  <Star size={15} className={selected.is_important ? 'fill-amber-400' : ''} />
                </button>
                <Link
                  to={`/profile/${selected.telegram_id}`}
                  className="inline-flex items-center gap-1 text-[10px] text-slate-500 hover:text-slate-300 transition-colors"
                >
                  <ExternalLink size={9} />
                  View Telegram profile
                </Link>
              </div>
            </div>

            {/* Relationship status */}
            <div className="mb-6 flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-slate-400 uppercase tracking-wider">Relationship Status</span>
                {selected.relationship_status_manual && (
                  <button
                    onClick={async () => {
                      const updated = await resetRelationshipStatus(selected.id)
                      handleContactChange(updated)
                    }}
                    title="Reset to auto-calculated"
                    className="flex items-center gap-1 text-[10px] text-slate-500 hover:text-slate-300 transition-colors"
                  >
                    <RotateCcw size={10} />
                    Auto
                  </button>
                )}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {(['new', 'active', 'nurturing', 'stalled', 'cold', 'needs_reply'] as const).map((s) => (
                  <button
                    key={s}
                    onClick={async () => {
                      const updated = await setRelationshipStatus(selected.id, s)
                      handleContactChange(updated)
                    }}
                    className={`rounded-full px-2.5 py-1 text-xs font-medium transition-colors ${
                      selected.relationship_status === s
                        ? STATUS_COLORS[s]
                        : 'text-slate-600 hover:text-slate-400 border border-slate-700 hover:border-slate-600'
                    }`}
                  >
                    {STATUS_LABELS[s]}
                  </button>
                ))}
              </div>
              {selected.last_interaction_at && (
                <p className="text-xs text-slate-500">
                  Last interaction: {fmtRelative(selected.last_interaction_at)}
                </p>
              )}
            </div>

            <ContactInfoSection contact={selected} onContactChange={handleContactChange} />
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
