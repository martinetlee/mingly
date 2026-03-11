import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { useLocation } from 'react-router-dom'
import { RefreshCw, Radio, EyeOff, Eye, FileText, Info, X } from 'lucide-react'
import type { Chat, Folder, Message, WSEvent } from '../api/types'
import { getChats, getFolders, getMessages, syncChat, sendMessage, getHiddenChats, setHidden, setBriefing, setTracked, markChatRead, reactToMessage } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import MessageList from '../components/MessageList'
import ComposeBar from '../components/ComposeBar'
import ChatInfoPanel from '../components/ChatInfoPanel'

function ChatItem({
  chat,
  selected,
  onClick,
  onToggleTrack,
  onHide,
  onBriefing,
}: {
  chat: Chat
  selected: boolean
  onClick: () => void
  onToggleTrack: () => void
  onHide: () => void
  onBriefing: () => void
}) {
  const [hovered, setHovered] = useState(false)

  const typeColor = {
    private: 'bg-indigo-500/20 text-indigo-400',
    group: 'bg-purple-500/20 text-purple-400',
    channel: 'bg-amber-500/20 text-amber-400',
  }[chat.type] ?? 'bg-slate-600 text-slate-400'

  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      className={`flex cursor-pointer items-center gap-3 px-3 py-2.5 transition-colors ${
        selected ? 'bg-indigo-500/15' : 'hover:bg-slate-700/50'
      }`}
    >
      {/* Avatar */}
      <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-sm font-medium text-slate-300">
        {chat.name.charAt(0).toUpperCase()}
      </div>

      {/* Name + type */}
      <div className="min-w-0 flex-1">
        <p title={chat.name} className={`truncate text-sm font-medium ${selected ? 'text-indigo-300' : 'text-gray-200'}`}>
          {chat.name}
        </p>
        <span className={`inline-block rounded px-1 text-[10px] font-medium ${typeColor}`}>
          {chat.type}
        </span>
      </div>

      {/* Actions */}
      <div className="flex flex-shrink-0 items-center gap-0.5">
        {hovered && (
          <>
            <button
              onClick={(e) => { e.stopPropagation(); onBriefing() }}
              title="Add to briefing"
              className="rounded p-1 text-slate-500 hover:text-purple-400 transition-colors"
            >
              <FileText size={14} />
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); onHide() }}
              title="Hide this chat"
              className="rounded p-1 text-slate-500 hover:text-slate-300 transition-colors"
            >
              <EyeOff size={14} />
            </button>
          </>
        )}
        {!hovered && (chat.unread_count ?? 0) > 0 && (
          <span className="min-w-[18px] rounded-full bg-indigo-500 px-1.5 py-0.5 text-center text-[10px] font-semibold leading-tight text-white">
            {(chat.unread_count ?? 0) > 99 ? '99+' : chat.unread_count}
          </span>
        )}
        <button
          onClick={(e) => { e.stopPropagation(); onToggleTrack() }}
          title={chat.is_tracked ? 'Stop tracking' : 'Track this chat'}
          className={`rounded p-1 transition-colors ${
            chat.is_tracked
              ? 'text-indigo-400 hover:text-indigo-300'
              : 'text-slate-600 hover:text-slate-400'
          }`}
        >
          <Radio size={14} />
        </button>
      </div>
    </div>
  )
}



// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

