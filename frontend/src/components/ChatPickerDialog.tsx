import { useState } from 'react'
import { Loader2, Search } from 'lucide-react'
import type { Chat } from '../api/types'
import { getChats, searchChatsLocal } from '../api/client'
import { Modal } from './ui'
import { useLatestQuery } from '../hooks/useLatestQuery'
import { useDebouncedQuery } from '../hooks/useDebouncedQuery'
import SenderAvatar from './SenderAvatar'

interface Props {
  open: boolean
  onClose: () => void
  onSelect: (chat: Chat) => void
  title?: string
  /** An action for the picked chat is in flight: rows are disabled */
  busy?: boolean
}

const TYPE_COLOR: Record<string, string> = {
  private: 'bg-indigo-500/20 text-indigo-400',
  group: 'bg-purple-500/20 text-purple-400',
  channel: 'bg-amber-500/20 text-amber-400',
}

export default function ChatPickerDialog({ open, onClose, onSelect, title = 'Forward to…', busy }: Props) {
  return (
    <Modal open={open} onClose={busy ? () => {} : onClose} title={title} size="md" closeOnBackdrop={!busy}>
      {/* Mounted per open: the search box starts empty every time */}
      {open && <PickerBody onSelect={onSelect} busy={busy} />}
    </Modal>
  )
}

function PickerBody({ onSelect, busy }: { onSelect: (chat: Chat) => void; busy?: boolean }) {
  const [search, setSearch] = useState('')
  const recent = useLatestQuery(['picker-recent'], () => getChats(100, 0, 'recent'))
  // Beyond the 100 most recent: search the local chat store
  const found = useDebouncedQuery(search, (q) => searchChatsLocal(q).then((r) => r.local), { minLength: 1 })
  const q = search.trim().toLowerCase()
  const list: Chat[] = q
    ? (found.data ?? (recent.data ?? []).filter((c) => c.name.toLowerCase().includes(q)))
    : (recent.data ?? [])
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
              key={chat.telegram_id}
              onClick={() => onSelect(chat)}
              disabled={busy}
              className="flex w-full items-center gap-3 px-4 py-2 text-left hover:bg-slate-700/50 transition-colors disabled:opacity-50"
            >
              <SenderAvatar senderId={chat.telegram_id} senderName={chat.name} size={32} link={false} />
              <div className="min-w-0 flex-1 flex items-center gap-1.5">
                <p className="truncate text-sm text-gray-200">{chat.name}</p>
                <span className={`flex-shrink-0 rounded px-1 text-[10px] font-medium ${TYPE_COLOR[chat.type] ?? 'bg-slate-600 text-slate-400'}`}>
                  {chat.type}
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
