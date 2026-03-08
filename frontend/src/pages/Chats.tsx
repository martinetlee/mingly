import { useEffect, useState, useCallback } from 'react'
import { RefreshCw, Radio } from 'lucide-react'
import type { Chat, Message, WSEvent } from '../api/types'
import { getChats, getMessages, setMonitored, syncChat, sendMessage } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'
import MessageList from '../components/MessageList'
import ComposeBar from '../components/ComposeBar'

function ChatItem({
  chat,
  selected,
  onClick,
  onToggleMonitor,
}: {
  chat: Chat
  selected: boolean
  onClick: () => void
  onToggleMonitor: (monitored: boolean) => void
}) {
  const typeColor = {
    private: 'bg-blue-100 text-blue-600',
    group: 'bg-purple-100 text-purple-600',
    channel: 'bg-orange-100 text-orange-600',
  }[chat.type] ?? 'bg-gray-100 text-gray-600'

  return (
    <div
      onClick={onClick}
      className={`flex cursor-pointer items-center gap-3 px-3 py-2.5 transition-colors ${
        selected ? 'bg-blue-50' : 'hover:bg-gray-50'
      }`}
    >
      {/* Avatar */}
      <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-gray-200 text-sm font-medium text-gray-600">
        {chat.name.charAt(0).toUpperCase()}
      </div>

      {/* Name + type */}
      <div className="min-w-0 flex-1">
        <p className={`truncate text-sm font-medium ${selected ? 'text-blue-700' : 'text-gray-900'}`}>
          {chat.name}
        </p>
        <span className={`inline-block rounded px-1 text-[10px] font-medium ${typeColor}`}>
          {chat.type}
        </span>
      </div>

      {/* Monitor toggle */}
      <button
        onClick={(e) => { e.stopPropagation(); onToggleMonitor(!chat.is_monitored) }}
        title={chat.is_monitored ? 'Stop monitoring' : 'Monitor this chat'}
        className={`flex-shrink-0 rounded p-1 transition-colors ${
          chat.is_monitored
            ? 'text-blue-500 hover:text-blue-700'
            : 'text-gray-300 hover:text-gray-500'
        }`}
      >
        <Radio size={14} />
      </button>
    </div>
  )
}

export default function Chats() {
  const [chats, setChats] = useState<Chat[]>([])
  const [selectedChat, setSelectedChat] = useState<Chat | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [search, setSearch] = useState('')

  const loadChats = useCallback(async () => {
    const data = await getChats()
    setChats(data)
  }, [])

  const loadMessages = useCallback(async (chat: Chat) => {
    setLoadingMessages(true)
    try {
      const data = await getMessages(chat.telegram_id)
      setMessages(data)
    } finally {
      setLoadingMessages(false)
    }
  }, [])

  useEffect(() => {
    loadChats()
  }, [loadChats])

  useEffect(() => {
    if (selectedChat) {
      loadMessages(selectedChat)
    }
  }, [selectedChat, loadMessages])

  const handleWsEvent = useCallback((event: WSEvent) => {
    if (event.type === 'new_message' && selectedChat) {
      if (event.chat_id === selectedChat.telegram_id) {
        loadMessages(selectedChat)
      }
    }
  }, [selectedChat, loadMessages])

  useWebSocket(handleWsEvent)

  const handleSelectChat = (chat: Chat) => {
    setSelectedChat(chat)
    setMessages([])
  }

  const handleToggleMonitor = async (chat: Chat, monitored: boolean) => {
    await setMonitored(chat.telegram_id, monitored)
    setChats((prev) =>
      prev.map((c) => (c.telegram_id === chat.telegram_id ? { ...c, is_monitored: monitored } : c))
    )
    if (selectedChat?.telegram_id === chat.telegram_id) {
      setSelectedChat((c) => c ? { ...c, is_monitored: monitored } : c)
      if (monitored) loadMessages({ ...chat, is_monitored: true })
    }
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
      <div className="flex w-72 flex-shrink-0 flex-col border-r border-gray-200 bg-white">
        <div className="border-b border-gray-100 px-3 py-3">
          <input
            type="text"
            placeholder="Search chats…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-1.5 text-sm outline-none focus:border-blue-300 focus:ring-1 focus:ring-blue-100"
          />
        </div>
        <div className="flex-1 overflow-y-auto">
          {filtered.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-gray-400">No chats found</p>
          ) : (
            filtered.map((chat) => (
              <ChatItem
                key={chat.telegram_id}
                chat={chat}
                selected={selectedChat?.telegram_id === chat.telegram_id}
                onClick={() => handleSelectChat(chat)}
                onToggleMonitor={(m) => handleToggleMonitor(chat, m)}
              />
            ))
          )}
        </div>
      </div>

      {/* Message area */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {selectedChat ? (
          <>
            {/* Header */}
            <div className="flex items-center justify-between border-b border-gray-200 bg-white px-4 py-3">
              <div>
                <h2 className="text-sm font-semibold text-gray-900">{selectedChat.name}</h2>
                <p className="text-xs text-gray-400">{selectedChat.type}</p>
              </div>
              <button
                onClick={handleSync}
                disabled={syncing}
                title="Sync messages"
                className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-gray-500 hover:bg-gray-100 disabled:opacity-50"
              >
                <RefreshCw size={13} className={syncing ? 'animate-spin' : ''} />
                Sync
              </button>
            </div>

            {/* Messages */}
            {loadingMessages ? (
              <div className="flex flex-1 items-center justify-center text-sm text-gray-400">
                Loading…
              </div>
            ) : (
              <MessageList messages={messages} chatType={selectedChat.type} />
            )}

            {/* Compose */}
            <ComposeBar onSend={handleSend} />
          </>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center text-gray-400">
            <p className="text-sm">Select a chat to start</p>
            <p className="mt-1 text-xs">Toggle the monitor icon to sync messages</p>
          </div>
        )}
      </div>
    </div>
  )
}
