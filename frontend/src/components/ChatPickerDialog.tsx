import { useState } from 'react'
import { Loader2, Search } from 'lucide-react'
import type { Conversation } from '../api/types'
import { getConversations, MESSENGERS_ONLY, searchConversationsLocal } from '../api/client'
import { conversationTitle, kindLabel } from '../lib/conversation'
import ChannelIcon from './ChannelIcon'
import ConversationAvatar from './ConversationAvatar'
import { Modal } from './ui'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useDebouncedQuery } from '../hooks/useDebouncedQuery'

interface Props {
  open: boolean
  onClose: () => void
  onSelect: (chat: Conversation) => void
  /** Only these conversations can be picked (e.g. same channel, can send) */
  filter?: (chat: Conversation) => boolean
  title?: string
  /** An action for the picked chat is in flight: rows are disabled */
  busy?: boolean
}

const KIND_COLOR: Record<string, string> = {
  direct: 'bg-indigo-500/20 text-indigo-400',
  group: 'bg-purple-500/20 text-purple-400',
  channel: 'bg-amber-500/20 text-amber-400',
  email_thread: 'bg-sky-500/20 text-sky-400',
}

export default function ChatPickerDialog({ open, onClose, onSelect, filter, title = 'Forward to…', busy }: Props) {
  return (
    <Modal open={open} onClose={busy ? () => {} : onClose} title={title} size="md" closeOnBackdrop={!busy}>
      {/* Mounted per open: the search box starts empty every time */}
      {open && <PickerBody onSelect={onSelect} filter={filter} busy={busy} />}
    </Modal>
  )
}

function PickerBody({ onSelect, filter, busy }: { onSelect: (chat: Conversation) => void; filter?: (chat: Conversation) => boolean; busy?: boolean }) {
  const [search, setSearch] = useState('')
  // Chat-only picker: email threads belong to Mail
  const recent = useLatestQuery(['picker-recent'], () => getConversations(100, 0, 'recent', undefined, undefined, undefined, MESSENGERS_ONLY))
  // Beyond the 100 most recent: search the local conversation store (every channel)
  const found = useDebouncedQuery(search, (q) => searchConversationsLocal(q).then((r) => r.local), { minLength: 1 })
  const q = search.trim().toLowerCase()
  const all: Conversation[] = (q
    ? (found.data ?? (recent.data ?? []).filter((c) => conversationTitle(c).toLowerCase().includes(q)))
    : (recent.data ?? [])
  ).filter((c) => c.kind !== MESSENGERS_ONLY.excludeKind)
  const list = filter ? all.filter(filter) : all
  const loading = q ? found.pending && !found.data : recent.loading

  return (
    <>
      <div className="border-b border-slate-700 px-4 py-2">
        <div className="flex items-center gap-2 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5">
          <Search size={14} className="text-slate-500" />
          <input
            autoFocus
            type="text"
            placeholder="Search chats…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="flex-1 bg-transparent text-sm text-gray-200 outline-none placeholder:text-slate-500"
          />
        </div>
      </div>
      <div className="max-h-80 overflow-y-auto py-1">
        {loading ? (
          <p className="px-4 py-6 text-center text-sm text-slate-500">Loading…</p>
        ) : recent.error && !q ? (
          <p className="px-4 py-6 text-center text-sm text-red-400">Couldn’t load chats</p>
        ) : list.length === 0 ? (
          <p className="px-4 py-6 text-center text-sm text-slate-500">No chats found</p>
        ) : (
          list.map((chat) => (
            <button
              key={chat.id}
              onClick={() => onSelect(chat)}
              disabled={busy}
              className="flex w-full items-center gap-3 px-4 py-2 text-left hover:bg-slate-700/50 transition-colors disabled:opacity-50"
            >
              <ConversationAvatar conversation={chat} size={32} />
              <div className="min-w-0 flex-1 flex items-center gap-1.5">
                <p className="truncate text-sm text-gray-200">{conversationTitle(chat)}</p>
                <ChannelIcon channel={chat.channel} />
                <span className={`flex-shrink-0 rounded px-1 text-[10px] font-medium ${KIND_COLOR[chat.kind] ?? 'bg-slate-600 text-slate-400'}`}>
                  {kindLabel(chat.kind)}
                </span>
              </div>
              {busy && <Loader2 size={14} className="animate-spin text-slate-500" />}
            </button>
          ))
        )}
      </div>
    </>
  )
}
