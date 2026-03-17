import { useEffect, useState } from 'react'
import { X, Search } from 'lucide-react'
import type { Chat } from '../api/types'
import { getChats } from '../api/client'

interface Props {
  open: boolean
  onClose: () => void
  onSelect: (chat: Chat) => void
}

export default function ChatPickerDialog({ open, onClose, onSelect }: Props) {
  const [search, setSearch] = useState('')
  const [chats, setChats] = useState<Chat[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open) return
    setLoading(true)
    getChats(100, 0, 'recent')
      .then(setChats)
      .finally(() => setLoading(false))
  }, [open])

  if (!open) return null

  const filtered = chats.filter((c) =>
    c.name.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="w-full max-w-md rounded-xl border border-slate-600 bg-slate-800 shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-700 px-4 py-3">
          <h3 className="text-sm font-semibold text-gray-100">Forward to...</h3>
          <button
            onClick={onClose}
            className="rounded p-1 text-slate-400 hover:bg-slate-700 hover:text-slate-200 transition-colors"
          >
            <X size={16} />
          </button>
        </div>

        {/* Search */}
        <div className="border-b border-slate-700 px-4 py-2">
          <div className="flex items-center gap-2 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5">
            <Search size={14} className="text-slate-500" />
            <input
              autoFocus
              type="text"
              placeholder="Search chats..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="flex-1 bg-transparent text-sm text-gray-200 outline-none placeholder:text-slate-500"
            />
          </div>
        </div>

        {/* Chat list */}
        <div className="max-h-80 overflow-y-auto py-1">
          {loading ? (
            <p className="px-4 py-6 text-center text-sm text-slate-500">Loading...</p>
          ) : filtered.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-slate-500">No chats found</p>
          ) : (
            filtered.map((chat) => {
              const typeColor = {
                private: 'bg-indigo-500/20 text-indigo-400',
                group: 'bg-purple-500/20 text-purple-400',
                channel: 'bg-amber-500/20 text-amber-400',
              }[chat.type] ?? 'bg-slate-600 text-slate-400'

              return (
                <button
                  key={chat.telegram_id}
                  onClick={() => onSelect(chat)}
                  className="flex w-full items-center gap-3 px-4 py-2 text-left hover:bg-slate-700/50 transition-colors"
                >
                  <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-xs font-medium text-slate-300">
                    {chat.name.charAt(0).toUpperCase()}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <p className="truncate text-sm text-gray-200">{chat.name}</p>
                      <span className={`flex-shrink-0 rounded px-1 text-[10px] font-medium ${typeColor}`}>
                        {chat.type}
                      </span>
                    </div>
                  </div>
                </button>
              )
            })
          )}
        </div>
      </div>
    </div>
  )
}
