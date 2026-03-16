import { useEffect, useState, useCallback, useRef } from 'react'
import { RefreshCw, Bookmark, X, Info, Radio } from 'lucide-react'
import type { Chat, Message, WSEvent } from '../api/types'
import { getTrackedChats, getMessages, setTracked, syncChat, sendMessage } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'
import MessageList from '../components/MessageList'
import ComposeBar from '../components/ComposeBar'
import ChatInfoPanel from '../components/ChatInfoPanel'

function fmtDate(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffDays = Math.floor(diffMs / 86400000)
  if (diffDays === 0) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (diffDays === 1) return 'Yesterday'
  if (diffDays < 7) return d.toLocaleDateString([], { weekday: 'short' })
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

const LIMIT = 100

export default function Tracked() {
  const [chats, setChats] = useState<Chat[]>([])
  const [selectedChat, setSelectedChat] = useState<Chat | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [messageOffset, setMessageOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [showInfo, setShowInfo] = useState(false)
  const [search, setSearch] = useState('')
  const [sidebarWidth, setSidebarWidth] = useState(288)
  const dragging = useRef(false)

  useEffect(() => {
    getTrackedChats().then(setChats)
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

  useEffect(() => {
    if (selectedChat) loadMessages(selectedChat)
  }, [selectedChat, loadMessages])

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

  const handleWsEvent = useCallback((event: WSEvent) => {
    if (event.type === 'new_message') {
      getTrackedChats().then(setChats)
      if (selectedChat && event.chat_id === selectedChat.telegram_id) {
        loadMessages(selectedChat)
      }
    }
  }, [selectedChat, loadMessages])

  useWebSocket(handleWsEvent)

  const handleSelectChat = (chat: Chat) => {
    setSelectedChat(chat)
    setMessages([])
    setHasMore(false)
    setMessageOffset(0)
    setShowInfo(false)
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

  const handleUntrack = async (chat: Chat) => {
    await setTracked(chat.telegram_id, false)
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

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    dragging.current = true
    const startX = e.clientX
    const startWidth = sidebarWidth
    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return
      setSidebarWidth(Math.min(480, Math.max(200, startWidth + e.clientX - startX)))
    }
    const onUp = () => {
      dragging.current = false
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [sidebarWidth])

  const filtered = chats.filter((c) =>
    c.name.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Chat list */}
      <div className="flex flex-shrink-0 flex-col border-r border-slate-700/50 bg-slate-800 relative" style={{ width: sidebarWidth }}>
        <div
          onMouseDown={onDragStart}
          className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-indigo-500 transition-colors z-10"
        />
        <div className="flex items-center gap-2 border-b border-slate-700/50 px-3 py-3">
          <Bookmark size={16} className="text-amber-500 flex-shrink-0" fill="currentColor" />
          <span className="text-sm font-semibold text-gray-200">Tracked</span>
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
            <p className="px-4 py-6 text-center text-sm text-slate-500">No tracked chats</p>
          ) : (
            filtered.map((chat) => (
              <div
                key={chat.telegram_id}
                onClick={() => handleSelectChat(chat)}
                className={`group flex cursor-pointer items-center gap-3 px-3 py-2.5 transition-colors ${
                  selectedChat?.telegram_id === chat.telegram_id ? 'bg-amber-500/15' : 'hover:bg-slate-700/50'
                }`}
              >
                <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-amber-500/20 text-sm font-medium text-amber-400">
                  {chat.name.charAt(0).toUpperCase()}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <p title={chat.name} className={`truncate text-sm font-medium ${selectedChat?.telegram_id === chat.telegram_id ? 'text-amber-300' : 'text-gray-100'}`}>
                      {chat.name}
                    </p>
                    {chat.last_message?.date && (
                      <span className="ml-auto flex-shrink-0 text-[10px] text-slate-500">
                        {fmtDate(chat.last_message.date)}
                      </span>
                    )}
                  </div>
                  {chat.last_message ? (
                    <p className="truncate text-xs text-slate-500 mt-0.5">
                      {chat.last_message.sender_name && (
                        <span className="text-slate-400">{chat.last_message.sender_name}: </span>
                      )}
                      {chat.last_message.text || '[media]'}
                    </p>
                  ) : (
                    <span className="text-[10px] text-slate-500">{chat.type}</span>
                  )}
                </div>
                {(chat.unread_count ?? 0) > 0 && (
                  <span className="min-w-[18px] rounded-full bg-indigo-500 px-1.5 py-0.5 text-center text-[10px] font-semibold leading-tight text-white group-hover:hidden">
                    {(chat.unread_count ?? 0) > 99 ? '99+' : chat.unread_count}
                  </span>
                )}
                <button
                  onClick={(e) => { e.stopPropagation(); handleUntrack(chat) }}
                  title="Untrack"
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
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setShowInfo((v) => !v)}
                  title="Chat info & tags"
                  className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs transition-colors ${
                    showInfo ? 'bg-indigo-500/20 text-indigo-400' : 'text-slate-400 hover:bg-slate-700'
                  }`}
                >
                  <Info size={13} />
                  Info
                </button>
                <button
                  onClick={handleSync}
                  disabled={syncing}
                  className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50"
                >
                  <RefreshCw size={13} className={syncing ? 'animate-spin' : ''} />
                  Sync
                </button>
              </div>
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
                      className="text-xs text-amber-500 hover:text-amber-700 disabled:opacity-50"
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
            <Bookmark size={32} className="mb-2 text-amber-200" fill="currentColor" />
            <p className="text-sm">Select a tracked chat to view</p>
          </div>
        )}
      </div>

      {showInfo && selectedChat && (
        <ChatInfoPanel chat={selectedChat} onClose={() => setShowInfo(false)} />
      )}
    </div>
  )
}
