import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { useLocation } from 'react-router-dom'
import { RefreshCw, Radio, EyeOff, Eye, FileText, Info, X, Search, Star, Loader2, MessageSquare, Pin, Archive, BellOff, Bell, MailOpen, MoreVertical, Plus, ShieldAlert } from 'lucide-react'
import type { Chat, Folder, Message, PinnedMessage, UserPresence, WSEvent } from '../api/types'
import { getChats, getFolders, getMessages, syncChat, sendMessage, editMessage, deleteMessages, forwardMessages, sendFiles, getHiddenChats, setHidden, setBriefing, setTracked, markChatRead, reactToMessage, searchChatMessages, getChatDateOffset, getContactByTelegram, setContactImportantByTelegram, ensureChat, setPinned, setArchived, setMuted, setMarkedUnread, getArchivedChats, getPinnedMessage, getUserPresence, sendTypingAction, getTopicMessages, pinMessage, getScamFlags } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import MessageList from '../components/MessageList'
import ComposeBar from '../components/ComposeBar'
import ChatInfoPanel from '../components/ChatInfoPanel'
import ForumTopicsPanel from '../components/ForumTopicsPanel'
import ChatPickerDialog from '../components/ChatPickerDialog'
import DeleteConfirmDialog from '../components/DeleteConfirmDialog'
import NewChatDialog from '../components/NewChatDialog'

import { authenticatedMediaUrl } from '../api/session'

const AVATAR_COLORS = [
  'bg-rose-600', 'bg-amber-600', 'bg-emerald-600', 'bg-sky-600',
  'bg-violet-600', 'bg-pink-600', 'bg-teal-600', 'bg-orange-600',
]
const failedChatPhotos = new Set<number>()

function ChatAvatar({ chatId, chatName, size = 36 }: { chatId: number; chatName: string; size?: number }) {
  const [imgFailed, setImgFailed] = useState(false)
  const initial = (chatName || '?')[0].toUpperCase()
  const colorIndex = Math.abs(chatId) % AVATAR_COLORS.length
  const bgColor = AVATAR_COLORS[colorIndex]
  const hasPhoto = !failedChatPhotos.has(chatId) && !imgFailed
  const photoUrl = authenticatedMediaUrl(`/api/media/profile_photos/${chatId}.jpg`)

  return (
    <div className="relative flex-shrink-0" style={{ width: size, height: size }}>
      <div
        className={`absolute inset-0 flex items-center justify-center rounded-full text-white font-medium ${bgColor}`}
        style={{ fontSize: size * 0.38 }}
      >
        {initial}
      </div>
      {hasPhoto && (
        <img
          src={photoUrl}
          alt=""
          className="absolute inset-0 rounded-full object-cover"
          style={{ width: size, height: size }}
          onError={() => { failedChatPhotos.add(chatId); setImgFailed(true) }}
        />
      )}
    </div>
  )
}

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

function ChatContextMenu({
  chat,
  position,
  onClose,
  onPin,
  onArchive,
  onMute,
  onMarkUnread,
  onHide,
  onBriefing,
  onToggleTrack,
}: {
  chat: Chat
  position: { x: number; y: number }
  onClose: () => void
  onPin: () => void
  onArchive: () => void
  onMute: (duration?: string) => void
  onMarkUnread: () => void
  onHide: () => void
  onBriefing: () => void
  onToggleTrack: () => void
}) {
  const [showMuteOptions, setShowMuteOptions] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose])

  const menuStyle: React.CSSProperties = {
    position: 'fixed',
    left: position.x,
    top: position.y,
    zIndex: 50,
  }

  const itemClass = "flex w-full items-center gap-2 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-600 transition-colors text-left"

  return (
    <div ref={menuRef} style={menuStyle} className="min-w-[180px] rounded-lg border border-slate-600 bg-slate-700 py-1 shadow-xl">
      <button onClick={onPin} className={itemClass}>
        <Pin size={13} className={chat.is_pinned ? 'text-indigo-400' : ''} />
        {chat.is_pinned ? 'Unpin' : 'Pin to top'}
      </button>
      <button onClick={onArchive} className={itemClass}>
        <Archive size={13} />
        {chat.is_archived ? 'Unarchive' : 'Archive'}
      </button>
      {!showMuteOptions ? (
        <button onClick={() => chat.is_muted ? onMute() : setShowMuteOptions(true)} className={itemClass}>
          {chat.is_muted ? <Bell size={13} /> : <BellOff size={13} />}
          {chat.is_muted ? 'Unmute' : 'Mute...'}
        </button>
      ) : (
        <div className="border-t border-b border-slate-600 py-0.5">
          <p className="px-3 py-1 text-[10px] text-slate-500 font-medium">Mute for:</p>
          {[
            { label: '1 hour', value: '1h' },
            { label: '8 hours', value: '8h' },
            { label: '1 day', value: '1d' },
            { label: 'Forever', value: 'forever' },
          ].map((opt) => (
            <button key={opt.value} onClick={() => onMute(opt.value)} className={itemClass}>
              <BellOff size={13} />
              {opt.label}
            </button>
          ))}
        </div>
      )}
      <button onClick={onMarkUnread} className={itemClass}>
        <MailOpen size={13} />
        {chat.is_marked_unread ? 'Mark as read' : 'Mark as unread'}
      </button>
      <div className="my-0.5 border-t border-slate-600" />
      <button onClick={onToggleTrack} className={itemClass}>
        <Radio size={13} className={chat.is_tracked ? 'text-indigo-400' : ''} />
        {chat.is_tracked ? 'Stop tracking' : 'Track'}
      </button>
      <button onClick={onBriefing} className={itemClass}>
        <FileText size={13} />
        Add to briefing
      </button>
      <button onClick={onHide} className={itemClass}>
        <EyeOff size={13} />
        Hide
      </button>
    </div>
  )
}

