import { useState } from 'react'
import { X, MessageSquare, Users, Radio, Link, Search, Loader2, Plus } from 'lucide-react'
import type { Chat, Contact } from '../api/types'
import { getContacts, telegramSearch, startPrivateChat, createGroup, createChannel, joinViaInviteLink } from '../api/client'
import { Modal } from './ui'
import { useAsyncAction } from '../hooks/useAsyncAction'
import { useDebouncedQuery } from '../hooks/useDebouncedQuery'
import { errorMessage } from '../lib/toast'
import SenderAvatar from './SenderAvatar'

type Tab = 'private' | 'group' | 'channel' | 'join'

interface NewChatDialogProps {
  open: boolean
  onClose: () => void
  onChatCreated: (chat: Chat) => void
}

/** One create/join action shared by every tab: single-flight, error shown inline. */
type CreateFn = (make: () => Promise<Chat>) => void

const inputCls = 'w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-2 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30'
const primaryCls = 'mt-1 w-full rounded-lg bg-indigo-600 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2'

export default function NewChatDialog({ open, onClose, onChatCreated }: NewChatDialogProps) {
  return (
    <Modal open={open} onClose={onClose} title="New Conversation" size="lg">
      {/* Body mounts per open: every field and error starts fresh */}
      {open && <NewChatBody onChatCreated={onChatCreated} />}
    </Modal>
  )
}

function NewChatBody({ onChatCreated }: { onChatCreated: (chat: Chat) => void }) {
  const [tab, setTab] = useState<Tab>('private')
  const action = useAsyncAction((make: () => Promise<Chat>) => make())
  const create: CreateFn = (make) => {
    void action.run(make).then((chat) => { if (chat) onChatCreated(chat) })
  }
  const busy = action.pending

  const tabs: { id: Tab; label: string; icon: React.ReactNode }[] = [
    { id: 'private', label: 'Private Chat', icon: <MessageSquare size={14} /> },
    { id: 'group', label: 'New Group', icon: <Users size={14} /> },
    { id: 'channel', label: 'New Channel', icon: <Radio size={14} /> },
    { id: 'join', label: 'Join via Link', icon: <Link size={14} /> },
  ]

  return (
    <>
      <div className="flex border-b border-slate-700/50 px-5">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => { setTab(t.id); action.reset() }}
            disabled={busy}
            className={`flex items-center gap-1.5 px-3 py-2.5 text-xs font-medium transition-colors border-b-2 ${
              tab === t.id ? 'border-indigo-500 text-indigo-400' : 'border-transparent text-slate-400 hover:text-slate-200'
            }`}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>
      <div className="px-5 py-4 min-h-[280px]">
        {action.error != null && (
          <div role="alert" className="mb-3 rounded-lg bg-red-500/10 border border-red-500/20 px-3 py-2 text-xs text-red-400">
            {errorMessage(action.error)}
          </div>
        )}
        {tab === 'private' && <PrivateChatTab busy={busy} create={create} />}
        {tab === 'group' && <GroupTab busy={busy} create={create} />}
        {tab === 'channel' && <ChannelTab busy={busy} create={create} />}
        {tab === 'join' && <JoinTab busy={busy} create={create} />}
      </div>
    </>
  )
}

function PersonRow({ id, name, username, busy, onClick, trailing }: {
  id: number; name: string; username?: string | null; busy: boolean; onClick: () => void; trailing?: React.ReactNode
}) {
  return (
    <button
      disabled={busy}
      onClick={onClick}
      className="flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left hover:bg-slate-700/50 transition-colors disabled:opacity-50"
    >
      <SenderAvatar senderId={id} senderName={name} size={32} link={false} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm text-gray-200">{name}</p>
        {username && <p className="truncate text-xs text-slate-500">@{username}</p>}
      </div>
      {trailing}
    </button>
  )
}

// ---------------------------------------------------------------------------
// Private Chat Tab — search contacts or Telegram directory
// ---------------------------------------------------------------------------

