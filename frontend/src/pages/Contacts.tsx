import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { RefreshCw, Search, ExternalLink, Star, RotateCcw, Info, X, Loader2 } from 'lucide-react'
import type { Contact, Message, RelationshipStatus } from '../api/types'
import { syncContacts, setContactImportantByTelegram, setRelationshipStatus, resetRelationshipStatus, sendMessage, reactToMessage, syncChat } from '../api/client'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import { useDebouncedValue } from '../hooks/useDebouncedQuery'
import { usePagedContacts } from '../hooks/usePagedContacts'
import { useChatMessages } from '../hooks/useChatMessages'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { invalidatePerson, invalidateAllPeople } from '../hooks/usePersonSummary'
import { reportError, errorMessage, toast } from '../lib/toast'
import ContactInfoSection from '../components/ContactInfoSection'
import PersonCard, { RELATIONSHIP_STATUS_COLORS, RELATIONSHIP_STATUS_LABELS, fmtRelative } from '../components/PersonCard'
import ChatMessagesView from '../components/ChatMessagesView'
import { applyReaction } from './chats/ChatPane'
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

const STATUS_TABS = ['all', 'important', 'needs_reply', 'active', 'nurturing', 'stalled', 'cold'] as const
type ContactFilter = typeof STATUS_TABS[number]

/** `?filter=important|needs_reply|…` (also legacy `?status=`); anything else = all */
export function parseContactFilter(params: URLSearchParams): ContactFilter {
  const v = params.get('filter') ?? params.get('status') ?? 'all'
  return (STATUS_TABS as readonly string[]).includes(v) ? (v as ContactFilter) : 'all'
}

