import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { RefreshCw, Search, ExternalLink, Star, RotateCcw, Info, X, Loader2, MessageSquare } from 'lucide-react'
import type { Conversation, Message, Person, RelationshipStatus } from '../api/types'
import { syncPeople, setPersonImportant, setRelationshipStatus, resetRelationshipStatus, sendMessage, reactToMessage, syncConversation } from '../api/client'
import { canStartConversation, NoConversationError, openDirectConversation } from '../lib/directConversation'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import { useDebouncedValue } from '../hooks/useDebouncedQuery'
import { usePagedContacts } from '../hooks/usePagedContacts'
import { useChatMessages } from '../hooks/useChatMessages'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { invalidatePerson, invalidateAllPeople } from '../hooks/usePersonSummary'
import { reportError, errorMessage, toast } from '../lib/toast'
import { can } from '../lib/capabilities'
import { personUrl } from '../lib/routes'
import ChannelIcon from '../components/channels/ChannelIcon'
import ContactInfoSection from '../components/people/ContactInfoSection'
import PersonCard, { RELATIONSHIP_STATUS_COLORS, RELATIONSHIP_STATUS_LABELS, fmtRelative } from '../components/people/PersonCard'
import ChatMessagesView from '../components/chat/ChatMessagesView'
import { applyReaction } from './chats/ChatPane'
import ComposeBar from '../components/chat/ComposeBar'
import { LoadError } from '../components/ui'