function PrivateChatTab({ busy, create }: { busy: boolean; create: CreateFn }) {
  const [query, setQuery] = useState('')
  // Latest-only typeahead: an older response can never overwrite a newer one
  const results = useDebouncedQuery(query, async (q, signal) => {
    const [contacts, tg] = await Promise.all([getContacts(q, 10), telegramSearch(q, 10, signal)])
    const contactIds = new Set(contacts.map((c) => c.telegram_id))
    return {
      contacts,
      telegram: tg.telegram_dialogs.filter((r) => r.type === 'private' && !contactIds.has(r.telegram_id)),
    }
  }, { delayMs: 300 })
  const contacts = results.data?.contacts ?? []
  const telegram = results.data?.telegram ?? []
  const open = (telegramId: number) => create(() => startPrivateChat(telegramId))

  return (
    <div className="flex flex-col gap-3">
      <div className="relative">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
        <input
          autoFocus
          type="text"
          placeholder="Search contacts or Telegram users…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className={`${inputCls} pl-9`}
        />
      </div>

      <div className="max-h-52 overflow-y-auto">
        {results.pending && !results.data && (
          <div className="flex items-center justify-center py-6 text-xs text-slate-500">
            <Loader2 size={14} className="animate-spin mr-2" /> Searching…
          </div>
        )}
        {results.error != null && <p className="py-2 text-center text-xs text-red-400">Search failed: {errorMessage(results.error)}</p>}
        {!results.pending && query.trim() && results.data && contacts.length === 0 && telegram.length === 0 && (
          <p className="py-6 text-center text-xs text-slate-500">No results found</p>
        )}
        {query.trim() && contacts.length > 0 && (
          <div className="mb-2">
            <p className="mb-1 text-[10px] font-medium uppercase tracking-wider text-slate-500">Contacts</p>
            {contacts.map((c) => (
              <PersonRow key={c.telegram_id} id={c.telegram_id} name={c.name} username={c.username} busy={busy} onClick={() => open(c.telegram_id)} />
            ))}
          </div>
        )}
        {query.trim() && telegram.length > 0 && (
          <div>
            <p className="mb-1 text-[10px] font-medium uppercase tracking-wider text-slate-500">Telegram</p>
            {telegram.map((r) => (
              <PersonRow key={r.telegram_id} id={r.telegram_id} name={r.name} username={r.username} busy={busy} onClick={() => open(r.telegram_id)} />
            ))}
          </div>
        )}
        {!query.trim() && <p className="py-6 text-center text-xs text-slate-500">Type a name or username to search</p>}
      </div>

      {busy && (
        <div className="flex items-center justify-center py-2 text-xs text-slate-400">
          <Loader2 size={14} className="animate-spin mr-2" /> Opening chat…
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Group Tab — name + member picker
// ---------------------------------------------------------------------------

function GroupTab({ busy, create }: { busy: boolean; create: CreateFn }) {
  const [groupName, setGroupName] = useState('')
  const [memberQuery, setMemberQuery] = useState('')
  const [selectedMembers, setSelectedMembers] = useState<{ id: number; name: string }[]>([])
  const search = useDebouncedQuery(memberQuery, (q) => getContacts(q, 10), { delayMs: 300 })
  const results = memberQuery.trim() ? (search.data ?? []) : []

  const addMember = (contact: Contact) => {
    setSelectedMembers((prev) => prev.some((m) => m.id === contact.telegram_id) ? prev : [...prev, { id: contact.telegram_id, name: contact.name }])
    setMemberQuery('')
  }

  const canCreate = !!groupName.trim() && selectedMembers.length > 0
  return (
    <div className="flex flex-col gap-3">
      <input autoFocus type="text" placeholder="Group name" value={groupName} onChange={(e) => setGroupName(e.target.value)} className={inputCls} />

      {selectedMembers.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {selectedMembers.map((m) => (
            <span key={m.id} className="flex items-center gap-1 rounded-full bg-indigo-500/20 px-2.5 py-1 text-xs text-indigo-300">
              {m.name}
              <button onClick={() => setSelectedMembers((prev) => prev.filter((x) => x.id !== m.id))} className="text-indigo-400 hover:text-indigo-200" aria-label={`Remove ${m.name}`}>
                <X size={12} />
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="relative">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
        <input type="text" placeholder="Search contacts to add…" value={memberQuery} onChange={(e) => setMemberQuery(e.target.value)} className={`${inputCls} pl-9`} />
      </div>

      <div className="max-h-32 overflow-y-auto">
        {search.pending && !search.data && (
          <div className="flex items-center justify-center py-4 text-xs text-slate-500">
            <Loader2 size={14} className="animate-spin mr-2" /> Searching…
          </div>
        )}
        {results
          .filter((c) => !selectedMembers.some((m) => m.id === c.telegram_id))
          .map((c) => (
            <PersonRow key={c.telegram_id} id={c.telegram_id} name={c.name} busy={false} onClick={() => addMember(c)} trailing={<Plus size={14} className="text-slate-500" />} />
          ))}
      </div>

      <button
        onClick={() => create(() => createGroup(groupName.trim(), selectedMembers.map((m) => m.id)))}
        disabled={busy || !canCreate}
        className={primaryCls}
      >
        {busy ? <><Loader2 size={14} className="animate-spin" /> Creating…</> : 'Create Group'}
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Channel Tab
// ---------------------------------------------------------------------------

function ChannelTab({ busy, create }: { busy: boolean; create: CreateFn }) {
  const [channelName, setChannelName] = useState('')
  const [about, setAbout] = useState('')
  return (
    <div className="flex flex-col gap-3">
      <input autoFocus type="text" placeholder="Channel name" value={channelName} onChange={(e) => setChannelName(e.target.value)} className={inputCls} />
      <textarea placeholder="Description (optional)" value={about} onChange={(e) => setAbout(e.target.value)} rows={3} className={`${inputCls} resize-none`} />
      <button
        onClick={() => create(() => createChannel(channelName.trim(), about.trim()))}
        disabled={busy || !channelName.trim()}
        className={primaryCls}
      >
        {busy ? <><Loader2 size={14} className="animate-spin" /> Creating…</> : 'Create Channel'}
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Join via Invite Link Tab
// ---------------------------------------------------------------------------

function JoinTab({ busy, create }: { busy: boolean; create: CreateFn }) {
  const [link, setLink] = useState('')
  const join = () => { if (link.trim() && !busy) create(() => joinViaInviteLink(link.trim())) }
  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-slate-400">Paste a Telegram invite link to join a group, channel, or chat.</p>
      <input
        autoFocus
        type="text"
        placeholder="https://t.me/joinchat/… or https://t.me/+…"
        value={link}
        onChange={(e) => setLink(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') join() }}
        className={inputCls}
      />
      <p className="text-[10px] text-slate-500">Supported formats: t.me/joinchat/…, t.me/+…, t.me/username, or @username</p>
      <button onClick={join} disabled={busy || !link.trim()} className={primaryCls}>
        {busy ? <><Loader2 size={14} className="animate-spin" /> Joining…</> : 'Join'}
      </button>
    </div>
  )
}
