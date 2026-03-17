import { useState, useEffect, useRef, useCallback } from 'react'
import { X, MessageSquare, Users, Radio, Link, Search, Loader2, Check, Plus } from 'lucide-react'
import type { Chat, Contact, TelegramSearchResult } from '../api/types'
import { getContacts, telegramSearch, startPrivateChat, createGroup, createChannel, joinViaInviteLink } from '../api/client'

type Tab = 'private' | 'group' | 'channel' | 'join'

interface NewChatDialogProps {
  open: boolean
  onClose: () => void
  onChatCreated: (chat: Chat) => void
}

export default function NewChatDialog({ open, onClose, onChatCreated }: NewChatDialogProps) {
  const [tab, setTab] = useState<Tab>('private')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Reset state when dialog opens
  useEffect(() => {
    if (open) {
      setError(null)
      setLoading(false)
    }
  }, [open])

  if (!open) return null

  const tabs: { id: Tab; label: string; icon: React.ReactNode }[] = [
    { id: 'private', label: 'Private Chat', icon: <MessageSquare size={14} /> },
    { id: 'group', label: 'New Group', icon: <Users size={14} /> },
    { id: 'channel', label: 'New Channel', icon: <Radio size={14} /> },
    { id: 'join', label: 'Join via Link', icon: <Link size={14} /> },
  ]

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-xl border border-slate-700 bg-slate-800 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-700/50 px-5 py-4">
          <h2 className="text-base font-semibold text-gray-100">New Conversation</h2>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-700 hover:text-slate-200 transition-colors"
          >
            <X size={16} />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-slate-700/50 px-5">
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => { setTab(t.id); setError(null) }}
              className={`flex items-center gap-1.5 px-3 py-2.5 text-xs font-medium transition-colors border-b-2 ${
                tab === t.id
                  ? 'border-indigo-500 text-indigo-400'
                  : 'border-transparent text-slate-400 hover:text-slate-200'
              }`}
            >
              {t.icon}
              {t.label}
            </button>
          ))}
        </div>

        {/* Content */}
        <div className="px-5 py-4 min-h-[280px]">
          {error && (
            <div className="mb-3 rounded-lg bg-red-500/10 border border-red-500/20 px-3 py-2 text-xs text-red-400">
              {error}
            </div>
          )}

          {tab === 'private' && (
            <PrivateChatTab
              loading={loading}
              setLoading={setLoading}
              setError={setError}
              onChatCreated={onChatCreated}
            />
          )}
          {tab === 'group' && (
            <GroupTab
              loading={loading}
              setLoading={setLoading}
              setError={setError}
              onChatCreated={onChatCreated}
            />
          )}
          {tab === 'channel' && (
            <ChannelTab
              loading={loading}
              setLoading={setLoading}
              setError={setError}
              onChatCreated={onChatCreated}
            />
          )}
          {tab === 'join' && (
            <JoinTab
              loading={loading}
              setLoading={setLoading}
              setError={setError}
              onChatCreated={onChatCreated}
            />
          )}
        </div>
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Private Chat Tab — search contacts or Telegram directory
// ---------------------------------------------------------------------------