export default function Chats() {
  const location = useLocation()
  const [chats, setChats] = useState<Chat[]>([])
  const [folders, setFolders] = useState<Folder[]>([])
  const [selectedFolder, setSelectedFolder] = useState<number | null>(null)
  const [selectedChat, setSelectedChat] = useState<Chat | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [messageOffset, setMessageOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [search, setSearch] = useState('')
  const [showHidden, setShowHidden] = useState(false)
  const [hiddenChats, setHiddenChats] = useState<Chat[]>([])
  const [showInfo, setShowInfo] = useState(false)
  const [sidebarWidth, setSidebarWidth] = useState(288) // 72 * 4 = w-72
  const dragging = useRef(false)
  const LIMIT = 100
  const CHAT_PAGE = 50
  const [chatOffset, setChatOffset] = useState(0)
  const [hasMoreChats, setHasMoreChats] = useState(false)
  const [loadingMoreChats, setLoadingMoreChats] = useState(false)
  const chatListRef = useRef<HTMLDivElement>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  // Track which chat telegram_ids have been auto-synced to avoid duplicate requests
  const autoSyncedRef = useRef<Set<number>>(new Set())
  // Highlight a specific message (set from URL params when navigating from Mentions/Briefing)
  const [highlightMsgId, setHighlightMsgId] = useState<number | null>(null)
  const pendingNavRef = useRef<{ chatId: number; msgId: number } | null>(null)
  // When set, loadMessages will use anchor-based fetching to land near this message
  const pendingAnchorRef = useRef<number | null>(null)
  // Reply-to state
  const [replyToMessage, setReplyToMessage] = useState<import('../api/types').Message | null>(null)

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    dragging.current = true
    const startX = e.clientX
    const startWidth = sidebarWidth

    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return
      const next = Math.min(480, Math.max(200, startWidth + e.clientX - startX))
      setSidebarWidth(next)
    }
    const onUp = () => {
      dragging.current = false
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [sidebarWidth])

  const loadChats = useCallback(async () => {
    const data = await getChats(CHAT_PAGE, 0)
    setChats(data)
    setChatOffset(0)
    setHasMoreChats(data.length === CHAT_PAGE)
  }, [CHAT_PAGE])

  const loadMoreChats = useCallback(async () => {
    if (loadingMoreChats || !hasMoreChats) return
    setLoadingMoreChats(true)
    try {
      const nextOffset = chatOffset + CHAT_PAGE
      const data = await getChats(CHAT_PAGE, nextOffset)
      setChats((prev) => {
        const existingIds = new Set(prev.map((c) => c.telegram_id))
        const newChats = data.filter((c) => !existingIds.has(c.telegram_id))
        // Auto-sync 25 messages for each newly visible chat (fire-and-forget)
        for (const c of newChats) {
          if (!autoSyncedRef.current.has(c.telegram_id)) {
            autoSyncedRef.current.add(c.telegram_id)
            syncChat(c.telegram_id, 25).catch(() => {})
          }
        }
        return [...prev, ...newChats]
      })
      setChatOffset(nextOffset)
      setHasMoreChats(data.length === CHAT_PAGE)
    } finally {
      setLoadingMoreChats(false)
    }
  }, [loadingMoreChats, hasMoreChats, chatOffset, CHAT_PAGE])

  const loadFolders = useCallback(async () => {
    const data = await getFolders()
    setFolders(data)
  }, [])

  const loadMessages = useCallback(async (chat: Chat) => {
    setLoadingMessages(true)
    // Consume the pending anchor (if navigating from a citation) and compute offset server-side
    const anchor = pendingAnchorRef.current
    pendingAnchorRef.current = null
    setMessageOffset(0)
    try {
      const data = await getMessages(chat.telegram_id, LIMIT, 0, anchor ?? undefined)
      setMessages(data)
      setHasMore(data.length === LIMIT)
    } finally {
      setLoadingMessages(false)
    }
  }, [LIMIT])

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
  }, [selectedChat, messageOffset, loadingMore, LIMIT])

  useEffect(() => {
    // Parse URL params for mention navigation: ?chatId=X&msgId=Y
    const params = new URLSearchParams(location.search)
    const chatIdParam = params.get('chatId')
    const msgIdParam = params.get('msgId')
    if (chatIdParam && msgIdParam) {
      const chatId = parseInt(chatIdParam, 10)
      const msgId = parseInt(msgIdParam, 10)
      if (!isNaN(chatId) && !isNaN(msgId)) {
        pendingNavRef.current = { chatId, msgId }
      }
    }
    loadChats()
    loadFolders()
  }, [loadChats, loadFolders, location.search])

  // When chats load, auto-select the chat from URL params if pending
  useEffect(() => {
    if (chats.length === 0 || !pendingNavRef.current) return
    const { chatId, msgId } = pendingNavRef.current
    pendingNavRef.current = null
    const target = chats.find((c) => c.telegram_id === chatId)
    if (target) {
      pendingAnchorRef.current = msgId  // loadMessages will use this to fetch the right window
      setSelectedChat(target)
      setMessages([])
      setHasMore(false)
      setMessageOffset(0)
      setShowInfo(false)
      setHighlightMsgId(msgId)
    }
  }, [chats])

  useEffect(() => {
    if (selectedChat) {
      loadMessages(selectedChat)
    }
  }, [selectedChat, loadMessages])

  const handleWsEvent = useCallback((event: WSEvent) => {
    if (event.type === 'new_message') {
      // If this chat_id is new (not yet seen in our list), sync 25 messages after loading
      setChats((prev) => {
        const known = prev.some((c) => c.telegram_id === event.chat_id)
        if (!known && !autoSyncedRef.current.has(event.chat_id)) {
          autoSyncedRef.current.add(event.chat_id)
          // Delay slightly so the chat upsert from loadChats completes first
          setTimeout(() => syncChat(event.chat_id, 25).catch(() => {}), 1500)
        }
        return prev
      })
      loadChats()
      if (selectedChat && event.chat_id === selectedChat.telegram_id) {
        loadMessages(selectedChat)
      }
      // Fire desktop notification if not outgoing
      if (!event.is_outgoing) {
        const pref = localStorage.getItem('notifPref') ?? 'tracked'
        const shouldNotify =
          pref === 'all' || (pref === 'tracked' && event.is_tracked)
        if (shouldNotify && Notification.permission === 'granted') {
          const title = event.chat_name ?? 'New message'
          const body = event.sender_name
            ? event.sender_name + ': ' + (event.text ?? '')
            : (event.text ?? '')
          new Notification(title, { body, silent: false })
        }
      }
    }
  }, [selectedChat, loadChats, loadMessages])

  useWebSocket(handleWsEvent)

  const handleSelectChat = (chat: Chat) => {
    if (selectedChat?.telegram_id === chat.telegram_id) return
    setSelectedChat(chat)
    setMessages([])
    setHasMore(false)
    setMessageOffset(0)
    setShowInfo(false)
    setHighlightMsgId(null)
    setReplyToMessage(null)
    // Mark as read: clear badge locally and persist to backend
    if ((chat.unread_count ?? 0) > 0) {
      setChats((prev) =>
        prev.map((c) => c.telegram_id === chat.telegram_id ? { ...c, unread_count: 0 } : c)
      )
      markChatRead(chat.telegram_id).catch(() => {})
    }
  }

  const handleTrackChat = async (chat: Chat) => {
    const tracked = !chat.is_tracked
    await setTracked(chat.telegram_id, tracked)
    setChats((prev) =>
      prev.map((c) => (c.telegram_id === chat.telegram_id ? { ...c, is_tracked: tracked } : c))
    )
    if (selectedChat?.telegram_id === chat.telegram_id) {
      setSelectedChat((c) => c ? { ...c, is_tracked: tracked } : c)
    }
  }

  const handleBriefingChat = async (chat: Chat) => {
    await setBriefing(chat.telegram_id, true)
    setChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
    if (selectedChat?.telegram_id === chat.telegram_id) {
      setSelectedChat(null)
      setMessages([])
    }
  }

  const handleHideChat = async (chat: Chat) => {
    await setHidden(chat.telegram_id, true)
    setChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
    if (selectedChat?.telegram_id === chat.telegram_id) {
      setSelectedChat(null)
      setMessages([])
    }
  }

  const handleUnhideChat = async (chat: Chat) => {
    await setHidden(chat.telegram_id, false)
    setHiddenChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
    setChats((prev) => [...prev, { ...chat, is_hidden: false }])
  }

  const handleToggleHiddenPanel = async () => {
    if (!showHidden) {
      const data = await getHiddenChats()
      setHiddenChats(data)
    }
    setShowHidden((v) => !v)
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
    await sendMessage(selectedChat.telegram_id, text, replyToMessage?.telegram_id)
    setReplyToMessage(null)
    await loadMessages(selectedChat)
  }

  const handleReact = async (msg: Message, emoji: string | null) => {
    if (!selectedChat) return
    try {
      await reactToMessage(selectedChat.telegram_id, msg.telegram_id, emoji)
      // Update the message's my_reaction in local state immediately
      setMessages((prev) =>
        prev.map((m) => m.telegram_id === msg.telegram_id ? { ...m, my_reaction: emoji } : m)
      )
    } catch {
      // silently ignore
    }
  }

  const folderFiltered = selectedFolder === null
    ? chats
    : (() => {
        const folder = folders.find((f) => f.id === selectedFolder)
        if (!folder) return chats
        return chats.filter((c) => {
          if (folder.chat_ids.includes(c.telegram_id)) return true
          if (folder.include_groups && c.type === 'group') return true
          if (folder.include_channels && c.type === 'channel') return true
          if (folder.include_contacts && c.type === 'private') return true
          return false
        })
      })()

  const filtered = folderFiltered.filter((c) =>
    c.name.toLowerCase().includes(search.toLowerCase())
  )

  // Clear selected chat when it's no longer in the filtered list
  useEffect(() => {
    if (selectedChat && !filtered.some((c) => c.telegram_id === selectedChat.telegram_id)) {
      setSelectedChat(null)
    }
  }, [filtered, selectedChat])

  // Keyboard shortcuts
  const chatShortcuts = useMemo(() => ({
    'j': () => {
      const idx = selectedChat ? filtered.findIndex((c) => c.telegram_id === selectedChat.telegram_id) : -1
      const next = filtered[idx + 1]
      if (next) handleSelectChat(next)
    },
    'arrowdown': () => {
      const idx = selectedChat ? filtered.findIndex((c) => c.telegram_id === selectedChat.telegram_id) : -1
      const next = filtered[idx + 1]
      if (next) handleSelectChat(next)
    },
    'k': () => {
      const idx = selectedChat ? filtered.findIndex((c) => c.telegram_id === selectedChat.telegram_id) : -1
      const prev = filtered[idx - 1]
      if (prev) handleSelectChat(prev)
    },
    'arrowup': () => {
      const idx = selectedChat ? filtered.findIndex((c) => c.telegram_id === selectedChat.telegram_id) : -1
      const prev = filtered[idx - 1]
      if (prev) handleSelectChat(prev)
    },
    '/': () => searchInputRef.current?.focus(),
    'i': () => setShowInfo((v) => !v),
  }), [selectedChat, filtered, handleSelectChat])

  useKeyboardShortcuts(chatShortcuts)

  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Chat list */}
      <div className="flex flex-shrink-0 flex-col border-r border-slate-700/50 bg-slate-800 relative" style={{ width: sidebarWidth }}>
        {/* Drag handle */}
        <div
          onMouseDown={onDragStart}
          className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-indigo-500 transition-colors z-10"
        />
        {folders.length > 0 && (
          <div className="folder-scrollbar flex overflow-x-auto border-b border-slate-700/50 px-2 pt-2 pb-1 gap-1">
            <button
              onClick={() => setSelectedFolder(null)}
              className={`flex-shrink-0 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                selectedFolder === null
                  ? 'bg-indigo-500/20 text-indigo-400'
                  : 'text-slate-400 hover:bg-slate-700'
              }`}
            >
              All
            </button>
            {folders.map((folder) => (
              <button
                key={folder.id}
                onClick={() => setSelectedFolder(folder.id)}
                className={`flex-shrink-0 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  selectedFolder === folder.id
                    ? 'bg-indigo-500/20 text-indigo-400'
                    : 'text-slate-400 hover:bg-slate-700'
                }`}
              >
                {folder.title}
              </button>
            ))}
          </div>
        )}
        <div className="border-b border-slate-700/50 px-3 py-3">
          <input
            ref={searchInputRef}
            type="text"
            placeholder="Search chats…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
          />
        </div>
        <div
          ref={chatListRef}
          className="flex-1 overflow-y-auto"
          onScroll={(e) => {
            const el = e.currentTarget
            if (el.scrollHeight - el.scrollTop - el.clientHeight < 80) {
              loadMoreChats()
            }
          }}
        >
          {filtered.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-slate-500">No chats found</p>
          ) : (
            filtered.map((chat) => (
              <ChatItem
                key={chat.telegram_id}
                chat={chat}
                selected={selectedChat?.telegram_id === chat.telegram_id}
                onClick={() => handleSelectChat(chat)}
                onToggleTrack={() => handleTrackChat(chat)}
                onHide={() => handleHideChat(chat)}
                onBriefing={() => handleBriefingChat(chat)}
              />
            ))
          )}
          {loadingMoreChats && (
            <p className="px-4 py-3 text-center text-xs text-slate-500">Loading…</p>
          )}
        </div>

        {/* Hidden chats panel */}
        <div className="border-t border-slate-700/50">
          <button
            onClick={handleToggleHiddenPanel}
            className="flex w-full items-center gap-1.5 px-3 py-2 text-xs text-slate-500 hover:text-slate-300 transition-colors"
          >
            <Eye size={12} />
            {showHidden ? 'Hide hidden chats' : 'Show hidden chats'}
          </button>
          {showHidden && (
            <div className="max-h-48 overflow-y-auto border-t border-slate-700/50 bg-slate-900/50">
              {hiddenChats.length === 0 ? (
                <p className="px-4 py-3 text-center text-xs text-slate-500">No hidden chats</p>
              ) : (
                hiddenChats.map((chat) => (
                  <div key={chat.telegram_id} className="flex items-center gap-2 px-3 py-2">
                    <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-xs font-medium text-slate-400">
                      {chat.name.charAt(0).toUpperCase()}
                    </div>
                    <p title={chat.name} className="min-w-0 flex-1 truncate text-xs text-slate-400">{chat.name}</p>
                    <button
                      onClick={() => handleUnhideChat(chat)}
                      title="Unhide"
                      className="flex-shrink-0 rounded p-1 text-slate-500 hover:text-slate-300 transition-colors"
                    >
                      <Eye size={12} />
                    </button>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      </div>

      {/* Message area + optional info panel */}
      <div className="flex flex-1 overflow-hidden">
        <div className="flex flex-1 flex-col overflow-hidden bg-slate-900">
          {selectedChat ? (
            <>
              {/* Header */}
              <div className="flex items-center justify-between border-b border-slate-700/50 bg-slate-800 px-4 py-3">
                <div>
                  <h2 className="text-sm font-semibold text-gray-100">{selectedChat.name}</h2>
                  <p className="text-xs text-slate-400">{selectedChat.type}</p>
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
                    title="Sync messages"
                    className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs text-slate-400 hover:bg-slate-700 disabled:opacity-50"
                  >
                    <RefreshCw size={13} className={syncing ? 'animate-spin' : ''} />
                    Sync
                  </button>
                </div>
              </div>

              {/* Messages */}
              {loadingMessages ? (
                <div className="flex flex-1 items-center justify-center text-sm text-slate-500">
                  Loading…
                </div>
              ) : (
                <div className="flex flex-1 flex-col overflow-hidden">
                  {hasMore && (
                    <div className="flex justify-center border-b border-slate-700/50 py-2">
                      <button
                        onClick={loadMoreMessages}
                        disabled={loadingMore}
                        className="text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-50"
                      >
                        {loadingMore ? 'Loading…' : 'Load older messages'}
                      </button>
                    </div>
                  )}
                  <MessageList messages={messages} chatType={selectedChat.type} highlightTelegramId={highlightMsgId} onReply={setReplyToMessage} onReact={handleReact} />
                </div>
              )}

              {/* Compose */}
              <ComposeBar onSend={handleSend} replyTo={replyToMessage} onCancelReply={() => setReplyToMessage(null)} />
            </>
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center text-slate-500">
              <p className="text-sm">Select a chat to start</p>
            </div>
          )}
        </div>

        {/* Info panel */}
        {showInfo && selectedChat && (
          <ChatInfoPanel chat={selectedChat} onClose={() => setShowInfo(false)} />
        )}
      </div>
    </div>
  )
}