function ChatItem({
  chat,
  selected,
  onClick,
  onToggleTrack,
  onHide,
  onBriefing,
  onContextMenu,
  hasScamFlag,
}: {
  chat: Chat
  selected: boolean
  onClick: () => void
  onToggleTrack: () => void
  onHide: () => void
  onBriefing: () => void
  onContextMenu: (e: React.MouseEvent) => void
  hasScamFlag?: boolean
}) {
  const typeColor = {
    private: 'bg-indigo-500/20 text-indigo-400',
    group: 'bg-purple-500/20 text-purple-400',
    channel: 'bg-amber-500/20 text-amber-400',
  }[chat.type] ?? 'bg-slate-600 text-slate-400'

  return (
    <div
      onContextMenu={(e) => { e.preventDefault(); onContextMenu(e) }}
      className={`group flex items-center gap-3 px-3 py-2.5 transition-colors ${
        selected ? 'bg-indigo-500/15' : 'hover:bg-slate-700/50'
      }`}
    >
      {/* Row select button */}
      <button
        type="button"
        onClick={onClick}
        aria-current={selected ? 'true' : undefined}
        className="flex min-w-0 flex-1 cursor-pointer items-center gap-3 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:rounded"
      >
        {/* Avatar */}
        <ChatAvatar chatId={chat.telegram_id} chatName={chat.name} size={36} />

        {/* Name + preview */}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            {chat.is_pinned && <Pin size={11} className="flex-shrink-0 text-indigo-400" />}
            <p title={chat.name} className={`truncate text-sm font-medium ${selected ? 'text-indigo-300' : 'text-gray-200'}`}>
              {chat.name}
            </p>
            <span className={`flex-shrink-0 inline-block rounded px-1 text-[10px] font-medium ${typeColor}`}>
              {chat.type}
            </span>
            {chat.is_forum && (
              <span className="flex-shrink-0 inline-block rounded px-1 text-[10px] font-medium bg-purple-500/20 text-purple-400">
                <MessageSquare size={9} className="inline" />
              </span>
            )}
            {hasScamFlag && <span title="Contains flagged scammer"><ShieldAlert size={11} className="flex-shrink-0 text-red-400" /></span>}
            {chat.is_muted && <BellOff size={11} className="flex-shrink-0 text-slate-500" />}
            {chat.last_message?.date && (
              <span className="ml-auto flex-shrink-0 text-[10px] text-slate-500">
                {fmtDate(chat.last_message.date)}
              </span>
            )}
          </div>
          {chat.last_message && (
            <p className="truncate text-xs text-slate-500 mt-0.5">
              {chat.last_message.sender_name && (
                <span className="text-slate-400">{chat.last_message.sender_name}: </span>
              )}
              {chat.last_message.text || '[media]'}
            </p>
          )}
        </div>
      </button>

      {/* Actions - always in DOM for keyboard/screen-reader access */}
      <div className="flex flex-shrink-0 items-center gap-0.5">
        {((chat.unread_count ?? 0) > 0 || chat.is_marked_unread) && (
          <span className={`min-w-[18px] rounded-full px-1.5 py-0.5 text-center text-[10px] font-semibold leading-tight text-white ${chat.is_muted ? 'bg-slate-500' : 'bg-indigo-500'}`} aria-label={`${chat.unread_count ?? 0} unread`}>
            {(chat.unread_count ?? 0) > 0
              ? (chat.unread_count ?? 0) > 99 ? '99+' : chat.unread_count
              : '\u2022'}
          </span>
        )}
        <button
          onClick={(e) => { e.stopPropagation(); onContextMenu(e) }}
          title="More actions"
          aria-label="More actions"
          className="rounded p-1 text-slate-500 opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100 hover:text-slate-300 focus-visible:ring-2 focus-visible:ring-indigo-500 focus:outline-none"
        >
          <MoreVertical size={14} />
        </button>
        <button
          onClick={onToggleTrack}
          title={chat.is_tracked ? 'Stop tracking' : 'Track this chat'}
          aria-label={chat.is_tracked ? 'Stop tracking' : 'Track this chat'}
          className={`rounded p-1 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 ${
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
  const [flaggedSenderIds, setFlaggedSenderIds] = useState<Set<number>>(new Set())
  const [flaggedChatIds, setFlaggedChatIds] = useState<Set<number>>(new Set())
  const [search, setSearch] = useState('')
  const [showHidden, setShowHidden] = useState(false)
  const [hiddenChats, setHiddenChats] = useState<Chat[]>([])
  const [showInfo, setShowInfo] = useState(false)
  const [sidebarWidth, setSidebarWidth] = useState(288) // 72 * 4 = w-72
  const dragging = useRef(false)
  const [sortBy, setSortBy] = useState<'recent' | 'unread' | 'alpha'>('recent')
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
  const [uploadProgress, setUploadProgress] = useState<number | null>(null)
  // In-chat search state
  const [showChatSearch, setShowChatSearch] = useState(false)
  const [chatSearch, setChatSearch] = useState('')
  const [chatSearchResults, setChatSearchResults] = useState<Message[]>([])
  const [searchingChat, setSearchingChat] = useState(false)
  const chatSearchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Forum topic state
  const [selectedTopicId, setSelectedTopicId] = useState<number | null>(null)
  const [selectedTopicTitle, setSelectedTopicTitle] = useState<string | null>(null)

  // Edit/delete/forward state
  const [editingMessage, setEditingMessage] = useState<Message | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Message | null>(null)
  const [forwardTarget, setForwardTarget] = useState<Message | null>(null)

  // Pinned message for the selected chat
  const [pinnedMessage, setPinnedMessage] = useState<PinnedMessage | null>(null)
  // Unread count snapshot for the selected chat (used for unread separator)
  const [selectedChatUnread, setSelectedChatUnread] = useState(0)
  // Typing indicator state: map of chat_id -> { userName, expiresAt }
  const [typingUsers, setTypingUsers] = useState<Record<number, { userName: string; expiresAt: number }>>({})
  // Online/last-seen presence for the selected private chat
  const [presence, setPresence] = useState<UserPresence | null>(null)

  // Important-contact star for 1-1 chats
  const [isImportant, setIsImportant] = useState(false)
  const [togglingImportant, setTogglingImportant] = useState(false)
  // True while we're resolving a pendingNav chat (loading indicator in right pane)
  const [navigatingToChat, setNavigatingToChat] = useState(false)
  // Context menu state
  const [contextMenu, setContextMenu] = useState<{ chat: Chat; x: number; y: number } | null>(null)
  // Archived chats panel
  const [showArchived, setShowArchived] = useState(false)
  const [archivedChats, setArchivedChats] = useState<Chat[]>([])
  // New Chat dialog
  const [showNewChat, setShowNewChat] = useState(false)

  // Fetch presence status when selected chat changes (private chats only)
  useEffect(() => {
    if (!selectedChat || selectedChat.type !== 'private') {
      setPresence(null)
      return
    }
    getUserPresence(selectedChat.telegram_id)
      .then((p) => setPresence(p))
      .catch(() => setPresence(null))
  }, [selectedChat?.telegram_id, selectedChat?.type])

  // Clear expired typing indicators every second
  useEffect(() => {
    const interval = setInterval(() => {
      setTypingUsers((prev) => {
        const now = Date.now()
        const next: Record<number, { userName: string; expiresAt: number }> = {}
        for (const [key, val] of Object.entries(prev)) {
          if (val.expiresAt > now) next[Number(key)] = val
        }
        return Object.keys(next).length !== Object.keys(prev).length ? next : prev
      })
    }, 1000)
    return () => clearInterval(interval)
  }, [])

  // Fetch pinned message when selected chat changes
  useEffect(() => {
    if (!selectedChat) {
      setPinnedMessage(null)
      return
    }
    getPinnedMessage(selectedChat.telegram_id)
      .then((msg) => setPinnedMessage(msg))
      .catch(() => setPinnedMessage(null))
  }, [selectedChat?.telegram_id])

  // Fetch importance status when selected chat changes (private chats only)
  useEffect(() => {
    if (!selectedChat || selectedChat.type !== 'private') {
      setIsImportant(false)
      return
    }
    getContactByTelegram(selectedChat.telegram_id)
      .then((c) => setIsImportant(c.is_important))
      .catch(() => setIsImportant(false))
  }, [selectedChat?.telegram_id, selectedChat?.type])

  const handleToggleImportant = useCallback(async () => {
    if (!selectedChat || togglingImportant) return
    const newVal = !isImportant
    setTogglingImportant(true)
    try {
      const updated = await setContactImportantByTelegram(selectedChat.telegram_id, newVal)
      setIsImportant(updated.is_important)
    } catch {
      // ignore
    } finally {
      setTogglingImportant(false)
    }
  }, [selectedChat, isImportant, togglingImportant])

  const handleChatCreated = useCallback((chat: Chat) => {
    setShowNewChat(false)
    // Add the new chat to the list if not already present
    setChats((prev) => {
      if (prev.some((c) => c.telegram_id === chat.telegram_id)) return prev
      return [chat, ...prev]
    })
    // Select the newly created chat
    setSelectedChat(chat)
    setMessages([])
    setHasMore(false)
    setMessageOffset(0)
    setShowInfo(false)
    setHighlightMsgId(null)
    setReplyToMessage(null)
  }, [])

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
    const data = await getChats(CHAT_PAGE, 0, sortBy)
    setChats(data)
    setChatOffset(0)
    setHasMoreChats(data.length === CHAT_PAGE)
  }, [CHAT_PAGE, sortBy])

  const loadMoreChats = useCallback(async () => {
    if (loadingMoreChats || !hasMoreChats) return
    setLoadingMoreChats(true)
    try {
      const nextOffset = chatOffset + CHAT_PAGE
      const data = await getChats(CHAT_PAGE, nextOffset, sortBy)
      setChats((prev) => {
        const existingIds = new Set(prev.map((c) => c.telegram_id))
        const newChats = data.filter((c) => !existingIds.has(c.telegram_id))
        // Auto-sync newly visible chats (sequential to avoid stampeding)
        const toSync = newChats.filter((c) => !autoSyncedRef.current.has(c.telegram_id))
        for (const c of toSync) autoSyncedRef.current.add(c.telegram_id)
        if (toSync.length > 0) {
          ;(async () => {
            for (const c of toSync) {
              await syncChat(c.telegram_id, 25).catch(() => {})
            }
          })()
        }
        return [...prev, ...newChats]
      })
      setChatOffset(nextOffset)
      setHasMoreChats(data.length === CHAT_PAGE)
    } finally {
      setLoadingMoreChats(false)
    }
  }, [loadingMoreChats, hasMoreChats, chatOffset, CHAT_PAGE, sortBy])

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

  // Track whether we've exhausted Telegram history for this chat
  const [noMoreRemote, setNoMoreRemote] = useState(false)

  const loadMoreMessages = useCallback(async () => {
    if (!selectedChat || loadingMore) return
    setLoadingMore(true)
    const nextOffset = messageOffset + LIMIT
    try {
      const data = await getMessages(selectedChat.telegram_id, LIMIT, nextOffset)
      if (data.length > 0) {
        setMessages((prev) => [...data, ...prev])
        setMessageOffset(nextOffset)
        setHasMore(data.length === LIMIT)
      } else {
        // No more local messages — try fetching from Telegram
        setHasMore(false)
        if (!noMoreRemote) {
          try {
            await syncChat(selectedChat.telegram_id, 100)
            // Reload all messages after sync
            const refreshed = await getMessages(selectedChat.telegram_id, LIMIT, nextOffset)
            if (refreshed.length > 0) {
              setMessages((prev) => [...refreshed, ...prev])
              setMessageOffset(nextOffset)
              setHasMore(refreshed.length === LIMIT)
            } else {
              setNoMoreRemote(true)
            }
          } catch {
            setNoMoreRemote(true)
          }
        }
      }
    } finally {
      setLoadingMore(false)
    }
  }, [selectedChat, messageOffset, loadingMore, LIMIT, noMoreRemote])

  useEffect(() => {
    // Parse URL params for mention navigation: ?chatId=X&msgId=Y
    const params = new URLSearchParams(location.search)
    const chatIdParam = params.get('chatId')
    const msgIdParam = params.get('msgId')
    if (chatIdParam) {
      const chatId = parseInt(chatIdParam, 10)
      const msgId = msgIdParam ? parseInt(msgIdParam, 10) : 0
      if (!isNaN(chatId)) {
        pendingNavRef.current = { chatId, msgId }
        setNavigatingToChat(true)
      }
    }
    loadChats()
    loadFolders()
    getScamFlags()
      .then((data) => {
        setFlaggedSenderIds(new Set(data.flags.map((f) => f.telegram_id)))
        setFlaggedChatIds(new Set(data.flagged_chat_ids))
      })
      .catch(() => {})
  }, [loadChats, loadFolders, location.search])

  // When chats load, auto-select the chat from URL params if pending
  const retryNavRef = useRef(false)
  useEffect(() => {
    if (!pendingNavRef.current) return
    if (chats.length === 0 && !retryNavRef.current) return
    const { chatId, msgId } = pendingNavRef.current
    const target = chats.find((c) => c.telegram_id === chatId)
    if (target) {
      pendingNavRef.current = null
      retryNavRef.current = false
      setNavigatingToChat(false)
      if (msgId) {
        pendingAnchorRef.current = msgId  // loadMessages will use this to fetch the right window
        setHighlightMsgId(msgId)
      }
      setSelectedChat(target)
      setMessages([])
      setHasMore(false)
      setMessageOffset(0)
      setShowInfo(false)
    } else if (!retryNavRef.current) {
      // Chat not found in paginated list — fetch it directly by telegram_id.
      // This handles chats beyond the first page (CHAT_PAGE=50).
      retryNavRef.current = true
      ensureChat(chatId)
        .then((chat) => {
          // Prepend the fetched chat so the next effect cycle finds it
          setChats((prev) => {
            if (prev.some((c) => c.telegram_id === chat.telegram_id)) return prev
            return [chat, ...prev]
          })
        })
        .catch(() => {
          // Could not fetch — give up
          pendingNavRef.current = null
          retryNavRef.current = false
          setNavigatingToChat(false)
        })
    } else {
      // Already retried, chat still not found — give up
      pendingNavRef.current = null
      retryNavRef.current = false
      setNavigatingToChat(false)
    }
  }, [chats, loadChats])

  useEffect(() => {
    if (selectedChat) {
      loadMessages(selectedChat)
    }
  }, [selectedChat, loadMessages])

  const handleWsEvent = useCallback((event: WSEvent) => {
    if (event.type === 'user_typing') {
      setTypingUsers((prev) => ({
        ...prev,
        [event.chat_id]: { userName: event.user_name, expiresAt: Date.now() + 5000 },
      }))
      return
    }
    if (event.type === 'user_status') {
      // Update presence if this is the currently selected chat user
      if (selectedChat && selectedChat.type === 'private' && event.user_id === selectedChat.telegram_id) {
        setPresence({
          status: event.status as UserPresence['status'],
          last_seen: event.last_seen,
        })
      }
      return
    }
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
    if (event.type === 'scam_detected') {
      // Update scam scores in-place without reloading (avoids scroll jump)
      if (selectedChat && event.chat_id === selectedChat.telegram_id) {
        setMessages((prev) => prev.map((m) =>
          event.message_ids.includes(m.tg_message_id)
            ? { ...m, scam_score: event.confidence, scam_reason: event.reason }
            : m
        ))
      }
    }
    if (event.type === 'scam_check_error') {
      window.dispatchEvent(new CustomEvent('scam_check_error', { detail: event }))
    }
    if (event.type === 'scam_check_safe') {
      window.dispatchEvent(new CustomEvent('scam_check_safe', { detail: event }))
    }
    if (event.type === 'chat_synced') {
      // Background sync finished — reload messages if viewing this chat
      if (selectedChat && event.chat_id === selectedChat.telegram_id) {
        loadMessages(selectedChat)
      }
    }
    if (event.type === 'scam_users_flagged') {
      setFlaggedSenderIds((prev) => {
        const next = new Set(prev)
        for (const id of event.telegram_ids) next.add(id)
        return next
      })
    }
  }, [selectedChat, loadChats, loadMessages])

  useWebSocket(handleWsEvent)

  // Debounced in-chat search
  useEffect(() => {
    if (!selectedChat || !chatSearch.trim()) {
      setChatSearchResults([])
      return
    }
    if (chatSearchTimerRef.current) clearTimeout(chatSearchTimerRef.current)
    chatSearchTimerRef.current = setTimeout(async () => {
      setSearchingChat(true)
      try {
        const results = await searchChatMessages(selectedChat.telegram_id, chatSearch.trim())
        setChatSearchResults(results)
      } finally {
        setSearchingChat(false)
      }
    }, 300)
    return () => { if (chatSearchTimerRef.current) clearTimeout(chatSearchTimerRef.current) }
  }, [chatSearch, selectedChat])

  const handleDateJump = async (dateStr: string) => {
    if (!selectedChat || !dateStr) return
    const { offset } = await getChatDateOffset(selectedChat.telegram_id, dateStr)
    setLoadingMessages(true)
    try {
      const data = await getMessages(selectedChat.telegram_id, LIMIT, offset)
      setMessages(data)
      setMessageOffset(offset)
      setHasMore(offset + data.length < offset + LIMIT || data.length === LIMIT)
    } finally {
      setLoadingMessages(false)
    }
  }

  const handleJumpToMessage = async (msg: Message) => {
    if (!selectedChat) return
    setLoadingMessages(true)
    try {
      const data = await getMessages(selectedChat.telegram_id, LIMIT, 0, msg.tg_message_id)
      setMessages(data)
      setHighlightMsgId(msg.tg_message_id)
      setTimeout(() => setHighlightMsgId(null), 3000)
    } finally {
      setLoadingMessages(false)
    }
  }

  const handleSelectChat = (chat: Chat) => {
    if (selectedChat?.telegram_id === chat.telegram_id) return
    // Capture unread count before clearing so MessageList can show the separator
    setSelectedChatUnread(chat.unread_count ?? 0)
    setSelectedChat(chat)
    setMessages([])
    setHasMore(false)
    setNoMoreRemote(false)
    setMessageOffset(0)
    setShowInfo(false)
    setHighlightMsgId(null)
    setReplyToMessage(null)
    setEditingMessage(null)
    setShowChatSearch(false)
    setChatSearch('')
    setChatSearchResults([])
    setSelectedTopicId(null)
    setSelectedTopicTitle(null)
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

  const handlePinChat = async (chat: Chat) => {
    const pinned = !chat.is_pinned
    await setPinned(chat.telegram_id, pinned)
    setChats((prev) =>
      prev.map((c) => (c.telegram_id === chat.telegram_id ? { ...c, is_pinned: pinned } : c))
    )
    if (selectedChat?.telegram_id === chat.telegram_id) {
      setSelectedChat((c) => c ? { ...c, is_pinned: pinned } : c)
    }
    setContextMenu(null)
  }

  const handleArchiveChat = async (chat: Chat) => {
    const archived = !chat.is_archived
    await setArchived(chat.telegram_id, archived)
    if (archived) {
      setChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
      if (selectedChat?.telegram_id === chat.telegram_id) {
        setSelectedChat(null)
        setMessages([])
      }
    } else {
      setArchivedChats((prev) => prev.filter((c) => c.telegram_id !== chat.telegram_id))
      setChats((prev) => [...prev, { ...chat, is_archived: false }])
    }
    setContextMenu(null)
  }

  const handleMuteChat = async (chat: Chat, duration?: string) => {
    const muted = !chat.is_muted || !!duration
    await setMuted(chat.telegram_id, muted, duration)
    const muted_until = muted && duration && duration !== 'forever'
      ? new Date(Date.now() + ({ '1h': 3600000, '8h': 28800000, '1d': 86400000 }[duration] ?? 0)).toISOString()
      : null
    setChats((prev) =>
      prev.map((c) => (c.telegram_id === chat.telegram_id ? { ...c, is_muted: muted, muted_until } : c))
    )
    if (selectedChat?.telegram_id === chat.telegram_id) {
      setSelectedChat((c) => c ? { ...c, is_muted: muted, muted_until } : c)
    }
    setContextMenu(null)
  }

  const handleMarkUnread = async (chat: Chat) => {
    const markedUnread = !chat.is_marked_unread
    await setMarkedUnread(chat.telegram_id, markedUnread)
    setChats((prev) =>
      prev.map((c) => (c.telegram_id === chat.telegram_id ? { ...c, is_marked_unread: markedUnread } : c))
    )
    setContextMenu(null)
  }

  const handleToggleArchivedPanel = async () => {
    if (!showArchived) {
      const data = await getArchivedChats()
      setArchivedChats(data)
    }
    setShowArchived((v) => !v)
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

  const handleTyping = useCallback(() => {
    if (!selectedChat) return
    sendTypingAction(selectedChat.telegram_id).catch(() => {})
  }, [selectedChat])

  const handleSend = async (text: string) => {
    if (!selectedChat) return
    await sendMessage(selectedChat.telegram_id, text, replyToMessage?.tg_message_id)
    setReplyToMessage(null)
    await loadMessages(selectedChat)
    // Update chat list sidebar with the sent message
    setChats((prev) =>
      prev.map((c) =>
        c.telegram_id === selectedChat.telegram_id
          ? { ...c, last_message: { text, sender_name: null, date: new Date().toISOString() }, last_activity_at: new Date().toISOString() }
          : c
      )
    )
  }

  const handleSelectTopic = useCallback(async (topicId: number | null, topicTitle: string | null) => {
    setSelectedTopicId(topicId)
    setSelectedTopicTitle(topicTitle)
    if (!selectedChat) return
    setLoadingMessages(true)
    try {
      if (topicId !== null) {
        const data = await getTopicMessages(selectedChat.telegram_id, topicId, LIMIT, 0)
        setMessages(data)
        setHasMore(data.length === LIMIT)
      } else {
        const data = await getMessages(selectedChat.telegram_id, LIMIT, 0)
        setMessages(data)
        setHasMore(data.length === LIMIT)
      }
      setMessageOffset(0)
    } finally {
      setLoadingMessages(false)
    }
  }, [selectedChat, LIMIT])

  const handlePinMessage = async (msg: Message) => {
    if (!selectedChat) return
    const newPinned = !msg.is_pinned
    try {
      await pinMessage(selectedChat.telegram_id, msg.tg_message_id, newPinned)
      setMessages((prev) =>
        prev.map((m) => m.tg_message_id === msg.tg_message_id ? { ...m, is_pinned: newPinned } : m)
      )
    } catch {
      // silently ignore
    }
  }

  const handleSendFiles = async (files: File[], text: string) => {
    if (!selectedChat) return
    setUploadProgress(0)
    try {
      const { promise } = sendFiles(
        selectedChat.telegram_id,
        files,
        text || undefined,
        replyToMessage?.tg_message_id,
        (fraction) => setUploadProgress(fraction),
      )
      await promise
      setReplyToMessage(null)
      await loadMessages(selectedChat)
      // Update chat list sidebar
      const preview = text || `[${files[0]?.type?.startsWith('image/') ? 'photo' : 'file'}]`
      setChats((prev) =>
        prev.map((c) =>
          c.telegram_id === selectedChat.telegram_id
            ? { ...c, last_message: { text: preview, sender_name: null, date: new Date().toISOString() }, last_activity_at: new Date().toISOString() }
            : c
        )
      )
    } finally {
      setUploadProgress(null)
    }
  }

  const handleReact = async (msg: Message, emoji: string | null) => {
    if (!selectedChat) return
    try {
      await reactToMessage(selectedChat.telegram_id, msg.tg_message_id, emoji)
      // Update the message's my_reaction in local state immediately
      setMessages((prev) =>
        prev.map((m) => m.tg_message_id === msg.tg_message_id ? { ...m, my_reaction: emoji } : m)
      )
    } catch {
      // silently ignore
    }
  }

  const handleEdit = (msg: Message) => {
    setEditingMessage(msg)
    setReplyToMessage(null)
  }

  const handleSaveEdit = async (text: string) => {
    if (!selectedChat || !editingMessage) return
    await editMessage(selectedChat.telegram_id, editingMessage.tg_message_id, text)
    setEditingMessage(null)
    // Update local state immediately
    setMessages((prev) =>
      prev.map((m) =>
        m.tg_message_id === editingMessage.tg_message_id
          ? { ...m, text, text_html: null }
          : m
      )
    )
  }

  const handleDelete = (msg: Message) => {
    setDeleteTarget(msg)
  }

  const handleConfirmDelete = async (revoke: boolean) => {
    if (!selectedChat || !deleteTarget) return
    await deleteMessages(selectedChat.telegram_id, [deleteTarget.tg_message_id], revoke)
    setMessages((prev) => prev.filter((m) => m.tg_message_id !== deleteTarget.tg_message_id))
    setDeleteTarget(null)
  }

  const handleForward = (msg: Message) => {
    setForwardTarget(msg)
  }

  const handleForwardToChat = async (targetChat: Chat) => {
    if (!selectedChat || !forwardTarget) return
    await forwardMessages(selectedChat.telegram_id, [forwardTarget.tg_message_id], targetChat.telegram_id)
    setForwardTarget(null)
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

  // Apply client-side sort to guarantee correct order (pinned first)
  const sorted = [...filtered].sort((a, b) => {
    // Pinned chats always come first
    if (a.is_pinned !== b.is_pinned) return a.is_pinned ? -1 : 1

    if (sortBy === 'unread') {
      const ua = a.unread_count ?? 0
      const ub = b.unread_count ?? 0
      if (ub !== ua) return ub - ua
      const da = a.last_activity_at ?? ''
      const db2 = b.last_activity_at ?? ''
      return db2.localeCompare(da)
    }
    if (sortBy === 'alpha') {
      return a.name.localeCompare(b.name)
    }
    // recent (default)
    const da = a.last_activity_at ?? ''
    const db2 = b.last_activity_at ?? ''
    if (da !== db2) return db2.localeCompare(da)
    return a.name.localeCompare(b.name)
  })

  // Clear selected chat when it's no longer in the filtered list
  useEffect(() => {
    if (selectedChat && !sorted.some((c) => c.telegram_id === selectedChat.telegram_id)) {
      setSelectedChat(null)
    }
  }, [sorted, selectedChat])

  // Keyboard shortcuts
  const chatShortcuts = useMemo(() => ({
    'j': () => {
      const idx = selectedChat ? sorted.findIndex((c) => c.telegram_id === selectedChat.telegram_id) : -1
      const next = sorted[idx + 1]
      if (next) handleSelectChat(next)
    },
    'arrowdown': () => {
      const idx = selectedChat ? sorted.findIndex((c) => c.telegram_id === selectedChat.telegram_id) : -1
      const next = sorted[idx + 1]
      if (next) handleSelectChat(next)
    },
    'k': () => {
      const idx = selectedChat ? sorted.findIndex((c) => c.telegram_id === selectedChat.telegram_id) : -1
      const prev = sorted[idx - 1]
      if (prev) handleSelectChat(prev)
    },
    'arrowup': () => {
      const idx = selectedChat ? sorted.findIndex((c) => c.telegram_id === selectedChat.telegram_id) : -1
      const prev = sorted[idx - 1]
      if (prev) handleSelectChat(prev)
    },
    '/': () => searchInputRef.current?.focus(),
    'i': () => setShowInfo((v) => !v),
    'mod+f': () => setShowChatSearch((v) => !v),
    'n': () => setShowNewChat(true),
    'enter': () => {
      if (!selectedChat && sorted.length > 0) handleSelectChat(sorted[0])
    },
  }), [selectedChat, sorted, handleSelectChat])

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
        <div className="border-b border-slate-700/50 px-3 py-3 flex flex-col gap-2">
          <div className="flex gap-2">
            <input
              ref={searchInputRef}
              type="text"
              placeholder="Search chats…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="min-w-0 flex-1 rounded-lg border border-slate-600 bg-slate-700 px-3 py-1.5 text-sm text-gray-200 outline-none placeholder:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30"
            />
            <button
              onClick={() => setShowNewChat(true)}
              title="New conversation"
              className="flex-shrink-0 rounded-lg border border-slate-600 bg-slate-700 px-2 py-1.5 text-slate-400 hover:bg-indigo-500/20 hover:text-indigo-400 hover:border-indigo-500/30 transition-colors"
            >
              <Plus size={16} />
            </button>
          </div>
          <div className="flex rounded-lg border border-slate-700 overflow-hidden text-[11px]">
            {(['recent', 'unread', 'alpha'] as const).map((s) => (
              <button
                key={s}
                onClick={() => setSortBy(s)}
                className={`flex-1 py-1 transition-colors ${
                  sortBy === s
                    ? 'bg-indigo-500/20 text-indigo-400 font-medium'
                    : 'text-slate-500 hover:text-slate-300 hover:bg-slate-700/50'
                }`}
              >
                {s === 'recent' ? 'Recent' : s === 'unread' ? 'Unread' : 'A-Z'}
              </button>
            ))}
          </div>
        </div>
        <div
          ref={chatListRef}
          className="flex-1 overflow-y-auto"
          onScroll={(e) => {
            const el = e.currentTarget
            if (el.scrollHeight - el.scrollTop - el.clientHeight < 400 && !loadingMoreChats) {
              loadMoreChats()
            }
          }}
        >
          {sorted.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-slate-500">No chats found</p>
          ) : (
            sorted.map((chat) => (
              <ChatItem
                key={chat.telegram_id}
                chat={chat}
                selected={selectedChat?.telegram_id === chat.telegram_id}
                onClick={() => handleSelectChat(chat)}
                onToggleTrack={() => handleTrackChat(chat)}
                onHide={() => handleHideChat(chat)}
                onBriefing={() => handleBriefingChat(chat)}
                onContextMenu={(e) => setContextMenu({ chat, x: e.clientX, y: e.clientY })}
                hasScamFlag={flaggedChatIds.has(chat.telegram_id)}
              />
            ))
          )}
          {loadingMoreChats && (
            <p className="px-4 py-3 text-center text-xs text-slate-500">Loading…</p>
          )}
        </div>

        {/* Archived chats panel */}
        <div className="border-t border-slate-700/50">
          <button
            onClick={handleToggleArchivedPanel}
            className="flex w-full items-center gap-1.5 px-3 py-2 text-xs text-slate-500 hover:text-slate-300 transition-colors"
          >
            <Archive size={12} />
            {showArchived ? 'Hide archived chats' : 'Show archived chats'}
          </button>
          {showArchived && (
            <div className="max-h-48 overflow-y-auto border-t border-slate-700/50 bg-slate-900/50">
              {archivedChats.length === 0 ? (
                <p className="px-4 py-3 text-center text-xs text-slate-500">No archived chats</p>
              ) : (
                archivedChats.map((chat) => (
                  <div key={chat.telegram_id} className="flex items-center gap-2 px-3 py-2">
                    <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-slate-700 text-xs font-medium text-slate-400">
                      {chat.name.charAt(0).toUpperCase()}
                    </div>
                    <p title={chat.name} className="min-w-0 flex-1 truncate text-xs text-slate-400">{chat.name}</p>
                    <button
                      onClick={() => handleArchiveChat(chat)}
                      title="Unarchive"
                      className="flex-shrink-0 rounded p-1 text-slate-500 hover:text-slate-300 transition-colors"
                    >
                      <Archive size={12} />
                    </button>
                  </div>
                ))
              )}
            </div>
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

        {/* Context menu */}
        {contextMenu && (
          <ChatContextMenu
            chat={contextMenu.chat}
            position={{ x: contextMenu.x, y: contextMenu.y }}
            onClose={() => setContextMenu(null)}
            onPin={() => handlePinChat(contextMenu.chat)}
            onArchive={() => handleArchiveChat(contextMenu.chat)}
            onMute={(duration) => handleMuteChat(contextMenu.chat, duration)}
            onMarkUnread={() => handleMarkUnread(contextMenu.chat)}
            onHide={() => { handleHideChat(contextMenu.chat); setContextMenu(null) }}
            onBriefing={() => { handleBriefingChat(contextMenu.chat); setContextMenu(null) }}
            onToggleTrack={() => { handleTrackChat(contextMenu.chat); setContextMenu(null) }}
          />
        )}
      </div>

      {/* Message area + optional info panel */}
      <div className="flex flex-1 overflow-hidden">
        <div className="flex flex-1 flex-col overflow-hidden bg-slate-900">
          {selectedChat ? (
            <>
              {/* Header */}
              <div className="flex items-center justify-between border-b border-slate-700/50 bg-slate-800 px-4 py-3">
                <div className="flex items-center gap-2">
                  <div>
                    <h2 className="text-sm font-semibold text-gray-100">{selectedChat.name}</h2>
                    <div className="flex items-center gap-1.5">
                      <p className="text-xs text-slate-400">
                        {(() => {
                          const typingEntry = typingUsers[selectedChat.telegram_id]
                          if (typingEntry && typingEntry.expiresAt > Date.now()) {
                            return <span className="text-indigo-400">typing...</span>
                          }
                          if (selectedChat.type === 'private' && presence) {
                            if (presence.status === 'online') {
                              return <span className="text-green-400">online</span>
                            }
                            if (presence.status === 'offline' && presence.last_seen) {
                              const lastSeen = new Date(presence.last_seen)
                              return <span>last seen {fmtDate(lastSeen.toISOString())}</span>
                            }
                            if (presence.status === 'recently') {
                              return <span>last seen recently</span>
                            }
                            if (presence.status === 'last_week') {
                              return <span>last seen within a week</span>
                            }
                            if (presence.status === 'last_month') {
                              return <span>last seen within a month</span>
                            }
                          }
                          return selectedChat.type
                        })()}
                      </p>
                      {selectedChat.is_forum && (
                        <span className="rounded px-1 text-[10px] font-medium bg-purple-500/20 text-purple-400">forum</span>
                      )}
                      {selectedTopicTitle && (
                        <span className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium bg-indigo-500/20 text-indigo-400">
                          <MessageSquare size={9} />
                          {selectedTopicTitle}
                        </span>
                      )}
                    </div>
                  </div>
                  {selectedChat.type === 'private' && (
                    <button
                      onClick={handleToggleImportant}
                      disabled={togglingImportant}
                      title={isImportant ? 'Remove from important' : 'Mark as important'}
                      className={`rounded-full p-1 transition-colors ${
                        isImportant
                          ? 'text-amber-400 hover:text-amber-300'
                          : 'text-slate-600 hover:text-amber-400'
                      }`}
                    >
                      <Star size={16} className={isImportant ? 'fill-amber-400' : ''} />
                    </button>
                  )}
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => { setShowChatSearch((v) => !v); if (showChatSearch) { setChatSearch(''); setChatSearchResults([]) } }}
                    title="Search in chat"
                    className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs transition-colors ${
                      showChatSearch ? 'bg-indigo-500/20 text-indigo-400' : 'text-slate-400 hover:bg-slate-700'
                    }`}
                  >
                    <Search size={13} />
                    Search
                  </button>
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

              {/* In-chat search bar */}
              {showChatSearch && (
                <div className="border-b border-slate-700/50 bg-slate-800/50 px-4 py-2 flex flex-col gap-2">
                  <div className="flex items-center gap-2">
                    <Search size={14} className="text-slate-500 flex-shrink-0" />
                    <input
                      autoFocus
                      type="text"
                      placeholder="Search messages…"
                      value={chatSearch}
                      onChange={(e) => setChatSearch(e.target.value)}
                      className="flex-1 bg-transparent text-sm text-gray-200 placeholder:text-slate-500 outline-none"
                    />
                    <input
                      type="date"
                      onChange={(e) => handleDateJump(e.target.value)}
                      title="Jump to date"
                      className="rounded border border-slate-600 bg-slate-700 px-2 py-0.5 text-[11px] text-slate-300 outline-none"
                    />
                    <button
                      onClick={() => { setShowChatSearch(false); setChatSearch(''); setChatSearchResults([]) }}
                      className="text-slate-500 hover:text-slate-300"
                    >
                      <X size={14} />
                    </button>
                  </div>
                  {chatSearch.trim() && (
                    <div className="text-[11px] text-slate-500">
                      {searchingChat ? 'Searching…' : `${chatSearchResults.length} result${chatSearchResults.length !== 1 ? 's' : ''}`}
                    </div>
                  )}
                  {chatSearchResults.length > 0 && (
                    <div className="max-h-40 overflow-y-auto flex flex-col gap-0.5">
                      {chatSearchResults.map((msg) => (
                        <button
                          key={msg.tg_message_id}
                          onClick={() => handleJumpToMessage(msg)}
                          className="flex items-center gap-2 rounded px-2 py-1 text-left text-xs hover:bg-slate-700/50 transition-colors"
                        >
                          <span className="text-slate-400 flex-shrink-0">{msg.sender_name || 'Unknown'}</span>
                          <span className="truncate text-slate-500">{msg.text || '[media]'}</span>
                          {msg.date && <span className="ml-auto flex-shrink-0 text-[10px] text-slate-600">{new Date(msg.date).toLocaleDateString()}</span>}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Forum topics panel */}
              {selectedChat.is_forum && (
                <ForumTopicsPanel
                  telegramId={selectedChat.telegram_id}
                  onSelectTopic={handleSelectTopic}
                  selectedTopicId={selectedTopicId}
                />
              )}

              {/* Pinned message banner */}
              {pinnedMessage && (
                <div className="flex items-center gap-2 border-b border-slate-700/50 bg-slate-800/50 px-4 py-2 cursor-pointer hover:bg-slate-700/50 transition-colors"
                  onClick={() => {
                    // Jump to pinned message
                    const el = document.querySelector(`[data-msg-id="${pinnedMessage.tg_message_id}"]`)
                    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
                  }}
                >
                  <Pin size={14} className="text-indigo-400 flex-shrink-0" />
                  {pinnedMessage.media_url && pinnedMessage.media_type === 'photo' && (
                    <img src={pinnedMessage.media_url} alt="" className="h-8 w-8 rounded object-cover flex-shrink-0" />
                  )}
                  <div className="min-w-0 flex-1">
                    <p className="text-[11px] font-medium text-indigo-400">Pinned message</p>
                    <p className="text-xs text-slate-300 truncate">
                      {pinnedMessage.text
                        ? pinnedMessage.text.slice(0, 120)
                        : pinnedMessage.media_type
                          ? `[${pinnedMessage.media_type}]`
                          : ''}
                    </p>
                  </div>
                  <button
                    onClick={(e) => { e.stopPropagation(); setPinnedMessage(null) }}
                    className="flex-shrink-0 text-slate-500 hover:text-slate-300 transition-colors"
                    title="Dismiss pinned message"
                  >
                    <X size={14} />
                  </button>
                </div>
              )}

              {/* Messages */}
              {loadingMessages ? (
                <div className="flex flex-1 items-center justify-center text-sm text-slate-500">
                  Loading…
                </div>
              ) : (
                <div className="flex flex-1 flex-col overflow-hidden">
                  <MessageList
                    messages={messages}
                    chatType={selectedChat.type}
                    highlightTelegramId={highlightMsgId}
                    onReply={setReplyToMessage}
                    onReact={handleReact}
                    onEdit={handleEdit}
                    onDelete={handleDelete}
                    onForward={handleForward}
                    unreadCount={selectedChatUnread}
                    loadingOlder={loadingMore}
                    onScrollNearTop={(hasMore || !noMoreRemote) ? loadMoreMessages : undefined}
                    flaggedSenderIds={flaggedSenderIds}
                  />
                </div>
              )}

              {/* Typing indicator */}
              {(() => {
                const typingEntry = typingUsers[selectedChat.telegram_id]
                if (typingEntry && typingEntry.expiresAt > Date.now()) {
                  return (
                    <div className="px-4 py-1 text-xs text-indigo-400 animate-pulse">
                      {typingEntry.userName} is typing...
                    </div>
                  )
                }
                return null
              })()}

              {/* Compose */}
              <ComposeBar
                onSend={handleSend}
                onSendFiles={handleSendFiles}
                replyTo={replyToMessage}
                onCancelReply={() => setReplyToMessage(null)}
                editingMessage={editingMessage}
                onSaveEdit={handleSaveEdit}
                onCancelEdit={() => setEditingMessage(null)}
                onTyping={handleTyping}
                uploadProgress={uploadProgress}
              />
            </>
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center text-slate-500">
              {navigatingToChat ? (
                <>
                  <Loader2 size={20} className="animate-spin text-slate-400 mb-2" />
                  <p className="text-sm text-slate-400">Loading chat…</p>
                </>
              ) : (
                <p className="text-sm">Select a chat to start</p>
              )}
            </div>
          )}
        </div>

        {/* Info panel */}
        {showInfo && selectedChat && (
          <ChatInfoPanel chat={selectedChat} onClose={() => setShowInfo(false)} />
        )}
      </div>

      {/* Forward chat picker dialog */}
      <ChatPickerDialog
        open={!!forwardTarget}
        onClose={() => setForwardTarget(null)}
        onSelect={handleForwardToChat}
      />

      {/* Delete confirmation dialog */}
      <DeleteConfirmDialog
        message={deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={handleConfirmDelete}
      />

      {/* New Chat Dialog */}
      <NewChatDialog
        open={showNewChat}
        onClose={() => setShowNewChat(false)}
        onChatCreated={handleChatCreated}
      />
    </div>
  )
}