function ContactCard({
  contact,
  selected,
  onClick,
}: {
  contact: Person
  selected: boolean
  onClick: () => void
}) {
  const active = (contact.positions ?? []).filter((p) => p.is_active !== false)
  const username = contact.identities.find((i) => i.username)?.username ?? null
  const channels = Array.from(new Set(contact.identities.map((i) => i.channel)))
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
        {(username || channels.length > 0) && (
          <p className="flex items-center gap-1 truncate text-xs text-slate-500">
            {channels.map((ch) => <ChannelIcon key={ch} channel={ch} />)}
            {username && <span title={`@${username}`} className="truncate">@{username}</span>}
          </p>
        )}
        {active.length > 0 && (
          <p title={active.map((p) => p.title ? `${p.title} @ ${p.organization}` : p.organization).join(', ')} className="truncate text-[10px] text-slate-500 mt-0.5">
            {active.map((p) => p.title ? `${p.title} @ ${p.organization}` : p.organization).join(', ')}
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
  const [selected, setSelected] = useState<Person | null>(null)
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

  // The selected person's chat: their most recent one-to-one messenger
  // conversation (email lives in Mail). Selecting never creates one — with
  // none, "Start a chat" does (lib/directConversation)
  const [direct, setDirect] = useState<{ personId: number; conversation: Conversation | null; error: string | null; missing?: boolean } | null>(null)
  const [directAttempt, setDirectAttempt] = useState(0)
  const conversation = direct && direct.personId === selected?.id ? direct.conversation : null
  const conversationId = conversation?.id ?? null
  // Same message-window engine as the Chats pane (paging, backfill, live WS updates)
  const pane = useChatMessages({ conversationId, topicId: null, nonce: 0, canBackfill: can(conversation, 'history_backfill') })
  // Reply target remembers its conversation so it can't leak into another one
  const [replyTo, setReplyTo] = useState<{ conversationId: number; msg: Message } | null>(null)
  const replyToMessage = replyTo && replyTo.conversationId === conversationId ? replyTo.msg : null
  const [syncingChat, setSyncingChat] = useState<number | null>(null)

  // Info panel state
  const [showInfo, setShowInfo] = useState(false)

  const selectedRef = useRef(selected)
  selectedRef.current = selected
  const selectedUsername = selected?.identities.find((i) => i.username)?.username ?? null
  const selectedPosition = (selected?.positions ?? []).find((p) => p.is_active !== false) ?? null

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

  // Resolve the selected person's direct conversation
  useEffect(() => {
    if (!selected) return
    const personId = selected.id
    let cancelled = false
    setDirect(null)
    openDirectConversation(personId, { canStart: false, messengersOnly: true })
      .then(
      (conv) => { if (!cancelled) setDirect({ personId, conversation: conv, error: null }) },
      (e) => {
        if (cancelled) return
        if (e instanceof NoConversationError) setDirect({ personId, conversation: null, error: null, missing: true })
        else setDirect({ personId, conversation: null, error: errorMessage(e) })
      },
    )
    return () => { cancelled = true }
    // Only re-resolve when a different person is selected (or on Retry)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id, directAttempt])

  /** Explicitly start a chat with the selected person (today: a Telegram one) */
  const startChat = useAsyncAction(async (person: Person) => {
    const conv = await openDirectConversation(person.id, { canStart: true, messengersOnly: true })
    if (selectedRef.current?.id === person.id) setDirect({ personId: person.id, conversation: conv, error: null })
  }, { errorContext: 'Couldn’t start a chat' })

  const syncAll = useAsyncAction(async () => {
    const r = await syncPeople()
    invalidateAllPeople()
    await list.reload()
    toast.success(`Synced ${r.total} contact${r.total !== 1 ? 's' : ''} (${r.imported} new)`)
  }, { errorContext: 'Contact sync failed' })

  const handleSelect = useCallback((c: Person) => {
    if (selectedRef.current?.id === c.id) return
    setSelected(c)
    setReplyTo(null)
  }, [])

  /** Adopt an updated person everywhere — if it's still the selected one. */
  const handleContactChange = useCallback((updated: Person) => {
    invalidatePerson(updated.id)
    const merge = (cur: Person) => ({ ...updated, positions: updated.positions ?? cur.positions })
    setSelected((cur) => (cur && cur.id === updated.id ? merge(cur) : cur))
    list.updateItem(selectedRef.current?.id === updated.id ? merge(selectedRef.current) : updated)
  }, [list])

  const toggleImportant = useAsyncAction(async (c: Person) => {
    handleContactChange(await setPersonImportant(c.id, !c.is_important))
  }, { errorContext: 'Couldn\'t update important' })

  const changeStatus = useAsyncAction(async (personId: number, status: RelationshipStatus | 'auto') => {
    const updated = status === 'auto'
      ? await resetRelationshipStatus(personId)
      : await setRelationshipStatus(personId, status)
    handleContactChange(updated)
  }, { errorContext: 'Couldn\'t change status' })

  const conversationRef = useRef(conversationId)
  conversationRef.current = conversationId

  const handleSend = async (text: string) => {
    const cid = conversationRef.current
    if (cid == null) return
    const replyId = replyTo && replyTo.conversationId === cid ? replyTo.msg.id : undefined
    const res = await sendMessage(cid, text, replyId)
    setReplyTo((r) => (r && r.conversationId === cid ? null : r))
    // applyMessage ignores a message whose conversation is no longer the open one
    if (res?.message) pane.applyMessage(res.message)
    else if (conversationRef.current === cid) await pane.refresh()
  }

  const handleReact = async (msg: Message, emoji: string | null) => {
    try {
      await reactToMessage(msg.id, emoji)
      // patchMessage only touches the message if it's in the open window
      pane.patchMessage(msg.id, (m) => applyReaction(m, emoji))
    } catch (e) {
      reportError(e, 'Reaction failed')
    }
  }

  const handleSyncChat = async () => {
    const cid = conversationRef.current
    if (cid == null || syncingChat === cid) return
    setSyncingChat(cid)
    try {
      await syncConversation(cid)
      if (conversationRef.current === cid) await pane.refresh()
    } catch (e) {
      reportError(e, 'Sync failed')
    } finally {
      setSyncingChat((v) => (v === cid ? null : v))
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
            title="Sync contacts from every connected account"
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
                Sync contacts
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
                  personId={selected.id}
                  name={selected.name}
                  username={selectedUsername}
                  photoUrl={selected.avatar_url ?? null}
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
                  {conversation && can(conversation, 'history_backfill') && (
                    <button
                      onClick={handleSyncChat}
                      disabled={syncingChat === conversation.id}
                      title="Sync messages"
                      className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50"
                    >
                      <RefreshCw size={13} className={syncingChat === conversation.id ? 'animate-spin' : ''} />
                      Sync
                    </button>
                  )}
                </div>
              </div>

              {conversation ? (
                <>
                  {/* Messages */}
                  <ChatMessagesView
                    key={conversation.id}
                    msgs={pane}
                    conversation={conversation}
                    onReply={can(conversation, 'reply') ? (msg) => setReplyTo({ conversationId: conversation.id, msg }) : undefined}
                    onReact={can(conversation, 'react') ? handleReact : undefined}
                  />

                  {/* Compose */}
                  {can(conversation, 'send') && (
                    <ComposeBar key={conversation.id} onSend={handleSend} replyTo={replyToMessage} onCancelReply={() => setReplyTo(null)} />
                  )}
                </>
              ) : (
                <div className="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center text-slate-500">
                  {direct?.personId !== selected.id ? (
                    <Loader2 size={16} className="animate-spin" />
                  ) : direct.missing ? (
                    <>
                      <p className="text-sm">No chat with {selected.name} yet</p>
                      {canStartConversation(selected.identities) && (
                        <button
                          type="button"
                          onClick={() => void startChat.run(selected)}
                          disabled={startChat.pending}
                          className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-400 disabled:opacity-50"
                        >
                          {startChat.pending ? <Loader2 size={12} className="animate-spin" /> : <MessageSquare size={12} />}
                          Start a Telegram chat
                        </button>
                      )}
                    </>
                  ) : (
                    <LoadError message={`Couldn’t open the chat: ${direct.error}`} onRetry={() => setDirectAttempt((n) => n + 1)} />
                  )}
                </div>
              )}
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
                personId={selected.id}
                name={selected.name}
                username={selectedUsername}
                isImportant={selected.is_important}
                position={selectedPosition ? { title: selectedPosition.title, organization: selectedPosition.organization } : null}
                phone={selected.identities.find((i) => i.phone)?.phone ?? null}
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
                  to={personUrl(selected.id)}
                  className="inline-flex items-center gap-1 text-[10px] text-slate-500 hover:text-slate-300 transition-colors"
                >
                  <ExternalLink size={9} />
                  View profile
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

              <ContactInfoSection key={selected.id} person={selected} onPersonChange={handleContactChange} compact />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
