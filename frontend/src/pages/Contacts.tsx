import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { RefreshCw, Search, ExternalLink, Star } from 'lucide-react'
import type { Contact } from '../api/types'
import { getContacts, syncContacts, setContactImportantByTelegram } from '../api/client'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import ContactInfoSection from '../components/ContactInfoSection'

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
  const [contacts, setContacts] = useState<Contact[]>([])
  const [selected, setSelected] = useState<Contact | null>(null)
  const [search, setSearch] = useState('')
  const [syncing, setSyncing] = useState(false)
  const [loading, setLoading] = useState(true)
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

  // Keyboard shortcuts
  const contactShortcuts = useMemo(() => ({
    'j': () => {
      const idx = selected ? contacts.findIndex((c) => c.id === selected.id) : -1
      const next = contacts[idx + 1]
      if (next) setSelected(next)
    },
    'arrowdown': () => {
      const idx = selected ? contacts.findIndex((c) => c.id === selected.id) : -1
      const next = contacts[idx + 1]
      if (next) setSelected(next)
    },
    'k': () => {
      const idx = selected ? contacts.findIndex((c) => c.id === selected.id) : -1
      const prev = contacts[idx - 1]
      if (prev) setSelected(prev)
    },
    'arrowup': () => {
      const idx = selected ? contacts.findIndex((c) => c.id === selected.id) : -1
      const prev = contacts[idx - 1]
      if (prev) setSelected(prev)
    },
    '/': () => searchInputRef.current?.focus(),
  }), [selected, contacts])

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
                <div className="flex items-center gap-2">
                  <Link
                    to={`/profile/${selected.telegram_id}`}
                    className="text-lg font-bold text-gray-100 hover:text-indigo-300 transition-colors"
                  >
                    {selected.name}
                  </Link>
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
                </div>
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