function PrivateChatTab({
  loading,
  setLoading,
  setError,
  onChatCreated,
}: {
  loading: boolean
  setLoading: (v: boolean) => void
  setError: (v: string | null) => void
  onChatCreated: (chat: Chat) => void
}) {
  const [query, setQuery] = useState('')
  const [contacts, setContacts] = useState<Contact[]>([])
  const [telegramResults, setTelegramResults] = useState<TelegramSearchResult[]>([])
  const [searching, setSearching] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Debounced search
  useEffect(() => {
    if (!query.trim()) {
      setContacts([])
      setTelegramResults([])
      return
    }
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(async () => {
      setSearching(true)
      try {
        const [contactResults, tgResults] = await Promise.all([
          getContacts(query.trim(), 10),
          telegramSearch(query.trim(), 10),
        ])
        setContacts(contactResults)
        setTelegramResults(tgResults.telegram_dialogs.filter((r) => r.type === 'private'))
      } catch {
        // ignore search errors
      } finally {
        setSearching(false)
      }
    }, 300)
    return () => { if (timerRef.current) clearTimeout(timerRef.current) }
  }, [query])

  const handleSelect = async (telegramId: number) => {
    setLoading(true)
    setError(null)
    try {
      const chat = await startPrivateChat(telegramId)
      onChatCreated(chat)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to start chat')
    } finally {
      setLoading(false)
    }
  }

  // Deduplicate: telegram results that already appear in contacts
  const contactTelegramIds = new Set(contacts.map((c) => c.telegram_id))
  const filteredTg = telegramResults.filter((r) => !contactTelegramIds.has(r.telegram_id))

  return (
    <div className="flex flex-col gap-3">
      <div className="relative">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
        <input
          autoFocus
          type="text"
          placeholder="Search contacts or Telegram users..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="w-full rounded-lg border border-slate-600 bg-slate-700 pl-9 pr-3 py-2 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
        />
      </div>

      <div className="max-h-52 overflow-y-auto">
        {searching && (
          <div className="flex items-center justify-center py-6 text-xs text-slate-500">
            <Loader2 size={14} className="animate-spin mr-2" /> Searching...
          </div>
        )}

        {!searching && query.trim() && contacts.length === 0 && filteredTg.length === 0 && (
          <p className="py-6 text-center text-xs text-slate-500">No results found</p>
        )}

        {contacts.length > 0 && (
          <div className="mb-2">
            <p className="mb-1 text-[10px] font-medium uppercase tracking-wider text-slate-500">Contacts</p>
            {contacts.map((c) => (
              <button
                key={c.telegram_id}
                disabled={loading}
                onClick={() => handleSelect(c.telegram_id)}
                className="flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left hover:bg-slate-700/50 transition-colors disabled:opacity-50"
              >
                <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-slate-600 text-xs font-medium text-slate-300">
                  {c.name.charAt(0).toUpperCase()}
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm text-gray-200">{c.name}</p>
                  {c.username && <p className="truncate text-xs text-slate-500">@{c.username}</p>}
                </div>
              </button>
            ))}
          </div>
        )}

        {filteredTg.length > 0 && (
          <div>
            <p className="mb-1 text-[10px] font-medium uppercase tracking-wider text-slate-500">Telegram</p>
            {filteredTg.map((r) => (
              <button
                key={r.telegram_id}
                disabled={loading}
                onClick={() => handleSelect(r.telegram_id)}
                className="flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left hover:bg-slate-700/50 transition-colors disabled:opacity-50"
              >
                <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-indigo-500/20 text-xs font-medium text-indigo-400">
                  {r.name.charAt(0).toUpperCase()}
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm text-gray-200">{r.name}</p>
                  {r.username && <p className="truncate text-xs text-slate-500">@{r.username}</p>}
                </div>
              </button>
            ))}
          </div>
        )}

        {!query.trim() && (
          <p className="py-6 text-center text-xs text-slate-500">
            Type a name or username to search
          </p>
        )}
      </div>

      {loading && (
        <div className="flex items-center justify-center py-2 text-xs text-slate-400">
          <Loader2 size={14} className="animate-spin mr-2" /> Opening chat...
        </div>
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Group Tab — name + member picker
// ---------------------------------------------------------------------------

function GroupTab({
  loading,
  setLoading,
  setError,
  onChatCreated,
}: {
  loading: boolean
  setLoading: (v: boolean) => void
  setError: (v: string | null) => void
  onChatCreated: (chat: Chat) => void
}) {
  const [groupName, setGroupName] = useState('')
  const [memberQuery, setMemberQuery] = useState('')
  const [selectedMembers, setSelectedMembers] = useState<{ id: number; name: string }[]>([])
  const [searchResults, setSearchResults] = useState<Contact[]>([])
  const [searching, setSearching] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (!memberQuery.trim()) {
      setSearchResults([])
      return
    }
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(async () => {
      setSearching(true)
      try {
        const results = await getContacts(memberQuery.trim(), 10)
        setSearchResults(results)
      } catch {
        // ignore
      } finally {
        setSearching(false)
      }
    }, 300)
    return () => { if (timerRef.current) clearTimeout(timerRef.current) }
  }, [memberQuery])

  const addMember = (contact: Contact) => {
    if (!selectedMembers.some((m) => m.id === contact.telegram_id)) {
      setSelectedMembers((prev) => [...prev, { id: contact.telegram_id, name: contact.name }])
    }
    setMemberQuery('')
    setSearchResults([])
  }

  const removeMember = (id: number) => {
    setSelectedMembers((prev) => prev.filter((m) => m.id !== id))
  }

  const handleCreate = async () => {
    if (!groupName.trim()) {
      setError('Group name is required')
      return
    }
    if (selectedMembers.length === 0) {
      setError('At least one member is required')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const chat = await createGroup(groupName.trim(), selectedMembers.map((m) => m.id))
      onChatCreated(chat)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to create group')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <input
        autoFocus
        type="text"
        placeholder="Group name"
        value={groupName}
        onChange={(e) => setGroupName(e.target.value)}
        className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
      />

      {/* Selected members */}
      {selectedMembers.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {selectedMembers.map((m) => (
            <span
              key={m.id}
              className="flex items-center gap-1 rounded-full bg-indigo-500/20 px-2.5 py-1 text-xs text-indigo-300"
            >
              {m.name}
              <button onClick={() => removeMember(m.id)} className="text-indigo-400 hover:text-indigo-200">
                <X size={12} />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Member search */}
      <div className="relative">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
        <input
          type="text"
          placeholder="Search contacts to add..."
          value={memberQuery}
          onChange={(e) => setMemberQuery(e.target.value)}
          className="w-full rounded-lg border border-slate-600 bg-slate-700 pl-9 pr-3 py-2 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
        />
      </div>

      <div className="max-h-32 overflow-y-auto">
        {searching && (
          <div className="flex items-center justify-center py-4 text-xs text-slate-500">
            <Loader2 size={14} className="animate-spin mr-2" /> Searching...
          </div>
        )}
        {searchResults
          .filter((c) => !selectedMembers.some((m) => m.id === c.telegram_id))
          .map((c) => (
            <button
              key={c.telegram_id}
              onClick={() => addMember(c)}
              className="flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left hover:bg-slate-700/50 transition-colors"
            >
              <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-slate-600 text-xs font-medium text-slate-300">
                {c.name.charAt(0).toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-gray-200">{c.name}</p>
              </div>
              <Plus size={14} className="text-slate-500" />
            </button>
          ))}
      </div>

      <button
        onClick={handleCreate}
        disabled={loading || !groupName.trim() || selectedMembers.length === 0}
        className="mt-1 w-full rounded-lg bg-indigo-600 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2"
      >
        {loading ? <><Loader2 size={14} className="animate-spin" /> Creating...</> : 'Create Group'}
      </button>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Channel Tab
// ---------------------------------------------------------------------------

function ChannelTab({
  loading,
  setLoading,
  setError,
  onChatCreated,
}: {
  loading: boolean
  setLoading: (v: boolean) => void
  setError: (v: string | null) => void
  onChatCreated: (chat: Chat) => void
}) {
  const [channelName, setChannelName] = useState('')
  const [about, setAbout] = useState('')

  const handleCreate = async () => {
    if (!channelName.trim()) {
      setError('Channel name is required')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const chat = await createChannel(channelName.trim(), about.trim())
      onChatCreated(chat)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to create channel')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <input
        autoFocus
        type="text"
        placeholder="Channel name"
        value={channelName}
        onChange={(e) => setChannelName(e.target.value)}
        className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
      />

      <textarea
        placeholder="Description (optional)"
        value={about}
        onChange={(e) => setAbout(e.target.value)}
        rows={3}
        className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30 resize-none"
      />

      <button
        onClick={handleCreate}
        disabled={loading || !channelName.trim()}
        className="mt-1 w-full rounded-lg bg-indigo-600 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2"
      >
        {loading ? <><Loader2 size={14} className="animate-spin" /> Creating...</> : 'Create Channel'}
      </button>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Join via Invite Link Tab
// ---------------------------------------------------------------------------

function JoinTab({
  loading,
  setLoading,
  setError,
  onChatCreated,
}: {
  loading: boolean
  setLoading: (v: boolean) => void
  setError: (v: string | null) => void
  onChatCreated: (chat: Chat) => void
}) {
  const [link, setLink] = useState('')

  const handleJoin = async () => {
    if (!link.trim()) {
      setError('Invite link is required')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const chat = await joinViaInviteLink(link.trim())
      onChatCreated(chat)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to join')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-slate-400">
        Paste a Telegram invite link to join a group, channel, or chat.
      </p>

      <input
        autoFocus
        type="text"
        placeholder="https://t.me/joinchat/... or https://t.me/+..."
        value={link}
        onChange={(e) => setLink(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && link.trim()) handleJoin() }}
        className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
      />

      <p className="text-[10px] text-slate-500">
        Supported formats: t.me/joinchat/..., t.me/+..., t.me/username, or @username
      </p>

      <button
        onClick={handleJoin}
        disabled={loading || !link.trim()}
        className="mt-1 w-full rounded-lg bg-indigo-600 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2"
      >
        {loading ? <><Loader2 size={14} className="animate-spin" /> Joining...</> : 'Join'}
      </button>
    </div>
  )
}