export default function Contacts() {
  const [selected, setSelected] = useState<Contact | null>(null)
  const [search, setSearch] = useState('')
  const debouncedSearch = useDebouncedValue(search.trim(), 200)
  // The filter lives in the URL so links (Home → ?filter=important), back/forward
  // and the tabs all go through the same state
  const [searchParams, setSearchParams] = useSearchParams()
  const statusFilter = parseContactFilter(searchParams)
  const setStatusFilter = useCallback((f: ContactFilter) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete('status')
      if (f === 'all') next.delete('filter'); else next.set('filter', f)
      return next
    }, { replace: true })
  }, [setSearchParams])
  const importantOnly = statusFilter === 'important'
  const [sidebarWidth, setSidebarWidth] = useState(320)
  const dragging = useRef(false)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const listEndRef = useRef<HTMLDivElement>(null)

  // Filtered + paged on the server; tab badges come from status_counts
  const list = usePagedContacts(debouncedSearch, importantOnly ? 'all' : statusFilter, 100, importantOnly)
  const { items: contacts, loading, hasMore: hasMoreContacts, loadMore: loadMoreContacts } = list

  // Message pane, bound to the selected contact's chat
  const selectedTid = selected?.telegram_id ?? null
  // Same message-window engine as the Chats pane (paging, backfill, live WS updates)
  const pane = useChatMessages({ chatId: selectedTid, topicId: null, nonce: 0 })
  // Reply target remembers its chat so it can't leak into another one
  const [replyTo, setReplyTo] = useState<{ chatId: number; msg: Message } | null>(null)
  const replyToMessage = replyTo && replyTo.chatId === selectedTid ? replyTo.msg : null
  const [syncingChat, setSyncingChat] = useState<number | null>(null)

  // Info panel state
  const [showInfo, setShowInfo] = useState(false)

  const selectedRef = useRef(selected)
  selectedRef.current = selected

  // Infinite scroll: load the next page when the end of the list is visible
  useEffect(() => {
    const el = listEndRef.current
    if (!el || !hasMoreContacts || typeof IntersectionObserver === 'undefined') return
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) loadMoreContacts()
    })
    io.observe(el)
    return () => io.disconnect()
  }, [hasMoreContacts, loadMoreContacts, contacts.length])

  const syncAll = useAsyncAction(async () => {
    const r = await syncContacts()
    invalidateAllPeople()
    await list.reload()
    toast.success(`Synced ${r.total} contact${r.total !== 1 ? 's' : ''} (${r.imported} new)`)
  }, { errorContext: 'Contact sync failed' })

  const handleSelect = useCallback((c: Contact) => {
    if (selectedRef.current?.id === c.id) return
    setSelected(c)
    setReplyTo(null)
  }, [])

  /** Adopt an updated contact everywhere — if it's still the selected one. */
  const handleContactChange = useCallback((updated: Contact) => {
    invalidatePerson(updated.telegram_id)
    setSelected((cur) => (cur && cur.id === updated.id ? updated : cur))
    list.updateItem(updated)
  }, [list])

  const toggleImportant = useAsyncAction(async (c: Contact) => {
    handleContactChange(await setContactImportantByTelegram(c.telegram_id, !c.is_important))
  }, { errorContext: 'Couldn\'t update important' })

  const changeStatus = useAsyncAction(async (contactId: number, status: RelationshipStatus | 'auto') => {
    const updated = status === 'auto'
      ? await resetRelationshipStatus(contactId)
      : await setRelationshipStatus(contactId, status)
    handleContactChange(updated)
  }, { errorContext: 'Couldn\'t change status' })

  const handleSend = async (text: string) => {
    const c = selectedRef.current
    if (!c) return
    const replyId = replyTo && replyTo.chatId === c.telegram_id ? replyTo.msg.tg_message_id : undefined
    const res = await sendMessage(c.telegram_id, text, replyId)
    setReplyTo((r) => (r && r.chatId === c.telegram_id ? null : r))
    // applyMessage ignores a message whose chat is no longer the open one
    if (res?.message) pane.applyMessage(res.message)
    else if (selectedRef.current?.telegram_id === c.telegram_id) await pane.refresh()
  }

  const handleReact = async (msg: Message, emoji: string | null) => {
    const c = selectedRef.current
    if (!c) return
    try {
      await reactToMessage(c.telegram_id, msg.tg_message_id, emoji)
      // tg_message_id is only unique per chat: patch only if it's still open
      if (selectedRef.current?.telegram_id === c.telegram_id) pane.patchMessage(msg.tg_message_id, (m) => applyReaction(m, emoji))
    } catch (e) {
      reportError(e, 'Reaction failed')
    }
  }

  const handleSyncChat = async () => {
    const c = selectedRef.current
    if (!c || syncingChat === c.telegram_id) return
    setSyncingChat(c.telegram_id)
    try {
      await syncChat(c.telegram_id)
      if (selectedRef.current?.telegram_id === c.telegram_id) await pane.refresh()
    } catch (e) {
      reportError(e, 'Sync failed')
    } finally {
      setSyncingChat((v) => (v === c.telegram_id ? null : v))
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

  // Server filters by status and importance
  const visibleContacts = contacts
  const totalAll = useMemo(() => Object.values(list.statusCounts).reduce((a, b) => a + b, 0), [list.statusCounts])

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
            onClick={() => syncAll.run()}
            disabled={syncAll.pending}
            title="Sync contacts from Telegram"
            className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50 transition-colors"
          >
            <RefreshCw size={13} className={syncAll.pending ? 'animate-spin' : ''} />
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
          {STATUS_TABS.map((s) => {
            // In the Important view the response's counts describe that subset only
            // important_count counts `search` alone; in the Important view the
            // status counts describe that subset only, so hide them there
            const count = s === 'important'
              ? list.importantCount
              : importantOnly ? null : s === 'all' ? totalAll : list.statusCounts[s] ?? 0
            return (
            <button
              key={s}
              onClick={() => setStatusFilter(s)}
              aria-pressed={statusFilter === s}
              className={`rounded-full px-2 py-0.5 text-[10px] font-medium transition-colors ${
                statusFilter === s
                  ? s === 'all' ? 'bg-indigo-500/20 text-indigo-400'
                    : s === 'important' ? 'bg-amber-500/20 text-amber-400' : RELATIONSHIP_STATUS_COLORS[s]
                  : 'text-slate-500 hover:text-slate-300'
              }`}
            >
              {s === 'all' ? 'All' : s === 'important' ? 'Important' : RELATIONSHIP_STATUS_LABELS[s]}
              {!loading && count != null && <span className="ml-1 opacity-70">{count}</span>}
            </button>
            )
          })}
        </div>

        {/* List */}
        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <p className="px-4 py-6 text-center text-sm text-slate-500">Loading...</p>
          ) : list.error != null && contacts.length === 0 ? (
            <div className="px-4 py-6 text-center">
              <p className="text-sm text-rose-400">Couldn't load contacts: {errorMessage(list.error)}</p>
              <button onClick={() => list.reload()} className="mt-2 text-xs text-indigo-400 hover:text-indigo-300">Retry</button>
            </div>
          ) : contacts.length === 0 ? (
            <div className="px-4 py-6 text-center">
              <p className="text-sm text-slate-500">No contacts found</p>
              <button
                onClick={() => syncAll.run()}
                disabled={syncAll.pending}
                className="mt-2 text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
              >
                Sync from Telegram
              </button>
            </div>
          ) : (
            <>
              {visibleContacts.map((c) => (
                <ContactCard
                  key={c.id}
                  contact={c}
                  selected={selected?.id === c.id}
                  onClick={() => handleSelect(c)}
                />
              ))}
              {hasMoreContacts && (
                <div ref={listEndRef} className="flex justify-center py-3">
                  <button
                    onClick={() => loadMoreContacts()}
                    disabled={list.loadingMore}
                    className="flex items-center gap-1.5 text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-50"
                  >
                    {list.loadingMore && <Loader2 size={12} className="animate-spin" />}
                    Load more ({contacts.length} of {list.total})
                  </button>
                </div>
              )}
            </>
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
                    disabled={syncingChat === selected.telegram_id}
                    title="Sync messages"
                    className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50"
                  >
                    <RefreshCw size={13} className={syncingChat === selected.telegram_id ? 'animate-spin' : ''} />
                    Sync
                  </button>
                </div>
              </div>

              {/* Messages */}
              <ChatMessagesView
                key={selected.telegram_id}
                msgs={pane}
                chatType="private"
                onReply={(msg) => setReplyTo({ chatId: selected.telegram_id, msg })}
                onReact={handleReact}
              />

              {/* Compose */}
              <ComposeBar key={selected.telegram_id} onSend={handleSend} replyTo={replyToMessage} onCancelReply={() => setReplyTo(null)} />
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
                  onClick={() => toggleImportant.run(selected)}
                  disabled={toggleImportant.pending}
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
                      onClick={() => changeStatus.run(selected.id, 'auto')}
                      disabled={changeStatus.pending}
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
                      onClick={() => changeStatus.run(selected.id, s)}
                      disabled={changeStatus.pending}
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

              <ContactInfoSection key={selected.id} contact={selected} onContactChange={handleContactChange} compact />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
