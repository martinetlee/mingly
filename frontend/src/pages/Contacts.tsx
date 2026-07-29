import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { RefreshCw, Search, ExternalLink, Star, RotateCcw, Info, X } from 'lucide-react'
import type { Contact, Message, RelationshipStatus } from '../api/types'
import { getContacts, syncContacts, setContactImportantByTelegram, setRelationshipStatus, resetRelationshipStatus, getMessages, sendMessage, reactToMessage, syncChat } from '../api/client'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import ContactInfoSection from '../components/ContactInfoSection'
import PersonCard, { RELATIONSHIP_STATUS_COLORS, RELATIONSHIP_STATUS_LABELS, fmtRelative } from '../components/PersonCard'
import MessageList from '../components/MessageList'
import ComposeBar from '../components/ComposeBar'

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
    <button
      type="button"
      onClick={onClick}
      aria-current={selected ? 'true' : undefined}
      className={`flex w-full cursor-pointer items-center gap-3 px-4 py-3 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-inset ${
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
            <Star size={11} className="flex-shrink-0 fill-amber-400 text-amber-400" aria-label="Important" />
          )}
          {contact.relationship_status && (
            <span className={`flex-shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-medium ${RELATIONSHIP_STATUS_COLORS[contact.relationship_status] ?? ''}`}>
              {RELATIONSHIP_STATUS_LABELS[contact.relationship_status] ?? contact.relationship_status}
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
    </button>
  )
}

const MSG_LIMIT = 100

export default function Contacts() {
  const [contacts, setContacts] = useState<Contact[]>([])
  const [selected, setSelected] = useState<Contact | null>(null)
  const [search, setSearch] = useState('')
  const [syncing, setSyncing] = useState(false)
  const [loading, setLoading] = useState(true)
  const [statusFilter, setStatusFilter] = useState<RelationshipStatus | 'all'>('all')
  const [sidebarWidth, setSidebarWidth] = useState(320)
  const dragging = useRef(false)
  const searchInputRef = useRef<HTMLInputElement>(null)

  // Chat message state
  const [messages, setMessages] = useState<Message[]>([])
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [messageOffset, setMessageOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [replyToMessage, setReplyToMessage] = useState<Message | null>(null)
  const [syncingChat, setSyncingChat] = useState(false)

  // Info panel state
  const [showInfo, setShowInfo] = useState(false)

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

  // Monotonic guard: rapid contact switches must not let an older response
  // land in the newer contact's message pane
  const loadSeqRef = useRef(0)

  // Load messages for selected contact
  const loadMessages = useCallback(async (contact: Contact) => {
    const seq = ++loadSeqRef.current
    setLoadingMessages(true)
    setMessageOffset(0)
    try {
      const data = await getMessages(contact.telegram_id, MSG_LIMIT, 0)
      if (seq !== loadSeqRef.current) return
      setMessages(data)
      setHasMore(data.length === MSG_LIMIT)
    } finally {
      if (seq === loadSeqRef.current) setLoadingMessages(false)
    }
  }, [])

  const loadMoreMessages = useCallback(async () => {
    if (!selected || loadingMore) return
    const seq = loadSeqRef.current
    setLoadingMore(true)
    const nextOffset = messageOffset + MSG_LIMIT
    try {
      const data = await getMessages(selected.telegram_id, MSG_LIMIT, nextOffset)
      if (seq !== loadSeqRef.current) return
      setMessages((prev) => [...data, ...prev])
      setMessageOffset(nextOffset)
      setHasMore(data.length === MSG_LIMIT)
    } finally {
      setLoadingMore(false)
    }
  }, [selected, messageOffset, loadingMore])

  const handleSelect = useCallback((c: Contact) => {
    if (selected?.id === c.id) return
    setSelected(c)
    setMessages([])
    setHasMore(false)
    setMessageOffset(0)
    setReplyToMessage(null)
  }, [selected])

  // Load messages when contact selection changes
  useEffect(() => {
    if (selected) {
      loadMessages(selected)
    }
  }, [selected, loadMessages])

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

  const handleSend = async (text: string) => {
    if (!selected) return
    await sendMessage(selected.telegram_id, text, replyToMessage?.tg_message_id)
    setReplyToMessage(null)
    await loadMessages(selected)
  }

  const handleReact = async (msg: Message, emoji: string | null) => {
    if (!selected) return
    try {
      await reactToMessage(selected.telegram_id, msg.tg_message_id, emoji)
      setMessages((prev) =>
        prev.map((m) => m.tg_message_id === msg.tg_message_id ? { ...m, my_reaction: emoji } : m)
      )
    } catch {
      // silently ignore
    }
  }

  const handleSyncChat = async () => {
    if (!selected) return
    setSyncingChat(true)
    try {
      await syncChat(selected.telegram_id)
      await loadMessages(selected)
    } finally {
      setSyncingChat(false)
    }
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
    contacts.filter((c) => statusFilter === 'all' || c.relationship_status === statusFilter),
    [contacts, statusFilter]
  )

  // Keyboard shortcuts
  const contactShortcuts = useMemo(() => ({
    'j': () => {
      const idx = selected ? visibleContacts.findIndex((c) => c.id === selected.id) : -1
      const next = visibleContacts[idx + 1]
      if (next) handleSelect(next)
    },
    'arrowdown': () => {
      const idx = selected ? visibleContacts.findIndex((c) => c.id === selected.id) : -1
      const next = visibleContacts[idx + 1]
      if (next) handleSelect(next)
    },
    'k': () => {
      const idx = selected ? visibleContacts.findIndex((c) => c.id === selected.id) : -1
      const prev = visibleContacts[idx - 1]
      if (prev) handleSelect(prev)
    },
    'arrowup': () => {
      const idx = selected ? visibleContacts.findIndex((c) => c.id === selected.id) : -1
      const prev = visibleContacts[idx - 1]
      if (prev) handleSelect(prev)
    },
    '/': () => searchInputRef.current?.focus(),
    'i': () => setShowInfo((v) => !v),
  }), [selected, visibleContacts, handleSelect])

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
          {(['all', 'needs_reply', 'active', 'nurturing', 'stalled', 'cold'] as const).map((s) => (
            <button
              key={s}
              onClick={() => setStatusFilter(s)}
              className={`rounded-full px-2 py-0.5 text-[10px] font-medium transition-colors ${
                statusFilter === s
                  ? s === 'all' ? 'bg-indigo-500/20 text-indigo-400' : RELATIONSHIP_STATUS_COLORS[s]
                  : 'text-slate-500 hover:text-slate-300'
              }`}
            >
              {s === 'all' ? 'All' : RELATIONSHIP_STATUS_LABELS[s]}
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

      {/* Chat area + optional info panel */}
      <div className="flex flex-1 overflow-hidden">
        <div className="flex flex-1 flex-col overflow-hidden bg-slate-900">
          {selected ? (
            <>
              {/* Header -- unified PersonCard */}
              <div className="flex items-center justify-between border-b border-slate-700/50 bg-slate-800 px-4 py-3">
                <PersonCard
                  telegramId={selected.telegram_id}
                  name={selected.name}
                  username={selected.username}
                  isImportant={selected.is_important}
                  status={selected.relationship_status}
                  mode="header"
                  showStatus={false}
                  disableLink
                />
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => setShowInfo((v) => !v)}
                    title="Contact info"
                    className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs transition-colors ${
                      showInfo ? 'bg-indigo-500/20 text-indigo-400' : 'text-slate-400 hover:bg-slate-700'
                    }`}
                  >
                    <Info size={13} />
                    Info
                  </button>
                  <button
                    onClick={handleSyncChat}
                    disabled={syncingChat}
                    title="Sync messages"
                    className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50"
                  >
                    <RefreshCw size={13} className={syncingChat ? 'animate-spin' : ''} />
                    Sync
                  </button>
                </div>
              </div>

              {/* Messages */}
              {loadingMessages ? (
                <div className="flex flex-1 items-center justify-center text-sm text-slate-500">
                  Loading...
                </div>
              ) : messages.length === 0 ? (
                <div className="flex flex-1 flex-col items-center justify-center text-slate-500">
                  <p className="text-sm">No messages yet</p>
                  <p className="text-xs text-slate-600 mt-1">Send a message or sync to load chat history</p>
                </div>
              ) : (
                <div className="flex flex-1 flex-col overflow-hidden">
                  {hasMore && (
                    <div className="flex justify-center border-b border-slate-700/50 py-2">
                      <button
                        onClick={loadMoreMessages}
                        disabled={loadingMore}
                        className="text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-50"
                      >
                        {loadingMore ? 'Loading...' : 'Load older messages'}
                      </button>
                    </div>
                  )}
                  <MessageList messages={messages} chatType="private" highlightTelegramId={null} onReply={setReplyToMessage} onReact={handleReact} />
                </div>
              )}

              {/* Compose */}
              <ComposeBar onSend={handleSend} replyTo={replyToMessage} onCancelReply={() => setReplyToMessage(null)} />
            </>
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center text-slate-500">
              <p className="text-sm">Select a contact to view chat</p>
            </div>
          )}
        </div>

        {/* Contact info panel */}
        {showInfo && selected && (
          <div className="flex w-80 flex-shrink-0 flex-col border-l border-slate-700/50 bg-slate-800 overflow-y-auto">
            <div className="flex items-center justify-between border-b border-slate-700/50 px-4 py-3">
              <h3 className="text-sm font-semibold text-gray-100">Contact Info</h3>
              <button onClick={() => setShowInfo(false)} className="text-slate-500 hover:text-slate-300 transition-colors">
                <X size={14} />
              </button>
            </div>
            <div className="p-4 flex flex-col gap-4">
              {/* Identity -- unified PersonCard */}
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
              <div className="flex items-center gap-2">
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

              {/* Relationship status */}
              <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-slate-400 uppercase tracking-wider">Status</span>
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
                          ? RELATIONSHIP_STATUS_COLORS[s]
                          : 'text-slate-600 hover:text-slate-400 border border-slate-700 hover:border-slate-600'
                      }`}
                    >
                      {RELATIONSHIP_STATUS_LABELS[s]}
                    </button>
                  ))}
                </div>
                {selected.status_source && (
                  <p className="text-[10px] text-slate-600">
                    Source: {selected.status_source === 'manual' ? 'Manually set' : 'Auto-calculated'}
                  </p>
                )}
                {selected.status_explanation && (
                  <div className="rounded-lg bg-slate-700/30 px-3 py-2 space-y-1">
                    <p className="text-xs text-slate-400">{selected.status_explanation}</p>
                    {selected.recommended_action && (
                      <p className="text-xs text-indigo-400/80">{selected.recommended_action}</p>
                    )}
                  </div>
                )}
                {selected.last_interaction_at && (
                  <p className="text-xs text-slate-500">
                    Last interaction: {fmtRelative(selected.last_interaction_at)}
                  </p>
                )}
              </div>

              <ContactInfoSection contact={selected} onContactChange={handleContactChange} compact />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
