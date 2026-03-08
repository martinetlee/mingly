import { useEffect, useState, useCallback } from 'react'
import { RefreshCw, FileText, X } from 'lucide-react'
import type { Chat, Message } from '../api/types'
import { getBriefingChats, getMessages, setBriefing, syncChat } from '../api/client'
import MessageList from '../components/MessageList'
import ComposeBar from '../components/ComposeBar'
import { sendMessage } from '../api/client'

const LIMIT = 100

export default function Briefing() {
  const [chats, setChats] = useState<Chat[]>([])
  const [selectedChat, setSelectedChat] = useState<Chat | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [messageOffset, setMessageOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [search, setSearch] = useState('')

  useEffect(() => {
    getBriefingChats().then(setChats)
  }, [])

  const loadMessages = useCallback(async (chat: Chat) => {
    setLoadingMessages(true)
    setMessageOffset(0)
    try {
      const data = await getMessages(chat.telegram_id, LIMIT, 0)
      setMessages(data)
      setHasMore(data.length === LIMIT)
    } finally {
      setLoadingMessages(false)
    }
  }, [])

  const loadMoreMessages = useCallback(async () => {
    if (!selectedChat || loadingMore) return
    setLoadingMore(true)
    const nextOffset = messageOffset + LIMIT
    try {
      const data = await getMessages(selectedChat.telegram_id, LIMIT, nextOffset)
      setMessages((prev) => [...data, ...prev])
      setMessageOffset(nextOffset)
      setHasMore(data.length === LIMIT)
    } finally {
      setLoadingMore(false)
    }
  }, [selectedChat, messageOffset, loadingMore])

  const handleSelectChat = (chat: Chat) => {
    setSelectedChat(chat)
    setMessages([])
    setHasMore(false)
    setMessageOffset(0)
    loadMessages(chat)
  }

  const handleSync = async () => {
    if (!selectedChat) return
    setSyncing(true)
    try {
      await syncChat(selectedChat.telegram_id)
      await loadMessages(selectedChat)
    } finally {
      setSyncing(false)
    }
  }

  const handleRemoveBriefing = async (chat: Chat) => {
    await setBriefing(chat.telegram_id, false)
    setChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
    if (selectedChat?.telegram_id === chat.telegram_id) {
      setSelectedChat(null)
      setMessages([])
    }
  }

  const handleSend = async (text: string) => {
    if (!selectedChat) return
    await sendMessage(selectedChat.telegram_id, text)
    await loadMessages(selectedChat)
  }

  const filtered = chats.filter((c) =>
    c.name.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Chat list */}
      <div className="flex w-72 flex-shrink-0 flex-col border-r border-slate-700/50 bg-slate-800">
        <div className="flex items-center gap-2 border-b border-slate-700/50 px-3 py-3">
          <FileText size={16} className="text-purple-500 flex-shrink-0" />
          <span className="text-sm font-semibold text-gray-200">Briefing</span>
        </div>
        <div className="border-b border-slate-700/50 px-3 py-2">
          <input
            type="text"
            placeholder="Search…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-gray-200 placeholder:text-slate-500 outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
          />
        </div>
        <div className="flex-1 overflow-y-auto">
          {filtered.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-slate-500">No briefing chats</p>
          ) : (
            filtered.map((chat) => (
              <div
                key={chat.telegram_id}
                onClick={() => handleSelectChat(chat)}
                className={`group flex cursor-pointer items-center gap-3 px-3 py-2.5 transition-colors ${
                  selectedChat?.telegram_id === chat.telegram_id ? 'bg-purple-500/15' : 'hover:bg-slate-700/50'
                }`}
              >
                <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-purple-500/20 text-sm font-medium text-purple-400">
                  {chat.name.charAt(0).toUpperCase()}
                </div>
                <div className="min-w-0 flex-1">
                  <p className={`truncate text-sm font-medium ${selectedChat?.telegram_id === chat.telegram_id ? 'text-purple-300' : 'text-gray-100'}`}>
                    {chat.name}
                  </p>
                  <span className="text-[10px] text-slate-500">{chat.type}</span>
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); handleRemoveBriefing(chat) }}
                  title="Remove from briefing"
                  className="hidden group-hover:flex rounded p-1 text-slate-500 hover:text-slate-300 transition-colors"
                >
                  <X size={14} />
                </button>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Message area */}
      <div className="flex flex-1 flex-col overflow-hidden bg-slate-900">
        {selectedChat ? (
          <>
            <div className="flex items-center justify-between border-b border-slate-700/50 bg-slate-800 px-4 py-3">
              <div>
                <h2 className="text-sm font-semibold text-gray-100">{selectedChat.name}</h2>
                <p className="text-xs text-slate-500">{selectedChat.type}</p>
              </div>
              <button
                onClick={handleSync}
                disabled={syncing}
                className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50"
              >
                <RefreshCw size={13} className={syncing ? 'animate-spin' : ''} />
                Sync
              </button>
            </div>

            {loadingMessages ? (
              <div className="flex flex-1 items-center justify-center text-sm text-slate-500">Loading…</div>
            ) : (
              <div className="flex flex-1 flex-col overflow-hidden">
                {hasMore && (
                  <div className="flex justify-center border-b border-slate-700/50 py-2">
                    <button
                      onClick={loadMoreMessages}
                      disabled={loadingMore}
                      className="text-xs text-purple-400 hover:text-purple-300 disabled:opacity-50"
                    >
                      {loadingMore ? 'Loading…' : 'Load older messages'}
                    </button>
                  </div>
                )}
                <MessageList messages={messages} chatType={selectedChat.type} />
              </div>
            )}
            <ComposeBar onSend={handleSend} />
          </>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center text-slate-400">
            <FileText size={32} className="mb-2 text-purple-400/40" />
            <p className="text-sm">Select a chat to view briefing</p>
          </div>
        )}
      </div>
    </div>
  )
}
